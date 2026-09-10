"""Live scanner: poll Yahoo bars, detect FPFSSL8.2 events, alert via Telegram.

Timeframe-agnostic (the Pine indicator runs on any TF): `data.interval`
selects daily (`1d`) or intraday (`15m`, `5m`, `1h`, ...) bars. The live clock
is the configured market timezone (default Asia/Kolkata for NSE/BSE).

Flow per poll, per symbol:
  1. fetch OHLCV (last bar = still-forming bar when the market is live)
  2. run the faithful engine over the whole history
  3. group events by bar; detect the composite ALL-RULES condition:
       eSSL tap  AND  footprint-source TAP  on the SAME bar
  4. dedupe against persisted state, apply cooldowns, format, send Telegram

Dedup key: symbol | kind | bar-time | confirmed? | zone-or-pool-id
Provisional (forming-bar) alerts carry a distinct key, so after the bar closes
the scanner sends one follow-up "confirmed" version.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta

import pandas as pd

from .config import AppConfig
from .data import DataError, load_symbol
from .engine import Engine, detect_tick
from .events import (
    K_DEFENCE,
    K_ESSL_SWEEP,
    K_ESSL_TAP,
    K_FOOTPRINT,
    K_SSL_CREATED,
    K_TAP,
    K_ZONE_INVALID,
    format_composite,
    format_event,
)
from .telegram import TelegramNotifier

log = logging.getLogger("fpfssl.scanner")


def timeframe_label(interval: str) -> str:
    m = {
        "1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
        "60m": "1H", "90m": "90m", "1h": "1H",
        "1d": "Daily", "d": "Daily",
        "1wk": "Weekly", "1w": "Weekly", "1mo": "Monthly",
    }
    return m.get((interval or "").lower(), interval or "Daily")


def interval_minutes(interval: str) -> float | None:
    m = {
        "1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
        "60m": 60, "90m": 90, "1h": 60,
        "1d": 24 * 60, "d": 24 * 60,
        "1wk": 7 * 24 * 60, "1w": 7 * 24 * 60, "1mo": 30 * 24 * 60,
    }
    return m.get((interval or "").lower())


def market_now(cfg: AppConfig) -> datetime:
    """Current time in the configured market timezone (naive wall time)."""
    tzname = cfg.scanner.market_timezone or "Asia/Kolkata"
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tzname)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - no tzdata: IST = UTC+5:30
        return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _parse_hhmm(s: str, default: str) -> tuple[int, int]:
    try:
        h, m = (s or default).split(":")
        return int(h), int(m)
    except Exception:  # noqa: BLE001
        h, m = default.split(":")
        return int(h), int(m)


def market_is_open(cfg: AppConfig, now: datetime | None = None) -> bool:
    """NSE equity session check: Mon-Fri 09:15-15:30 market time."""
    now = now if now is not None else market_now(cfg)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    oh, om = _parse_hhmm(cfg.scanner.market_open, "09:15")
    ch, cm = _parse_hhmm(cfg.scanner.market_close, "15:30")
    t = (now.hour, now.minute)
    return (oh, om) <= t < (ch, cm)


def _session_bounds(cfg: AppConfig, now: datetime) -> tuple[datetime, datetime]:
    """Today's session open/close as naive datetimes in market time."""
    oh, om = _parse_hhmm(cfg.scanner.market_open, "09:15")
    ch, cm = _parse_hhmm(cfg.scanner.market_close, "15:30")
    return (now.replace(hour=oh, minute=om, second=0, microsecond=0),
            now.replace(hour=ch, minute=cm, second=0, microsecond=0))


def minutes_until_close(cfg: AppConfig, now: datetime | None = None) -> float:
    now = now if now is not None else market_now(cfg)
    _, close_dt = _session_bounds(cfg, now)
    return (close_dt - now).total_seconds() / 60.0


