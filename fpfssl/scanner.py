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


def is_live_last_bar(cfg: AppConfig, last_bar_time, now: datetime | None = None) -> bool:
    """Is the feed's last bar still forming? (Pine barstate.isconfirmed=False).

    * Daily: today's bar is forming while now < close + buffer.
    * Intraday: the bar is forming while now < bar_open + interval + buffer.
    On weekends/holidays — or when the feed lags past the bar end — the last
    bar is already closed, so events on it are confirmed, not LIVE.
    """
    import pandas as pd

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
    import pandas as pd

    a, b = pd.Timestamp(a).date(), pd.Timestamp(b).date()
    if b <= a:
        return 0
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


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
                import pandas as pd

                lag_min = (now_mkt - pd.Timestamp(last_ts).to_pydatetime()).total_seconds() / 60.0
                if lag_min > cfg.scanner.max_lag_minutes:
                    log.warning("%s: feed lags %.0f min (last bar %s), skipped",
                                sym, lag_min, last_ts)
                    return 0
        df = df.tail(cfg.data.history_bars)
        tick = cfg.data.tick_overrides.get(sym, detect_tick(df, sym))
        live_last = is_live_last_bar(cfg, df.index[-1], now_mkt)
        res = Engine(sym, cfg.engine, tick, tf=cfg.data.interval).run(df, live_last_bar=live_last)
        c = res.counters
        log.info("%s [%s]: %d bars%s | OBs %d (active %d) | taps %d | eSSL taps %d | active eSSL %d | fresh %d",
                 sym, tf, len(df), " (LIVE last bar)" if live_last else "",
                 c.get("footprint_ob_created", 0), c.get("active_zones", 0),
                 c.get("taps", 0), c.get("essl_taps", 0), c.get("active_e_ssl", 0), c.get("fresh_active", 0))

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
        poll = max(1.0, self.cfg.scanner.poll_minutes)
        tf = timeframe_label(self.cfg.data.interval)
        log.info("starting live scanner: %d symbols [%s], poll every %.1f min (ctrl-c to stop)",
                 len(self.symbols), tf, poll)
        while True:
            try:
                self.scan_once()
            except Exception as e:  # noqa: BLE001
                log.exception("scan pass failed: %s", e)
            time.sleep(poll * 60)
