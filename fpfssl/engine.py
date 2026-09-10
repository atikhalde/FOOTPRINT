"""Faithful Python port of the Pine v6 script
"Footprint Source TAP + Fresh SSL + Developing Preview v8.2".

Fidelity notes
--------------
* Bar ordering of every section follows the Pine script exactly:
    (A) if confirmed: setup lifecycle stops
    (B) if confirmed: SSL pool lifecycle + range bookkeeping + pivot emission
    (C) if confirmed: FRESH SSL registry + developing preview
    (D) if confirmed: confirmation state machine (structure latch -> break ->
        departure/origin freeze -> displacement -> eligibility -> OB creation)
    (E) if confirmed: structure-high publication
    (F) if confirmed: footprint evidence registration (LAST) + record pruning
    (G) LIVE source-TAP state machine (runs on EVERY bar, forming bar included)
* `barstate.isconfirmed` is modelled with a per-bar flag: in backtests every
  bar is confirmed; in the live scanner the last (intraday) bar is not.
* The only extension beyond the source script is the eSSL tap/sweep/break
  EVENT EMITTER (group 8), which re-uses the script's own penetration /
  reclaim / touch definitions so live alerts and backtest signals reuse the
  script's exact price semantics.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .config import EngineConfig
from .events import (
    Event,
    K_APPROACH,
    K_DEFENCE,
    K_ESSL_BREAK,
    K_ESSL_SWEEP,
    K_ESSL_TAP,
    K_FRESH_ESSL,
    K_FOOTPRINT,
    K_ISSL_CREATED,
    K_SSL_CREATED,
    K_TAP,
    K_ZONE_INVALID,
)

# ---------------------------------------------------------------------------
# Pine-compatible primitives
# ---------------------------------------------------------------------------

def rma(x: np.ndarray, length: int) -> np.ndarray:
    """ta.rma / Wilder smoothing: SMA-seeded, then alpha = 1/length."""
    n = len(x)
    out = np.full(n, np.nan)
    if n < length:
        return out
    out[length - 1] = np.nanmean(x[:length])
    a = 1.0 / length
    for i in range(length, n):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def sma(x: np.ndarray, length: int) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan)
    if n < length:
        return out
    csum = np.cumsum(np.insert(x, 0, 0.0))
    out[length - 1:] = (csum[length:] - csum[:-length]) / length
    return out


def roll_min(x: np.ndarray, length: int) -> np.ndarray:
    s = pd.Series(x).rolling(length, min_periods=length).min().to_numpy()
    return s


def roll_max(x: np.ndarray, length: int) -> np.ndarray:
    s = pd.Series(x).rolling(length, min_periods=length).max().to_numpy()
    return s


def round_tick(x: float, tick: float) -> float:
    """math.round_to_mintick (nearest tick, half away from zero for positives)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return x
    return math.floor(x / tick + 0.5 + 1e-7) * tick