def minutes_until_open(cfg: AppConfig, now: datetime | None = None) -> float | None:
    """Minutes until the next session open (None on weekends)."""
    now = now if now is not None else market_now(cfg)
    open_dt, close_dt = _session_bounds(cfg, now)
    if now < open_dt:
        return (open_dt - now).total_seconds() / 60.0
    # after the close (or a weekend): next weekday's open
    days = 0
    probe = now
    while days < 7:
        probe = probe + timedelta(days=1)
        days += 1
        if probe.weekday() < 5:
            nxt_open, _ = _session_bounds(cfg, probe)
            return (nxt_open - now).total_seconds() / 60.0
    return None


def _session_finished(cfg: AppConfig, now: datetime) -> bool:
    """True once the close (plus a settling grace) has passed.

    The grace matters: the final 15m bar closes at 15:30, and Yahoo needs a
    few minutes before the closing bar is final — the scanner keeps polling
    through `stop_after_close_minutes` so the last bar still gets alerted.
    """
    if now.weekday() >= 5:
        return True
    return minutes_until_close(cfg, now) < -abs(cfg.scanner.stop_after_close_minutes)


def is_live_last_bar(cfg: AppConfig, last_bar_time, now: datetime | None = None) -> bool:
    """Is the feed's last bar still forming? (Pine barstate.isconfirmed=False).

    * Daily: today's bar is forming while now < close + buffer.
    * Intraday: the bar is forming while now < bar_open + interval + buffer.
    On weekends/holidays — or when the feed lags past the bar end — the last
    bar is already closed, so events on it are confirmed, not LIVE.
    """
    if cfg.data.source != "yahoo":
        return False
    now = now if now is not None else market_now(cfg)
    last = pd.Timestamp(last_bar_time).to_pydatetime()
    interval = (cfg.data.interval or "1d").lower()
    buf = timedelta(minutes=2)
    if interval in ("1d", "d"):
        if last.date() != now.date():
            return False
        ch, cm = _parse_hhmm(cfg.scanner.market_close, "15:30")
        close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
        return now < close_dt + timedelta(minutes=15)
    if interval in ("1wk", "1w", "1mo"):
        # weekly/monthly bars are always "forming" intra-period; treat the
        # last bar as provisional only while the market is open today.
        return bool(market_is_open(cfg, now))
    mins = interval_minutes(interval)
    if not mins:
        return False
    bar_end = last + timedelta(minutes=mins)
    return now < bar_end + buf


def trading_days_between(a, b) -> int:
    """Weekday (Mon-Fri) count in (a, b] for stale-data purposes."""
    a, b = pd.Timestamp(a).date(), pd.Timestamp(b).date()
    if b <= a:
        return 0
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def watch_lines(res, cfg: AppConfig, tick: float, limit: int = 3) -> list[str]:
    """One log line describing what is armed right now.

    A silent scan pass is otherwise indistinguishable from a broken one: this
    prints the nearest live TAP reference, the nearest active eSSL level and
    the distance price still has to travel, so `scan` output explains itself.
    """
    price = float(res.close[-1])
    zones = []
    for z in res.zones:
        if not z.active:
            continue
        ref = float(z.source_reference)
        zones.append((abs(price - ref),
                      "FP-OB #%d ref %.2f (band %.2f-%.2f, %s, taps %d) price %+.2f%% away"
                      % (z.id, ref, z.bottom, z.top,
                         "departed" if z.source_departed else "awaiting departure",
                         z.source_taps, (price - ref) / price * 100.0)))
    pools = []
    for p in res.pools:
        if p.active and p.scope == 1:
            lvl = float(p.lower)
            pools.append((abs(price - lvl),
                          "eSSL #%d level %.2f (%d member(s)) price %+.2f%% away"
                          % (p.id, lvl, p.members, (price - lvl) / price * 100.0)))
    zones.sort(key=lambda x: x[0])
    pools.sort(key=lambda x: x[0])
    if not zones and not pools:
        return ["armed: nothing — no active FP-OB and no active eSSL level, "
                "so no TAP/sweep/eSSL event can fire yet"]
    out = []
    if zones:
        out.append("armed FP-OBs: " + " | ".join(t for _, t in zones[:limit]))
    else:
        out.append("armed FP-OBs: none (composite cannot fire without a TAP)")
    if pools:
        out.append("armed eSSL:   " + " | ".join(t for _, t in pools[:limit]))
    else:
        out.append("armed eSSL:   none")
    return out


