"""Live-market diagnostics — "is the scanner armed, and why no alert?".

The scanner is silent by design (it only speaks when a rule fires).  When the
market is open and no alert arrives, this module explains *where price sits
relative to every rule* that would have to fire:

* which active footprint OBs are live TAP candidates, how far price is from
  their TAP reference and how many bars of history the zone needed;
* which eSSL pools are active, how far price is from the level, and whether
  the level is inside the configured tap buffer / age limit;
* which scanner-level filter would have dropped the symbol (stale feed, lag,
  too little history);
* which events fired in the recent-bars alert window.

It is used by ``python -m fpfssl diagnose`` and by the Scanner workflow, which
prints the table into the run summary so a silent session is explainable.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .config import AppConfig
from .data import DataError, load_all, load_symbol
from .engine import Engine, detect_tick
from .events import K_ESSL_TAP, K_TAP
from .scanner import (
    interval_minutes,
    is_live_last_bar,
    market_is_open,
    market_now,
    timeframe_label,
    trading_days_between,
)

log = logging.getLogger("fpfssl.diag")


@dataclass
class Watch:
    """One live candidate reference (FP-OB TAP or eSSL level)."""
    kind: str                 # "fp_ob" | "essl"
    obj_id: int
    level: float              # the price that has to be touched
    band: str = ""            # OB band (zones only)
    distance: float = 0.0     # last close - level (negative = price below)
    distance_pct: float = 0.0
    distance_ticks: float = 0.0
    state: str = ""
    age_bars: int = 0
    taps: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def touches_now(self) -> bool:
        return self.distance <= 0 <= self.distance + max(self.distance_ticks, 0)


@dataclass
class SymbolDiag:
    symbol: str
    interval: str
    tf: str
    status: str = "ok"            # ok | skipped
    skip_reason: str = ""
    bars: int = 0
    first_bar: str = ""
    last_bar: str = ""
    last_close: float = 0.0
    tick: float = 0.0
    live_last_bar: bool = False
    market_open: bool = False
    feed_lag_minutes: float = 0.0
    stale_trading_days: int = 0
    counters: dict = field(default_factory=dict)
    zones_active: int = 0
    essl_active: int = 0
    composites: int = 0          # composite bars found in the fetched window
    last_composite: str = ""
    bars_since_composite: int = -1
    watches: list[Watch] = field(default_factory=list)
    recent_events: list[dict] = field(default_factory=list)
    would_alert: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


def _watch_from_zone(z, price: float, tick: float, cfg) -> Watch:
    ref = float(z.source_reference)
    dist = float(price) - ref
    return Watch(
        kind="fp_ob", obj_id=z.id, level=ref,
        band=f"{z.bottom:.2f}-{z.top:.2f}",
        distance=dist,
        distance_pct=(dist / price * 100.0) if price else 0.0,
        distance_ticks=dist / tick if tick else 0.0,
        state=("TAPPED / pending" if z.source_state == 1 else
               ("OLD" if z.source_state < 0 else "armed")),
        taps=z.source_taps,
        detail={
            "born": z.born_date, "departed": bool(z.source_departed),
            "stop": round(float(z.invalidation), 4),
            "evidence": z.evidence_rule, "rvol": round(float(z.evidence_rvol), 3),
            "origin": z.origin_date,
        },
    )


def _watch_from_pool(p, price: float, tick: float, cfg) -> Watch:
    lvl = float(p.lower)
    dist = float(price) - lvl
    age = p.ended_bar - p.born_bar if p.ended_bar >= 0 else -1
    return Watch(
        kind="essl", obj_id=p.id, level=lvl,
        distance=dist,
        distance_pct=(dist / price * 100.0) if price else 0.0,
        distance_ticks=dist / tick if tick else 0.0,
        state=("inside tap buffer"
               if dist <= cfg.engine.essl_tap_buffer_ticks * tick else "unreached"),
        age_bars=age,
        detail={"members": p.members, "born_bar": p.born_bar,
                "max_age_bars": cfg.engine.essl_tap_max_age},
    )


def diag_symbol(
    sym: str,
    cfg: AppConfig,
    df=None,
    now: datetime | None = None,
    live: bool | None = None,
) -> SymbolDiag:
    """Run the same engine pass the scanner runs and explain the state."""
    tf = timeframe_label(cfg.data.interval)
    out = SymbolDiag(symbol=sym, interval=cfg.data.interval, tf=tf)
    now = now if now is not None else market_now(cfg)
    if df is None:
        try:
            df = load_symbol(sym, cfg.data)
        except DataError as e:
            out.status = "skipped"
            out.skip_reason = str(e)
            return out

    out.bars = len(df)
    out.first_bar = str(df.index[0])
    out.last_bar = str(df.index[-1])
    out.last_close = float(df["close"].iloc[-1])
    out.market_open = market_is_open(cfg, now)
    if cfg.data.source == "yahoo":
        out.stale_trading_days = trading_days_between(df.index[-1], now)
        if out.market_open and cfg.data.is_intraday():
            out.feed_lag_minutes = max(
                0.0, (now - df.index[-1].to_pydatetime()).total_seconds() / 60.0)
    out.live_last_bar = (is_live_last_bar(cfg, df.index[-1], now)
                         if live is None else bool(live))

    # scanner-level filters, in the order scan_symbol applies them
    if cfg.data.source == "yahoo":
        if out.stale_trading_days > cfg.scanner.max_stale_days:
            out.status = "skipped"
            out.skip_reason = (f"stale feed: last bar {out.last_bar} is "
                               f"{out.stale_trading_days} trading days old "
                               f"(max_stale_days={cfg.scanner.max_stale_days})")
        elif (cfg.data.is_intraday() and out.market_open
              and out.feed_lag_minutes > cfg.scanner.max_lag_minutes
              and df.index[-1].date() == now.date()):
            out.status = "skipped"
            out.skip_reason = (f"feed lag {out.feed_lag_minutes:.0f} min "
                               f"(max_lag_minutes={cfg.scanner.max_lag_minutes})")
    if (out.status == "ok" and cfg.data.source == "yahoo" and cfg.data.is_intraday()
            and df.index[-1].date() != now.date()):
        out.status = "stale-session"
        out.skip_reason = (f"newest bar {out.last_bar} is from a previous session — "
                           "warm-up only until today's first bar prints")
    if out.status == "ok" and len(df) < cfg.scanner.min_bars:
        out.status = "skipped"
        out.skip_reason = (f"only {len(df)} bars < min_bars={cfg.scanner.min_bars} "
                           "(engine warmup)")

    tick = cfg.data.tick_overrides.get(sym, detect_tick(df, sym))
    out.tick = tick
    res = Engine(sym, cfg.engine, tick, tf=cfg.data.interval).run(
        df, live_last_bar=out.live_last_bar)
    out.counters = dict(res.counters)

    price = out.last_close
    for z in res.zones:
        if z.active:
            out.zones_active += 1
            out.watches.append(_watch_from_zone(z, price, tick, cfg))
    for p in res.pools:
        if p.active and p.scope == 1:
            out.essl_active += 1
            out.watches.append(_watch_from_pool(p, price, tick, cfg))
    out.watches.sort(key=lambda w: abs(w.distance))

    # how often does the composite actually fire on this symbol/timeframe?
    per_bar: dict[str, set] = {}
    for e in res.events:
        if e.kind in (K_TAP, K_ESSL_TAP):
            per_bar.setdefault(e.date, set()).add(e.kind)
    comp_bars = [i for i, d in enumerate(res.dates)
                 if K_TAP in per_bar.get(d, ()) and K_ESSL_TAP in per_bar.get(d, ())]
    out.composites = len(comp_bars)
    if comp_bars:
        out.last_composite = res.dates[comp_bars[-1]]
        out.bars_since_composite = len(df) - 1 - comp_bars[-1]

    cutoff = max(0, len(df) - max(1, int(cfg.scanner.recent_bars or 3)))
    for e in res.events:
        if e.bar < cutoff:
            continue
        out.recent_events.append({
            "date": e.date, "kind": e.kind, "confirmed": e.confirmed,
            "zone_id": e.zone_id, "pool_id": e.pool_id,
        })

    # Would the composite fire right now?
    by_bar: dict[str, set] = {}
    for e in res.events:
        if e.bar >= cutoff:
            by_bar.setdefault(e.date, set()).add(e.kind)
    for date, kinds in sorted(by_bar.items()):
        if K_TAP in kinds and K_ESSL_TAP in kinds:
            out.would_alert.append(f"essl_ob_tap @ {date}")
    if not out.would_alert:
        taps = [w for w in out.watches if w.kind == "fp_ob"]
        essl = [w for w in out.watches if w.kind == "essl"]
        if not taps and not essl:
            out.would_alert.append("no live FP-OB TAP reference and no active eSSL level")
        elif not taps:
            out.would_alert.append("no armed footprint OB (composite needs a TAP as well)")
        elif not essl:
            out.would_alert.append("no active eSSL level to tap")
        else:
            out.would_alert.append(
                "waiting: price must reach an FP-OB reference AND an eSSL level "
                "on the same bar (nearest: %s %.2f / %s %.2f)"
                % (taps[0].kind, taps[0].level, essl[0].kind, essl[0].level))
    return out


def format_diag(d: SymbolDiag) -> str:
    lines: list[str] = []
    head = (f"{d.symbol} [{d.tf}] {d.bars} bars {d.first_bar} → {d.last_bar} "
            f"close {d.last_close:,.2f} tick {d.tick:g}")
    lines.append(head)
    state = ("market OPEN" if d.market_open else "market closed")
    lines.append(f"   {state} | last bar {'FORMING (LIVE)' if d.live_last_bar else 'closed'}"
                 + (f" | feed lag {d.feed_lag_minutes:.0f} min" if d.feed_lag_minutes else "")
                 + (f" | stale {d.stale_trading_days} trading days" if d.stale_trading_days else ""))
    if d.status == "stale-session":
        lines.append(f"   ⏳ {d.skip_reason}")
        return "\n".join(lines)
    if d.status != "ok":
        lines.append(f"   ⛔ SKIPPED — {d.skip_reason}")
        return "\n".join(lines)
    c = d.counters
    lines.append(f"   counters: OBs {c.get('footprint_ob_created', 0)} created / "
                 f"{d.zones_active} active | TAPs {c.get('taps', 0)} | "
                 f"eSSL taps {c.get('essl_taps', 0)} | "
                 f"eSSL {c.get('pools_created', 0)} pools / {d.essl_active} active | "
                 f"sweeps {c.get('essl_sweeps', 0)}")
    rate = (f"{d.composites} composite bar(s) in {d.bars} bars "
            f"({d.composites / d.bars * 100:.2f}%)")
    if d.last_composite:
        rate += f"; last {d.last_composite} ({d.bars_since_composite} bars ago)"
    lines.append(f"   alert rate: {rate}")
    if d.watches:
        lines.append("   live references (closest first):")
        for w in d.watches[:8]:
            tag = "FP-OB" if w.kind == "fp_ob" else "eSSL "
            band = f" band {w.band}" if w.band else ""
            lines.append(
                f"     {tag} #{w.obj_id:<3} level {w.level:>10,.2f}{band} | "
                f"price {w.distance:+,.2f} ({w.distance_pct:+.2f}%, "
                f"{w.distance_ticks:+.0f} ticks) | {w.state} | taps {w.taps}"
                + (f" | born {w.detail.get('born')}" if w.detail.get("born") else ""))
    else:
        lines.append("   live references: none (no armed FP-OB / active eSSL)")
    if d.recent_events:
        ev = ", ".join(f"{e['date']} {e['kind']}{'' if e['confirmed'] else ' (live)'}"
                       for e in d.recent_events[-6:])
        lines.append(f"   recent-bar events ({len(d.recent_events)}): {ev}")
    for a in d.would_alert:
        lines.append(f"   → {a}")
    return "\n".join(lines)


def format_diag_many(diags: list[SymbolDiag], as_json: bool = False) -> str:
    if as_json:
        return json.dumps([d.as_dict() for d in diags], indent=2, default=str)
    return "\n".join(format_diag(d) for d in diags)


def run_diag(cfg: AppConfig, symbols: list[str] | None = None,
             now: datetime | None = None, live: bool | None = None) -> list[SymbolDiag]:
    syms = symbols or list(cfg.symbols)
    # batched fetch for the full universe (identical frames -> identical
    # engine results as the per-symbol path; only the transport differs)
    frames: dict = {}
    if cfg.data.source == "yahoo" and len(syms) > 1 and cfg.data.batch:
        try:
            frames = load_all(syms, cfg.data)
        except DataError as e:  # noqa: BLE001
            log.warning("batch load failed (%s); falling back to per-symbol", e)
    return [diag_symbol(s, cfg, df=frames.get(s), now=now, live=live) for s in syms]


def session_window(cfg: AppConfig) -> tuple[str, str]:
    """`(open, close)` reminder string for logs/summaries."""
    return (cfg.scanner.market_open, cfg.scanner.market_close)


__all__ = [
    "Watch", "SymbolDiag", "diag_symbol", "format_diag", "format_diag_many",
    "run_diag", "interval_minutes", "session_window",
]