def floor_tick(x: float, tick: float) -> float:
    """math.floor(price / mintick) * mintick."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return x
    return math.floor(x / tick + 1e-7) * tick


def detect_tick(df: pd.DataFrame) -> float:
    """Infer the symbol tick size from price decimals (fallback 0.01)."""
    vals = []
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            vals.extend(df[col].dropna().tolist())
    if not vals:
        return 0.01
    a = np.array(sorted(set(round(v, 9) for v in vals)))
    diffs = np.diff(a)
    diffs = diffs[diffs > 1e-9]
    if len(diffs) == 0:
        return 0.01
    return float(round(min(diffs), 9))


# ---------------------------------------------------------------------------
# Record types (mirrors Pine `type FootprintSetup / FootprintOB / SSLPool /
# FreshSSLReference`)
# ---------------------------------------------------------------------------
UNBREACHED, TOUCHED, PARTIAL, SWEEP, GAP_RECLAIM, CLOSED_BELOW, GAP_THROUGH, \
    NO_RECLAIM, OLD_RANGE, CLUSTERED, EXPIRED, PRUNED, RECOVERY = range(13)

SSL_STATE_NAMES = {
    UNBREACHED: "Unbreached", TOUCHED: "Touched", PARTIAL: "Partial",
    SWEEP: "Swept / reclaimed", GAP_RECLAIM: "Gap reclaim",
    CLOSED_BELOW: "Closed below", GAP_THROUGH: "Gapped through",
    NO_RECLAIM: "Breached, not reclaimed", OLD_RANGE: "Outside current range",
    CLUSTERED: "Superseded by EQL", EXPIRED: "Expired", PRUNED: "Pruned",
    RECOVERY: "Recovered from below",
}


@dataclass
class Setup:
    id: int
    start_bar: int
    known_bar: int
    top: float
    bottom: float
    atr: float
    invalidation: float
    rule: str
    evidence_rvol: float
    observations: int
    active: bool = True
    used: bool = False
    reason: str = "Pending; not plotted"
    structure: Optional[float] = None
    structure_origin: int = -1
    structure_known_bar: int = -1
    break_bar: int = -1
    departure_bar: int = -1
    displacement_bar: int = -1
    origin_bar: int = -1
    ob_top: Optional[float] = None
    ob_bottom: Optional[float] = None
    ob_invalidation: Optional[float] = None
    raw_origin_high: Optional[float] = None
    raw_origin_low: Optional[float] = None
    departure_atr: Optional[float] = None


@dataclass
class Zone:
    id: int
    born_bar: int
    born_date: str
    top: float
    bottom: float
    midpoint: float
    invalidation: float
    source_initial_reference: float
    source_reference: float
    source_birth_atr: float
    structure: Optional[float]
    origin_bar: int
    origin_date: str
    departure_date: str
    displacement_date: Optional[str]
    precision_method: str
    evidence_rvol: float
    observations: int
    evidence_rule: str
    departure_atr: float
    ssl_note: str = ""
    source_state: int = 0
    source_taps: int = 0
    source_tap_bar: int = -1
    source_departed: bool = False
    source_adjustment_bar: int = -1
    formation_invalidation: Optional[float] = None
    active: bool = True
    terminal_reason: str = ""
    ended_bar: int = -1
    pre_contacts: int = 0
    armed: bool = False


@dataclass
class Pool:
    id: int
    scope: int            # 0 = iSSL (internal), 1 = eSSL (external)
    state: int
    active: bool
    first_origin: int
    last_origin: int
    born_bar: int
    members: int
    lower: float
    upper: float
    anchor: float
    tolerance: float
    ended_bar: int = -1
    terminal_state: int = -1


@dataclass
class FreshRef:
    origin_bar: int
    origin_date: str
    known_bar: int
    price: float
    major_seen: bool
    active: bool = True
    breached_bar: int = -1


@dataclass
class EngineResult:
    events: list[Event]
    zones: list[Zone]
    pools: list[Pool]
    fresh_refs: list[FreshRef]
    setups: list[Setup]
    counters: dict
    atr: np.ndarray
    close: np.ndarray
    low: np.ndarray
    high: np.ndarray
    open: np.ndarray
    volume: np.ndarray
    dates: list[str]


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self, symbol: str, cfg: EngineConfig, tick: float, tf: str = "1D"):
        self.symbol = symbol
        self.cfg = cfg
        self.tick = tick
        self.tf = tf
        self._ssl_events: deque = deque(maxlen=60)

    # -- zone / reference helpers (Pine f_* functions) ----------------------
    def _precision_bounds(self, o, h, l, c):
        m = self.cfg.precision_zone_method
        if m == "Open to low":
            top = o
        elif m == "Body to low":
            top = max(o, c)
        elif m == "Body only":
            top = max(o, c)
        elif m == "Lower half of candle":
            top = l + (h - l) * 0.50
        else:  # "Full candle"
            top = h
        bottom = min(o, c) if m == "Body only" else l
        return top, bottom

    def _source_depth(self):
        return {
            "Proximal": 0.0, "50%": 0.50, "62%": 0.62,
            "70.5%": 0.705, "79%": 0.79, "Distal + 1 tick": 1.0,
        }[self.cfg.source_entry_mode]

    def _source_buffer(self, width, atr):
        tick_b = self.cfg.source_front_run_ticks * self.tick
        atr_b = self.cfg.source_front_run_atr * atr
        mode = self.cfg.source_front_run_mode
        if mode == "ATR":
            return atr_b
        if mode == "Ticks":
            return tick_b
        if mode == "Off":
            return 0.0
        return min(max(tick_b, atr_b), width * self.cfg.source_max_zone_buffer)

    def _source_initial_reference(self, top, bottom, atr):
        width = top - bottom
        base = top - width * self._source_depth()
        value = base + self._source_buffer(width, atr) + self.cfg.source_entry_offset_ticks * self.tick
        if self.cfg.source_entry_mode == "Distal + 1 tick":
            return bottom + self.tick
        return value

    def _leg_state(self, t, origin_bar, top, bottom, invalidation, atr):
        """Pine f_legState: replays bars origin+1 .. t (chronological)."""
        contacts, armed, broken = 0, False, False
        for b in range(origin_bar + 1, t + 1):
            k = b
            failure = self._close[k] if self.cfg.invalidation_mode == "Close" else self._low[k]
            if failure < invalidation:
                broken = True
            overlap = self._low[k] <= top and self._high[k] >= bottom
            if armed and overlap:
                contacts += 1
                armed = False
            elif self._close[k] > top + atr * self.cfg.contact_departure_atr and (contacts == 0 or not overlap):
                armed = True
        return contacts, armed, broken

    # -- main run ------------------------------------------------------------
    def run(self, df: pd.DataFrame, live_last_bar: bool = False) -> EngineResult:
        cfg = self.cfg
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        v = df["volume"].to_numpy(float)
        dates = [d.strftime("%Y-%m-%d") for d in df.index]
        n = len(df)
        self._open, self._high, self._low, self._close, self._dates = o, h, l, c, dates
        tick = self.tick
        eps = tick * 1e-6  # syminfo.mintick * 0.000001

        # ---- global series (Pine top-of-script section) ---------------------
        tr = np.empty(n)
        tr[0] = h[0] - l[0]
        if n > 1:
            pc = c[:-1]
            tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
        atr = rma(tr, cfg.atr_length)
        prior_atr = np.roll(atr, 1); prior_atr[0] = np.nan

        volma = sma(v, cfg.volume_length)
        prior_volma = np.roll(volma, 1); prior_volma[0] = np.nan
        valid_vol = pd.Series(v).where(v >= 0, 0.0).rolling(cfg.volume_length, min_periods=1).sum().to_numpy()
        base_valid_vol = pd.Series(v).where(v >= 0, 0.0).rolling(cfg.evidence_window, min_periods=1).sum().to_numpy()
        support = roll_min(l, cfg.support_length)
        support1 = np.roll(support, 1); support1[0] = np.nan

        src_atr = rma(tr, cfg.source_atr_length)
        src_volma = sma(v, cfg.source_volume_length)
        with np.errstate(invalid="ignore", divide="ignore"):
            src_rvol = np.where(src_volma > 0, v / np.where(np.isnan(src_volma), np.nan, src_volma), 0.0)
        src_range = np.maximum(h - l, tick)
        clv = (c - l) / src_range
        # ta.lowest(low[1], k): k previous bars (excluding current)
        l_prev = np.roll(l, 1)
        src_prior_low = np.full(n, np.nan)
        if n > 1:
            src_prior_low[1:] = pd.Series(l_prev[1:]).rolling(cfg.source_sweep_length, min_periods=cfg.source_sweep_length).min().to_numpy()
        h_prev = np.roll(h, 1)
        src_micro_high = np.full(n, np.nan)
        if n > 1:
            src_micro_high[1:] = pd.Series(h_prev[1:]).rolling(cfg.source_confirm_bos_length, min_periods=cfg.source_confirm_bos_length).max().to_numpy()

        body = np.abs(c - o)
        rng = h - l
        with np.errstate(invalid="ignore", divide="ignore"):
            body_frac = np.where(rng > 0, body / rng, 0.0)
            # closeLocation = (close-low)/candleRange (script definition)
            close_location = np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1.0), 0.0)
        # NOTE: closeLocation (candleRange-denominator) feeds strongDisplacement;
        # sourceCLV (mintick-floor denominator) feeds source defence. Kept separate.
        strong_disp = (
            ~np.isnan(prior_atr) & (prior_atr > 0) & (c > o)
            & (rng >= cfg.minimum_displacement_range * prior_atr)
            & (body_frac >= cfg.minimum_displacement_body)
            & (close_location >= cfg.displacement_close_location)
        )

        # pivots: ta.pivotlow/ta.pivothigh(low/high, d, d) -> confirmed d bars later
        def pivots(series, d, mode):
            out = np.full(n, np.nan)
            if n > 2 * d:
                win = pd.Series(series).rolling(2 * d + 1, min_periods=2 * d + 1)
                ext = (win.min() if mode == "low" else win.max()).to_numpy()
                for t in range(2 * d, n):
                    cand = series[t - d]
                    if not math.isnan(ext[t]) and cand == ext[t]:
                        out[t] = cand
            return out

        minor_low_pivot = pivots(l, cfg.ssl_internal_depth, "low")
        major_low_pivot = pivots(l, cfg.ssl_external_depth, "low")
        major_high_pivot = pivots(h, cfg.ssl_external_depth, "high")
        minor_high_pivot = pivots(h, cfg.ssl_internal_depth, "high")

        # ---- mutable state ----------------------------------------------------
        setups: list[Setup] = []
        zones: list[Zone] = []
        pools: list[Pool] = []
        fresh_refs: list[FreshRef] = []
        events: list[Event] = []
        next_setup_id = next_zone_id = next_pool_id = 1
        footprints_observed = footprint_ob_created = 0
        counters = {}

        known_structure_high: Optional[float] = None
        known_structure_origin = known_structure_bar = -1
        structure_consumed = False

        ssl_major_low: Optional[float] = None
        ssl_major_high: Optional[float] = None
        # *_origin mirrors Pine's tracked state (used by display there); kept
        # for 1:1 state parity even though the engine logic doesn't read them.
        ssl_major_low_origin = ssl_major_high_origin = -1
        ssl_last_major_low_obs = ssl_last_major_high_obs = ssl_last_minor_obs = ssl_last_extern_issued = -1
        ssl_range_key = 0
        ssl_range_broken = False

        fresh_last_minor_origin = fresh_last_major_origin = -1
        # developing preview: display-only in the source; tracked for parity
        dev_candidate: Optional[float] = None
        dev_origin_bar = -1

        def emit(kind, t, zone_id=None, pool_id=None, price=None, price2=None, **extra):
            events.append(Event(
                kind=kind, bar=t, date=dates[t],
                confirmed=(t < n - 1) or not live_last_bar,
                symbol=self.symbol, zone_id=zone_id, pool_id=pool_id,
                price=price, price2=price2, extra=extra,
            ))

        def is_confirmed(t):
            return t < n - 1 or not live_last_bar

        def nearest_bearish(t):
            for j in range(1, cfg.origin_search + 1):
                if t - j >= 0 and c[t - j] < o[t - j]:
                    return j
            return None

        def f_shape(t, offset, atr_v, support_v, mean_vol, min_rvol):
            # Pine series[x] = x bars AGO: close[offset+1] == the close of the
            # bar BEFORE the evidence bar k (a gap-down guard, not a future value).
            k = t - offset
            if k < 1 or k >= n:
                return False
            span = h[k] - l[k]
            if not (v[k] > 0 and not math.isnan(mean_vol) and mean_vol > 0):
                return False
            if math.isnan(atr_v) or atr_v <= 0 or span <= 0 or math.isnan(c[k - 1]):
                return False
            if v[k] / mean_vol < min_rvol:
                return False
            if abs(c[k] - o[k]) > atr_v * cfg.max_evidence_body:
                return False
            if span > atr_v * cfg.max_evidence_range:
                return False
            if (c[k] - l[k]) / span < cfg.evidence_close_location:
                return False
            if (min(o[k], c[k]) - l[k]) / span < cfg.minimum_lower_wick:
                return False
            if max(c[k - 1] - c[k], 0.0) > atr_v * cfg.max_downside_progress:
                return False
            if math.isnan(support_v):
                return False
            if not (l[k] <= support_v + atr_v * cfg.support_distance_atr and c[k] >= support_v):
                return False
            return True

        def stop_setup(f: Setup, reason: str):
            f.active = False
            f.reason = reason

        def register_ssl(t, scope, price, source_bar, creation_atr):
            nonlocal next_pool_id
            created = False
            duplicate = False
            match_idx = -1
            best_dist = None
            for i, old in enumerate(pools):
                if old.scope == scope and (old.first_origin == source_bar or old.last_origin == source_bar):
                    duplicate = True
                if cfg.ssl_show_equal and old.active and old.scope == scope:
                    new_width = max(old.upper, price) - min(old.lower, price)
                    anchor_dist = abs(price - old.anchor)
                    if (anchor_dist <= old.tolerance + eps and new_width <= old.tolerance + eps):
                        if match_idx == -1 or anchor_dist < best_dist:
                            match_idx = i
                            best_dist = anchor_dist
            if duplicate or math.isnan(price) or price <= 0:
                return created
            p = Pool(
                id=next_pool_id, scope=scope, state=UNBREACHED, active=True,
                first_origin=source_bar, last_origin=source_bar, born_bar=t,
                members=1, lower=price, upper=price, anchor=price,
                tolerance=max(tick, creation_atr * cfg.ssl_equal_atr),
            )
            if match_idx >= 0:
                old = pools[match_idx]
                p.first_origin = old.first_origin
                p.lower = min(old.lower, price)
                p.upper = max(old.upper, price)
                p.anchor = old.anchor
                p.tolerance = old.tolerance
                p.members = old.members + 1
                p.state = old.state
                old.active = False
                old.state = CLUSTERED
                old.terminal_state = CLUSTERED
                old.ended_bar = t
            pools.append(p)
            created = True
            next_pool_id += 1
            kind = K_SSL_CREATED if scope == 1 else K_ISSL_CREATED
            emit(kind, t, pool_id=p.id, price=price,
                 members=p.members, origin_bar=p.first_origin,
                 origin_date=dates[p.first_origin], state="Unbreached")
            return created

        def finish_pool(p: Pool, state: int, t: int):
            p.active = False
            p.state = state
            p.terminal_state = state
            p.ended_bar = t

        def ssl_context_note(t, top, bottom, creation_atr):
            for e in reversed(self._ssl_events):
                age = t - e["bar"]
                dist = max(0.0, max(bottom - e["upper"], e["lower"] - top))
                if 0 < age <= cfg.ssl_context_bars and dist <= creation_atr * cfg.ssl_context_distance:
                    scale = "eSSL" if e["scope"] == 1 else "iSSL"
                    tag = "gap reclaim" if e["state"] == GAP_RECLAIM else "reclaim"
                    return f"{scale} {tag} nearby, {age} bars earlier (context only)"
            return ""

        def register_fresh(t, price, origin, major):
            for r in fresh_refs:
                if r.origin_bar == origin:
                    if major and not r.major_seen:
                        r.major_seen = True
                    return False
            if math.isnan(price) or price <= 0:
                return False
            fresh_refs.append(FreshRef(
                origin_bar=origin, origin_date=dates[origin], known_bar=t,
                price=round_tick(price, tick), major_seen=major,
            ))
            if major:
                emit(K_FRESH_ESSL, t, price=round_tick(price, tick),
                     members=1, origin_bar=origin, origin_date=dates[origin])
            return True

        # ---- per-bar loop ------------------------------------------------------
        for t in range(n):
            confirmed = is_confirmed(t)

            # (A) setup lifecycle ------------------------------------------------
            if confirmed:
                for f in setups:
                    if f.active and t > f.known_bar:
                        fp = c[t] if cfg.invalidation_mode == "Close" else l[t]
                        if fp < f.invalidation:
                            stop_setup(f, "Footprint area failed")
                        elif t - f.known_bar > cfg.confirmation_deadline:
                            stop_setup(f, "Confirmation deadline expired")
                        elif f.ob_invalidation is not None and fp < f.ob_invalidation:
                            stop_setup(f, "Frozen origin failed while pending")
                        elif f.departure_bar >= 0 and f.displacement_bar < 0 and t - f.departure_bar > cfg.displacement_grace:
                            stop_setup(f, "Departure lacked strong displacement")
                        elif f.break_bar >= 0 and (t - f.break_bar > cfg.break_grace or c[t] <= f.structure):
                            stop_setup(f, "Buffered break response expired or failed")

            # (B) SSL lifecycle + emission --------------------------------------
            if confirmed and cfg.ssl_enabled:
                for p in pools:
                    if p.active and t > p.born_bar:
                        full_pen = l[t] <= p.lower - cfg.ssl_penetration_ticks * tick + eps
                        overlap = l[t] <= p.upper + eps and h[t] >= p.lower - eps
                        if full_pen:
                            reclaimed = c[t] >= p.upper + cfg.ssl_reclaim_ticks * tick - eps
                            whole_below = h[t] < p.lower - eps
                            opened_below = o[t] < p.lower - eps
                            from_above = (t >= 1) and c[t - 1] >= p.lower - eps
                            gap_from_above = opened_below and from_above
                            final = NO_RECLAIM
                            if whole_below:
                                final = GAP_THROUGH if gap_from_above else CLOSED_BELOW
                            elif reclaimed:
                                final = GAP_RECLAIM if gap_from_above else (SWEEP if (from_above and not opened_below) else RECOVERY)
                            elif c[t] < p.lower - eps:
                                final = CLOSED_BELOW
                            age_bars = t - p.born_bar
                            if final in (SWEEP, GAP_RECLAIM):
                                self._ssl_events.append({
                                    "bar": t, "scope": p.scope, "state": final,
                                    "lower": p.lower, "upper": p.upper, "pool_id": p.id,
                                })
                                if p.scope == 1:
                                    emit(K_ESSL_SWEEP, t, pool_id=p.id, price=p.lower,
                                         low=l[t], close=c[t], penetrated=True,
                                         depth=p.lower - l[t], reclaimed=True,
                                         members=p.members, age_bars=age_bars,
                                         origin_date=dates[p.first_origin],
                                         classification=SSL_STATE_NAMES[final])
                                    counters["ssl_reclaims"] = counters.get("ssl_reclaims", 0) + 1
                            elif final == RECOVERY:
                                counters["ssl_recoveries"] = counters.get("ssl_recoveries", 0) + 1
                            else:
                                counters["ssl_breaks"] = counters.get("ssl_breaks", 0) + 1
                                if p.scope == 1:
                                    emit(K_ESSL_BREAK, t, pool_id=p.id, price=p.lower,
                                         low=l[t], close=c[t], penetrated=True,
                                         depth=max(p.lower - l[t], 0.0), reclaimed=False,
                                         members=p.members, age_bars=age_bars,
                                         origin_date=dates[p.first_origin],
                                         classification=SSL_STATE_NAMES[final])
                            if p.scope == 1 and l[t] <= p.lower + eps:
                                emit(K_ESSL_TAP, t, pool_id=p.id, price=p.lower,
                                     low=l[t], close=c[t], penetrated=True,
                                     depth=max(p.lower - l[t], 0.0),
                                     reclaimed=bool(reclaimed), members=p.members,
                                     age_bars=age_bars, origin_date=dates[p.first_origin],
                                     classification=SSL_STATE_NAMES[final])
                            finish_pool(p, final, t)
                        elif t - p.born_bar >= cfg.ssl_max_age:
                            finish_pool(p, EXPIRED, t)
                        elif overlap:
                            if l[t] < p.upper - eps:
                                if p.state != PARTIAL and p.scope == 1:
                                    emit(K_ESSL_TAP, t, pool_id=p.id, price=p.lower,
                                         low=l[t], close=c[t], penetrated=True,
                                         depth=max(p.lower - l[t], 0.0),
                                         reclaimed=bool(c[t] >= p.upper + cfg.ssl_reclaim_ticks * tick - eps),
                                         members=p.members, age_bars=t - p.born_bar,
                                         origin_date=dates[p.first_origin],
                                         classification="Partial penetration")
                                p.state = PARTIAL
                            elif p.state == UNBREACHED:
                                p.state = TOUCHED
                                if p.scope == 1:
                                    emit(K_ESSL_TAP, t, pool_id=p.id, price=p.lower,
                                         low=l[t], close=c[t], penetrated=False,
                                         depth=0.0,
                                         reclaimed=bool(c[t] >= p.upper + cfg.ssl_reclaim_ticks * tick - eps),
                                         members=p.members, age_bars=t - p.born_bar,
                                         origin_date=dates[p.first_origin],
                                         classification="Touched")
                # external range bookkeeping
                if ssl_major_low is not None and c[t] < ssl_major_low - eps:
                    ssl_range_broken = True
                if not math.isnan(major_low_pivot[t]):
                    origin = t - cfg.ssl_external_depth
                    if origin != ssl_last_major_low_obs:
                        ssl_major_low = round_tick(major_low_pivot[t], tick)
                        ssl_major_low_origin = origin
                        ssl_last_major_low_obs = origin
                        ssl_range_broken = False
                        ssl_range_key += 1
                if not math.isnan(major_high_pivot[t]):
                    origin = t - cfg.ssl_external_depth
                    if origin != ssl_last_major_high_obs:
                        ssl_major_high = round_tick(major_high_pivot[t], tick)
                        ssl_major_high_origin = origin
                        ssl_last_major_high_obs = origin
                        ssl_range_key += 1
                range_ready = (not ssl_range_broken and ssl_major_low is not None
                               and ssl_major_high is not None
                               and ssl_major_high > ssl_major_low + eps)
                for p in pools:
                    if p.active and p.scope == 0:
                        still_internal = (range_ready and p.lower > ssl_major_low + eps
                                          and p.upper < ssl_major_high - eps)
                        if not still_internal:
                            finish_pool(p, OLD_RANGE, t)
                        else:
                            p.tolerance = p.tolerance  # rangeKey refresh (no price change)
                if range_ready and not math.isnan(prior_atr[t]) and prior_atr[t] > 0 \
                        and ssl_major_low_origin != ssl_last_extern_issued:
                    register_ssl(t, 1, ssl_major_low, ssl_major_low_origin, prior_atr[t])
                    ssl_last_extern_issued = ssl_major_low_origin
                if not math.isnan(minor_low_pivot[t]):
                    origin = t - cfg.ssl_internal_depth
                    if origin != ssl_last_minor_obs:
                        price = round_tick(minor_low_pivot[t], tick)
                        inside = (range_ready and price > ssl_major_low + eps
                                  and price < ssl_major_high - eps)
                        if inside and not math.isnan(prior_atr[t]) and prior_atr[t] > 0:
                            register_ssl(t, 0, price, origin, prior_atr[t])
                        ssl_last_minor_obs = origin
                while len(pools) > cfg.ssl_record_cap:
                    idx = 0
                    for i, p in enumerate(pools):
                        if not p.active:
                            idx = i
                            break
                    removed = pools.pop(idx)
                    if removed.active:
                        finish_pool(removed, PRUNED, t)

            # (C) FRESH SSL registry + developing preview ------------------------
            fresh_on = cfg.fresh_show_confirmed or cfg.fresh_show_developing
            # Pine: freshPreviewDepth = External ? externalDepth : internalDepth
            fresh_preview_depth = cfg.ssl_external_depth if cfg.fresh_low_source == "External" else cfg.ssl_internal_depth
            if confirmed and fresh_on:
                for r in fresh_refs:
                    if r.active and t > r.known_bar and l[t] <= r.price - tick + eps:
                        r.active = False
                        r.breached_bar = t
                if cfg.fresh_low_source != "External" and not math.isnan(minor_low_pivot[t]):
                    origin = t - cfg.ssl_internal_depth
                    if origin != fresh_last_minor_origin:
                        register_fresh(t, minor_low_pivot[t], origin, major=False)
                        fresh_last_minor_origin = origin
                if cfg.fresh_low_source != "Internal" and not math.isnan(major_low_pivot[t]):
                    origin = t - cfg.ssl_external_depth
                    if origin != fresh_last_major_origin:
                        register_fresh(t, major_low_pivot[t], origin, major=True)
                        fresh_last_major_origin = origin
                # developing preview (display-only in the source; kept for parity)
                prev_low_ref = np.nan
                if t >= fresh_preview_depth:
                    prev_low_ref = min(l[t - fresh_preview_depth:t])
                if t >= fresh_preview_depth and not math.isnan(prev_low_ref) and l[t] > 0 and l[t] <= prev_low_ref:
                    dev_candidate = round_tick(l[t], tick)
                    dev_origin_bar = t
                if dev_origin_bar >= 0 and t - dev_origin_bar >= fresh_preview_depth:
                    dev_candidate = None
                    dev_origin_bar = -1
                while len(fresh_refs) > cfg.fresh_record_cap:
                    # Pine: prefer the oldest INACTIVE reference; otherwise the
                    # oldest origin overall.
                    idx, oldest_origin, found_inactive = -1, None, False
                    for i, r in enumerate(fresh_refs):
                        if not r.active:
                            if not found_inactive or r.origin_bar < oldest_origin:
                                idx, oldest_origin, found_inactive = i, r.origin_bar, True
                        elif not found_inactive and (idx == -1 or r.origin_bar < oldest_origin):
                            idx, oldest_origin = i, r.origin_bar
                    fresh_refs.pop(idx)

            # (D) confirmation state machine (newest setup first) -----------------
            if confirmed:
                for step in range(len(setups) - 1, -1, -1):
                    f = setups[step]
                    if not (f.active and t > f.known_bar):
                        continue
                    # structure latch
                    if f.structure is None and known_structure_high is not None and not structure_consumed:
                        if (known_structure_bar > f.known_bar and known_structure_origin >= f.start_bar
                                and known_structure_high >= f.top - eps):
                            f.structure = known_structure_high
                            f.structure_origin = known_structure_origin
                            f.structure_known_bar = known_structure_bar
                    # break latch
                    if (f.structure is not None and t > f.structure_known_bar and f.break_bar < 0
                            and c[t] > f.structure + f.atr * cfg.break_clearance):
                        f.break_bar = t
                    # first departure -> freeze origin
                    if f.departure_bar < 0 and c[t] > f.top + f.atr * cfg.departure_clearance:
                        off = nearest_bearish(t)
                        if off is None:
                            stop_setup(f, "No opposing candle before departure")
                        else:
                            ob = t - off
                            sel_top, sel_bot = self._precision_bounds(o[ob], h[ob], l[ob], c[ob])
                            top = round_tick(sel_top, tick)
                            bot = round_tick(sel_bot, tick)
                            inv = floor_tick(l[ob] - max(prior_atr[t] * cfg.invalidation_atr, tick * cfg.invalidation_ticks), tick)
                            intersection = max(0.0, min(top, f.top) - max(bot, f.bottom))
                            linked = (ob >= f.start_bar - cfg.origin_padding
                                      and intersection >= cfg.minimum_link_ticks * tick - eps)
                            valid = ((top - bot) >= cfg.minimum_width_ticks * tick - eps
                                     and (top - bot) <= cfg.max_ob_width_atr * prior_atr[t]
                                     and inv > 0 and inv <= bot)
                            contacts, armed, broken = self._leg_state(t, ob, top, bot, inv, prior_atr[t])
                            if not linked or not valid or broken or contacts >= cfg.max_contacts:
                                stop_setup(f, "Initiating origin not valid / footprint-linked")
                            else:
                                f.departure_bar = t
                                f.origin_bar = ob
                                f.ob_top, f.ob_bottom, f.ob_invalidation = top, bot, inv
                                f.raw_origin_high, f.raw_origin_low = h[ob], l[ob]
                                f.departure_atr = prior_atr[t]
                    # displacement
                    if (f.departure_bar >= 0 and f.displacement_bar < 0 and strong_disp[t]
                            and t - f.departure_bar <= cfg.displacement_grace
                            and c[t] > f.top + f.atr * cfg.departure_clearance):
                        f.displacement_bar = t
                    # eligibility -> create OB
                    eligible = (
                        f.active and not math.isnan(src_atr[t]) and not math.isnan(src_volma[t]) and src_volma[t] > 0
                        and f.displacement_bar >= 0 and f.break_bar >= 0
                        and t - f.break_bar <= cfg.break_grace
                        and c[t] > max(f.top + f.atr * cfg.break_clearance,
                                       f.structure + f.atr * cfg.break_clearance,
                                       f.ob_top)
                    )
                    if eligible:
                        contacts, armed, broken = self._leg_state(t, f.origin_bar, f.ob_top, f.ob_bottom,
                                                                  f.ob_invalidation, f.departure_atr)
                        duplicate = False
                        for old in zones:
                            if old.origin_bar == f.origin_bar:
                                duplicate = True
                            if old.active:
                                shared = max(0.0, min(old.top, f.ob_top) - max(old.bottom, f.ob_bottom))
                                smaller = min(old.top - old.bottom, f.ob_top - f.ob_bottom)
                                if smaller > 0 and shared / smaller >= cfg.duplicate_overlap:
                                    duplicate = True
                        if broken or contacts >= cfg.max_contacts:
                            stop_setup(f, "Origin failed before final confirmation")
                        elif duplicate:
                            f.used = True
                            stop_setup(f, "Already represented by a confirmed FP-OB")
                        else:
                            z = Zone(
                                id=next_zone_id, born_bar=t, born_date=dates[t],
                                top=f.ob_top, bottom=f.ob_bottom,
                                midpoint=round_tick((f.ob_top + f.ob_bottom) * 0.5, tick),
                                invalidation=f.ob_bottom - src_atr[t] * cfg.source_stop_atr,
                                source_initial_reference=self._source_initial_reference(f.ob_top, f.ob_bottom, src_atr[t]),
                                source_reference=0.0,
                                source_birth_atr=src_atr[t],
                                structure=f.structure, origin_bar=f.origin_bar,
                                origin_date=dates[f.origin_bar],
                                departure_date=dates[t], displacement_date=dates[t],
                                precision_method=cfg.precision_zone_method,
                                evidence_rvol=f.evidence_rvol, observations=f.observations,
                                evidence_rule=f.rule, departure_atr=f.departure_atr,
                                formation_invalidation=f.ob_invalidation,
                                pre_contacts=contacts, armed=armed,
                            )
                            z.source_reference = z.source_initial_reference
                            z.ssl_note = cfg.ssl_enabled and ssl_context_note(t, z.top, z.bottom, z.departure_atr) or ""
                            zones.append(z)
                            f.used = True
                            stop_setup(f, "Confirmed footprint-supported OB")
                            footprint_ob_created += 1
                            next_zone_id += 1
                            emit(K_FOOTPRINT, t, zone_id=z.id, price=z.bottom, price2=z.top,
                                 bottom=z.bottom, top=z.top, reference=z.source_reference,
                                 invalidation=z.invalidation, taps=0, max_taps=cfg.source_max_touches,
                                 state="READY", evidence=f.rule,
                                 rvol=f.evidence_rvol, observations=f.observations,
                                 born_date=dates[t], structure=f.structure,
                                 displacement_date=dates[t], ssl_note=z.ssl_note)

            # (E) structure publication -------------------------------------------
            if confirmed:
                if known_structure_high is not None and c[t] > known_structure_high:
                    structure_consumed = True
                sel_pivot = minor_high_pivot[t] if cfg.confirmation_structure == "Internal" else major_high_pivot[t]
                sel_depth = cfg.ssl_internal_depth if cfg.confirmation_structure == "Internal" else cfg.ssl_external_depth
                if not math.isnan(sel_pivot):
                    known_structure_high = round_tick(sel_pivot, tick)
                    known_structure_origin = t - sel_depth
                    known_structure_bar = t
                    structure_consumed = False

            # (F) footprint evidence registration (LAST) + pruning ------------------
            if confirmed:
                prior_volume_ready = (not math.isnan(valid_vol[t - 1]) if t >= 1 else False) \
                    and valid_vol[t - 1] >= cfg.volume_length \
                    and not math.isnan(prior_volma[t]) and prior_volma[t] > 0
                single = prior_volume_ready and f_shape(t, 0, prior_atr[t], support1[t], prior_volma[t], cfg.strong_rvol)
                cluster_ready = (
                    t >= cfg.evidence_window
                    and valid_vol[t - cfg.evidence_window] >= cfg.volume_length
                    and base_valid_vol[t] >= cfg.evidence_window
                    and not math.isnan(volma[t - cfg.evidence_window]) and volma[t - cfg.evidence_window] > 0
                )
                base_atr = atr[t - cfg.evidence_window]
                base_vol = volma[t - cfg.evidence_window]
                base_sup = support[t - cfg.evidence_window]
                repeated = False
                observations, rule = 1, "Single high-volume rejection / absorption-style proxy"
                evidence_ratio = 0.0
                evidence_atr = prior_atr[t]
                # Pine: evidenceTop/Bottom start at the current bar's high/low;
                # the repeated branch WIDENS them to the union of ALL intervening
                # candles (0..firstOffset), and evidenceStart moves to the
                # OLDEST matched bar (bar_index - firstOffset).
                evidence_top = h[t]
                evidence_bottom = l[t]
                evidence_start = t
                if single:
                    evidence_ratio = v[t] / prior_volma[t]
                elif cluster_ready and f_shape(t, 0, base_atr, base_sup, base_vol, cfg.repeated_bar_rvol):
                    matched, matched_rvol, first_offset = 0, 0.0, 0
                    for j in range(cfg.evidence_window):
                        if f_shape(t, j, base_atr, base_sup, base_vol, cfg.repeated_bar_rvol):
                            matched += 1
                            matched_rvol += v[t - j] / base_vol
                            first_offset = j
                    if matched >= cfg.minimum_evidence_bars:
                        mean_rvol = matched_rvol / matched
                        if mean_rvol >= cfg.repeated_mean_rvol:
                            for j in range(0, first_offset + 1):
                                evidence_top = max(evidence_top, h[t - j])
                                evidence_bottom = min(evidence_bottom, l[t - j])
                            if evidence_top - evidence_bottom <= cfg.max_base_width_atr * base_atr:
                                repeated = True
                                evidence_ratio = mean_rvol
                                evidence_atr = base_atr
                                evidence_start = t - first_offset
                                observations = matched
                                rule = "Repeated base-response proxy; pre-base volume reference"
                if single or repeated:
                    top_e = round_tick(evidence_top, tick)
                    bot_e = round_tick(evidence_bottom, tick)
                    width = top_e - bot_e
                    inv = floor_tick(bot_e - max(evidence_atr * cfg.invalidation_atr, cfg.invalidation_ticks * tick), tick)
                    valid = (width >= cfg.minimum_width_ticks * tick - eps
                             and width <= cfg.max_base_width_atr * evidence_atr and inv > 0)
                    duplicate = False
                    for old in setups:
                        shared = width > 0 and max(0.0, min(top_e, old.top) - max(bot_e, old.bottom)) / width
                        if old.active and shared >= cfg.duplicate_overlap:
                            duplicate = True
                        # do not recycle already-used observations into a new cluster
                        # (Pine: evidenceStart of the NEW cluster, oldest matched bar)
                        if old.used and evidence_start <= old.known_bar \
                                and t - old.known_bar <= cfg.evidence_window + cfg.origin_padding:
                            duplicate = True
                    if not duplicate and valid:
                        s = Setup(
                            id=next_setup_id, start_bar=evidence_start, known_bar=t,
                            top=top_e, bottom=bot_e, atr=evidence_atr,
                            invalidation=inv, rule=rule, evidence_rvol=evidence_ratio,
                            observations=observations,
                        )
                        if known_structure_high is not None and not structure_consumed:
                            s.structure = known_structure_high
                            s.structure_origin = known_structure_origin
                            s.structure_known_bar = known_structure_bar
                        setups.append(s)
                        footprints_observed += 1
                        next_setup_id += 1
                while len(setups) > cfg.max_pending_setups:
                    idx = 0
                    for i, s in enumerate(setups):
                        if not s.active:
                            idx = i
                            break
                    rem = setups.pop(idx)
                    if rem.active:
                        stop_setup(rem, "Internal record cap")
                while len(zones) > cfg.max_zones:
                    idx = 0
                    for i, z in enumerate(zones):
                        if not z.active:
                            idx = i
                            break
                    rem = zones.pop(idx)
                    if rem.active:
                        rem.active = False
                        rem.source_state = -1
                        rem.terminal_reason = "Record cap, no longer tracked"
                        rem.ended_bar = t
                        emit(K_ZONE_INVALID, t, zone_id=rem.id, price=c[t],
                             reason=rem.terminal_reason, close=c[t], stop=rem.invalidation,
                             taps=rem.source_taps, state="OLD")

            # (G) LIVE source-TAP state machine (every bar, forming included) ------
            for z in zones:
                if z.source_state < -1 or not z.active:
                    continue
                age = t - z.born_bar
                entry = z.source_reference
                top, bot = z.top, z.bottom
                stop = z.invalidation

                if z.source_state >= 0 and not z.source_departed and h[t] >= top + src_atr[t] * cfg.source_require_departure:
                    z.source_departed = True

                approaching = (z.source_state >= 0 and z.source_departed and age >= cfg.source_min_age
                               and c[t] > entry and l[t] <= entry + src_atr[t] * cfg.source_approach_atr)
                if approaching:
                    emit(K_APPROACH, t, zone_id=z.id, price=l[t], price2=entry,
                         bottom=bot, top=top, reference=entry, invalidation=stop,
                         taps=z.source_taps, max_taps=cfg.source_max_touches,
                         state="APPROACH", evidence=z.evidence_rule, rvol=z.evidence_rvol,
                         observations=z.observations, born_date=z.born_date,
                         structure=z.structure, displacement_date=z.displacement_date)

                touched = (z.source_state >= 0 and z.source_departed and age >= cfg.source_min_age
                           and l[t] <= entry and h[t] >= bot)
                swept = (not math.isnan(src_prior_low[t]) and l[t] < src_prior_low[t])
                qualified = touched and (not cfg.source_require_sweep or swept)
                adjusted = False
                new_ref = None
                if qualified and (z.source_tap_bar < 0 or t > z.source_tap_bar):
                    ref_before = entry
                    z.source_taps += 1
                    z.source_tap_bar = t
                    z.source_state = 1
                    if cfg.source_raise_after_first_tap and z.source_taps == 1:
                        adaptive = max(entry, l[t] + src_atr[t] * cfg.source_repeat_tap_atr)
                        z.source_reference = adaptive
                        z.source_adjustment_bar = t
                        entry = adaptive
                        adjusted = True
                        new_ref = adaptive
                    emit(K_TAP, t, zone_id=z.id, price=l[t], price2=entry,
                         bottom=bot, top=top, reference=ref_before, invalidation=stop,
                         taps=z.source_taps, max_taps=cfg.source_max_touches,
                         state="TAPPED / pending", evidence=z.evidence_rule,
                         rvol=z.evidence_rvol, observations=z.observations,
                         born_date=z.born_date, structure=z.structure,
                         displacement_date=z.displacement_date,
                         sweep=bool(swept), sweep_length=cfg.source_sweep_length,
                         adjusted=adjusted, new_reference=new_ref)

                pending = z.source_state == 1 and z.source_tap_bar >= 0 and t - z.source_tap_bar <= cfg.source_confirm_bars
                micro_bos = (not math.isnan(src_micro_high[t]) and c[t] > src_micro_high[t])
                defence = (confirmed and pending and c[t] > o[t]
                           and clv[t] >= cfg.source_confirm_clv
                           and src_rvol[t] >= cfg.source_confirm_rvol
                           and c[t] > top and micro_bos)
                if defence:
                    z.source_state = 2
                    emit(K_DEFENCE, t, zone_id=z.id, price=c[t],
                         bottom=bot, top=top, reference=z.source_reference, invalidation=stop,
                         taps=z.source_taps, max_taps=cfg.source_max_touches, state="DEFENCE",
                         evidence=z.evidence_rule, rvol=z.evidence_rvol,
                         observations=z.observations, born_date=z.born_date,
                         structure=z.structure, displacement_date=z.displacement_date,
                         rvol_def=src_rvol[t], clv=clv[t])

                if z.source_state == 1 and t - z.source_tap_bar > cfg.source_confirm_bars:
                    z.source_state = 0

                invalid = z.source_state >= 0 and (c[t] < stop or z.source_taps > cfg.source_max_touches)
                if invalid:
                    z.source_state = -1
                    z.active = False
                    z.armed = False  # f_retireZone
                    z.ended_bar = t
                    reason = "Source live-close stop condition" if c[t] < stop else "Source tap count exceeded maximum"
                    z.terminal_reason = reason
                    emit(K_ZONE_INVALID, t, zone_id=z.id, price=c[t],
                         reason=reason, close=c[t], stop=stop, taps=z.source_taps, state="OLD")

            # eSSL tap on the FORMING bar only (confirmed bars handled in (B)).
            # Independent of zones: must run even when no zone is active.
            if not confirmed and cfg.ssl_enabled:
                for p in pools:
                    if p.active and p.scope == 1 and t > p.born_bar:
                        level = p.lower
                        if l[t] <= level + cfg.essl_tap_buffer_ticks * tick + eps \
                                and t - p.born_bar <= cfg.essl_tap_max_age:
                            pen = bool(l[t] <= level - cfg.ssl_penetration_ticks * tick + eps)
                            emit(K_ESSL_TAP, t, pool_id=p.id, price=level,
                                 low=l[t], close=c[t], penetrated=pen,
                                 depth=max(level - l[t], 0.0) if pen else 0.0,
                                 reclaimed=bool(c[t] >= level + cfg.ssl_reclaim_ticks * tick - eps),
                                 members=p.members, age_bars=t - p.born_bar,
                                 origin_date=dates[p.first_origin],
                                 classification="LIVE tap (forming bar)")

        counters.update({
            "footprints_observed": footprints_observed,
            "footprint_ob_created": footprint_ob_created,
            "taps": sum(1 for e in events if e.kind == K_TAP),
            "defence": sum(1 for e in events if e.kind == K_DEFENCE),
            "essl_taps": sum(1 for e in events if e.kind == K_ESSL_TAP),
            "essl_sweeps": sum(1 for e in events if e.kind == K_ESSL_SWEEP),
            "essl_breaks": sum(1 for e in events if e.kind == K_ESSL_BREAK),
            "pools_created": next_pool_id - 1,
            "zones_created": len(zones),
            "active_zones": sum(1 for z in zones if z.active),
            "active_e_ssl": sum(1 for p in pools if p.active and p.scope == 1),
            "active_i_ssl": sum(1 for p in pools if p.active and p.scope == 0),
            "fresh_active": sum(1 for r in fresh_refs if r.active),
        })
        return EngineResult(
            events=events, zones=zones, pools=pools, fresh_refs=fresh_refs,
            setups=setups, counters=counters, atr=atr, close=c, low=l, high=h,
            open=o, volume=v, dates=dates,
        )


def summarize_state(res: EngineResult) -> dict:
    """Current-view summary used by `report` and alert context."""
    return {
        "active_zones": [
            {
                "id": z.id, "top": z.top, "bottom": z.bottom, "midpoint": z.midpoint,
                "reference": z.source_reference, "invalidation": z.invalidation,
                "taps": z.source_taps, "state": z.source_state,
                "born": z.born_date, "departed": z.source_departed,
            } for z in res.zones if z.active
        ],
        "e_ssl": [
            {
                "id": p.id, "level": p.lower, "upper": p.upper, "members": p.members,
                "state": SSL_STATE_NAMES.get(p.state, str(p.state)),
                "born_bar_age": None, "first_origin": p.first_origin,
            } for p in res.pools if p.active and p.scope == 1
        ],
        "i_ssl": [
            {"id": p.id, "level": p.lower, "members": p.members,
             "state": SSL_STATE_NAMES.get(p.state, str(p.state))}
            for p in res.pools if p.active and p.scope == 0
        ],
        "fresh": [
            {"origin_bar": r.origin_bar, "origin": r.origin_date, "price": r.price,
             "major": r.major_seen, "active": r.active}
            for r in res.fresh_refs if r.active
        ],
        "pending_setups": sum(1 for s in res.setups if s.active),
    }