class LiveScanner:
    def __init__(self, cfg: AppConfig, notifier: TelegramNotifier, symbols: list[str] | None = None):
        self.cfg = cfg
        self.notifier = notifier
        self.symbols = symbols or list(cfg.symbols)
        self.state: dict = {"alerted": {}, "cooldown": {}}
        self._load_state()

    # -- state ---------------------------------------------------------------
    def _load_state(self):
        p = self.cfg.scanner.state_file
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    self.state = json.load(fh)
            except Exception:  # noqa: BLE001
                log.warning("could not read state file %s; starting fresh", p)
        self.state.setdefault("alerted", {})
        self.state.setdefault("cooldown", {})

    def _save_state(self):
        p = self.cfg.scanner.state_file
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        # keep the file bounded
        alerted = self.state["alerted"]
        if len(alerted) > 20000:
            cutoff = (datetime.now() - timedelta(days=14)).isoformat()
            alerted = {k: v for k, v in alerted.items() if v >= cutoff}
            self.state["alerted"] = alerted
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)

    # -- alerting ------------------------------------------------------------
    def _try_alert(self, sym: str, kind: str, message: str, dedup_key: str) -> bool:
        sc = self.cfg.scanner
        now = datetime.now()
        if dedup_key in self.state["alerted"]:
            return False
        cd_key = f"{sym}|{kind}"
        last = self.state["cooldown"].get(cd_key)
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(minutes=sc.alert_cooldown_minutes):
                    return False
            except Exception:  # noqa: BLE001
                pass
        ok = self.notifier.send(message)
        if ok:
            # In-memory dedupe/cooldown always applies (so a --dry-run preview
            # shows exactly what a live pass would send), but dry runs never
            # touch the persisted state file — previewing must not consume
            # live alerts.
            self.state["alerted"][dedup_key] = now.isoformat()
            self.state["cooldown"][cd_key] = now.isoformat()
            if not self.cfg.telegram.dry_run:
                self._save_state()
        return ok

    # -- single symbol pass ----------------------------------------------------
    def scan_symbol(self, sym: str) -> int:
        cfg = self.cfg
        tf = timeframe_label(cfg.data.interval)
        try:
            df = load_symbol(sym, cfg.data)
        except DataError as e:
            log.warning("%s: %s", sym, e)
            return 0
        if len(df) < cfg.scanner.min_bars:
            log.info("%s: only %d bars (< %d), skipped", sym, len(df), cfg.scanner.min_bars)
            return 0
        now_mkt = market_now(cfg)
        last_ts = df.index[-1]
        if cfg.data.source == "yahoo":
            stale_days = trading_days_between(last_ts, now_mkt)
            if stale_days > cfg.scanner.max_stale_days:
                log.warning("%s: last bar %s is %d trading days old, skipped",
                            sym, last_ts, stale_days)
                return 0
            if cfg.data.is_intraday() and market_is_open(cfg, now_mkt):
                open_dt, _ = _session_bounds(cfg, now_mkt)
                if pd.Timestamp(last_ts).to_pydatetime() >= open_dt:
                    # the feed is inside today's session -> a large gap is real lag
                    lag_min = (now_mkt - pd.Timestamp(last_ts).to_pydatetime()).total_seconds() / 60.0
                    if lag_min > cfg.scanner.max_lag_minutes:
                        log.warning("%s: feed lags %.0f min (last bar %s), skipped",
                                    sym, lag_min, last_ts)
                        return 0
                else:
                    log.info("%s: no bar from today's session yet (last %s) — "
                             "holiday or feed not started", sym, last_ts)
        # A restarted scanner (fresh Actions runner, evicted dedup cache) would
        # otherwise re-announce the last `recent_bars` bars of a *previous*
        # session as if they were news. Intraday alerts must belong to the
        # current session: the last bar has to carry today's date.
        if cfg.data.source == "yahoo" and cfg.data.is_intraday() \
                and pd.Timestamp(last_ts).date() != now_mkt.date():
            log.info("%s: newest bar %s is from a previous session — warm-up only, "
                     "nothing to alert yet", sym, last_ts)
            return 0
        # NB: never cut the frame down to history_bars here. The engine's
        # footprint/TAP state machine is path-dependent — an OB born 600 bars
        # ago can be the TAP reference that fires today — so every bar the
        # feed serves is live state. `data.max_bars` is the only cap.
        tick = cfg.data.tick_overrides.get(sym, detect_tick(df, sym))
        live_last = is_live_last_bar(cfg, df.index[-1], now_mkt)
        res = Engine(sym, cfg.engine, tick, tf=cfg.data.interval).run(df, live_last_bar=live_last)
        c = res.counters
        fmt = "%Y-%m-%d %H:%M" if cfg.data.is_intraday() else "%Y-%m-%d"
        log.info("%s [%s]: %d bars %s → %s%s | OBs %d (active %d) | taps %d | eSSL taps %d | active eSSL %d | fresh %d",
                 sym, tf, len(df), df.index[0].strftime(fmt), last_ts.strftime(fmt),
                 " (LIVE last bar)" if live_last else "",
                 c.get("footprint_ob_created", 0), c.get("active_zones", 0),
                 c.get("taps", 0), c.get("essl_taps", 0), c.get("active_e_ssl", 0), c.get("fresh_active", 0))
        for line in watch_lines(res, cfg, tick):
            log.info("%s: %s", sym, line)

        sc = cfg.scanner
        want = set(sc.alert_events or [])
        sent = 0
        cutoff_bar = len(df) - max(1, int(sc.recent_bars or 3))
        by_bar: dict[str, dict] = {}
        for ev in res.events:
            if ev.bar < cutoff_bar:
                continue  # only the latest bars can be new to the user
            d = by_bar.setdefault(ev.date, {"tap": None, "essl": None, "other": []})
            if ev.kind == K_TAP:
                d["tap"] = ev  # latest tap on the bar
            elif ev.kind == K_ESSL_TAP:
                d["essl"] = ev
            else:
                d["other"].append(ev)

        for date, d in sorted(by_bar.items()):
            composite_bar = d["tap"] is not None and d["essl"] is not None
            # composite: ALL RULES = eSSL tap + footprint tap on the same bar
            if "essl_ob_tap" in want and composite_bar:
                tap, essl = d["tap"], d["essl"]
                provisional = not tap.confirmed
                if not (provisional and not sc.provisional_alerts):
                    msg = format_composite(sym, tf, tap, essl)
                    key = f"{sym}|{cfg.data.interval}|essl_ob_tap|{date}|{tap.confirmed}|{tap.zone_id}|{essl.pool_id}"
                    if self._try_alert(sym, "essl_ob_tap", msg, key):
                        sent += 1
            # individual events
            singles = []
            if d["tap"] is not None and "footprint_tap" in want:
                singles.append(("footprint_tap", d["tap"], d["tap"].zone_id))
            # standalone eSSL tap (opt-in); suppressed when the composite fired
            if d["essl"] is not None and "essl_tap" in want and not composite_bar:
                ev = d["essl"]
                singles.append(("essl_tap", ev, ev.pool_id))
            for ev in d["other"]:
                mapping = {
                    K_DEFENCE: "defence",
                    K_ZONE_INVALID: "zone_invalid",
                    K_ESSL_SWEEP: "essl_sweep",
                    K_SSL_CREATED: "essl_created",
                    K_FOOTPRINT: "footprint_created",
                }
                kind = mapping.get(ev.kind)
                if kind and kind in want:
                    singles.append((kind, ev, ev.zone_id or ev.pool_id))
            for kind, ev, obj in singles:
                if not ev.confirmed and not sc.provisional_alerts:
                    continue
                msg = format_event(sym, tf, ev)
                key = f"{sym}|{cfg.data.interval}|{kind}|{date}|{ev.confirmed}|{obj}"
                if self._try_alert(sym, kind, msg, key):
                    sent += 1
        return sent

    # -- loops -----------------------------------------------------------------
    def scan_once(self) -> int:
        t0 = time.time()
        total = 0
        for sym in self.symbols:
            try:
                total += self.scan_symbol(sym)
            except Exception as e:  # noqa: BLE001
                log.exception("symbol %s failed: %s", sym, e)
        if not self.cfg.telegram.dry_run:
            self._save_state()
        tf = timeframe_label(self.cfg.data.interval)
        log.info("scan finished in %.1fs: %d new alert(s) across %d symbols [%s]",
                 time.time() - t0, total, len(self.symbols), tf)
        return total

    def run_forever(self):
        """Poll until the session ends.

        Designed for a single scheduled trigger that has to cover the whole
        NSE session (09:15-15:30 IST): GitHub's cron is best-effort and drops
        most ticks of a 5-minute schedule, so one long job that polls
        internally is far more reliable than 96 one-shot jobs.

        * started before the open   -> waits for the open, then polls
        * started during the session -> polls immediately
        * started after the close   -> one final pass, then exits
        """
        poll = max(1.0, self.cfg.scanner.poll_minutes)
        tf = timeframe_label(self.cfg.data.interval)
        now = market_now(self.cfg)
        log.info("starting live scanner: %d symbols [%s], poll every %.1f min, "
                 "session %s-%s %s (ctrl-c to stop)",
                 len(self.symbols), tf, poll,
                 self.cfg.scanner.market_open, self.cfg.scanner.market_close,
                 self.cfg.scanner.market_timezone)
        # pre-open: wait for the bell instead of hammering the feed — but only
        # when the open is close. A run started hours early does one pass and
        # exits so it does not hold the runner (a later tick starts the session).
        if not market_is_open(self.cfg, now):
            wait_min = minutes_until_open(self.cfg, now)
            if wait_min is None or wait_min > self.cfg.scanner.preopen_wait_minutes:
                log.info("market closed (%s; next open in %s min) — single pass, then exit",
                         now.strftime("%a %H:%M"),
                         "?" if wait_min is None else f"{wait_min:.0f}")
                self.scan_once()
                self._save_state()
                return
            if wait_min > 0:
                log.info("market closed now (%s) — waiting %.0f min for the open",
                         now.strftime("%a %H:%M"), wait_min)
                deadline = now + timedelta(minutes=wait_min)
                while market_now(self.cfg) < deadline:
                    time.sleep(min(60.0, max(1.0, wait_min * 60.0)))
        while True:
            try:
                self.scan_once()
            except Exception as e:  # noqa: BLE001
                log.exception("scan pass failed: %s", e)
            now = market_now(self.cfg)
            if _session_finished(self.cfg, now):
                log.info("session finished (%s) — scanner exiting",
                         now.strftime("%a %H:%M"))
                self._save_state()
                return
            # fixed cadence, but never oversleep the close; inside the settle
            # window after the close keep the same cadence (no busy loop)
            until_close = minutes_until_close(self.cfg, now)
            if until_close <= 0:
                nap = poll * 60.0
            else:
                nap = min(poll * 60.0, max(30.0, until_close * 60.0))
            time.sleep(nap)
