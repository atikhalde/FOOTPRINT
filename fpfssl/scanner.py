"""Live scanner: poll daily bars, detect FPFSSL8.2 events, alert via Telegram.

Flow per poll, per symbol:
  1. fetch daily OHLCV (last bar = still-forming intraday bar when live)
  2. run the faithful engine over the whole history
  3. group events by bar date; detect the composite ALL-RULES condition:
       eSSL tap  AND  footprint-source TAP  on the SAME bar
  4. dedupe against persisted state, apply cooldowns, format, send Telegram

Dedup key: symbol | kind | bar-date | confirmed? | zone-or-pool-id
Provisional (intraday) alerts carry a distinct key, so after the daily close
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

TF = "Daily"


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
            if now - datetime.fromisoformat(last) < timedelta(minutes=sc.alert_cooldown_minutes):
                return False
        ok = self.notifier.send(message)
        if ok:
            self.state["alerted"][dedup_key] = now.isoformat()
            self.state["cooldown"][cd_key] = now.isoformat()
            self._save_state()
        return ok

    # -- single symbol pass ----------------------------------------------------
    def scan_symbol(self, sym: str) -> int:
        cfg = self.cfg
        try:
            df = load_symbol(sym, cfg.data)
        except DataError as e:
            log.warning("%s: %s", sym, e)
            return 0
        if len(df) < cfg.scanner.min_bars:
            log.info("%s: only %d bars (< %d), skipped", sym, len(df), cfg.scanner.min_bars)
            return 0
        last_date = df.index[-1].date()
        if cfg.data.source == "yahoo" and (datetime.now().date() - last_date).days > cfg.scanner.max_stale_days:
            log.warning("%s: last bar %s is stale, skipped", sym, last_date)
            return 0
        df = df.tail(cfg.data.history_bars)
        tick = cfg.data.tick_overrides.get(sym, detect_tick(df))
        live_last = cfg.data.source == "yahoo"
        res = Engine(sym, cfg.engine, tick).run(df, live_last_bar=live_last)
        c = res.counters
        log.info("%s: %d bars | OBs %d (active %d) | taps %d | eSSL taps %d | active eSSL %d | fresh %d",
                 sym, len(df), c.get("footprint_ob_created", 0), c.get("active_zones", 0),
                 c.get("taps", 0), c.get("essl_taps", 0), c.get("active_e_ssl", 0), c.get("fresh_active", 0))

        sc = cfg.scanner
        want = set(sc.alert_events)
        sent = 0
        by_date: dict[str, dict] = {}
        for ev in res.events:
            if ev.date < (df.index[-1] - timedelta(days=2)).strftime("%Y-%m-%d"):
                continue  # only (yesterday, today) can be new to the user
            d = by_date.setdefault(ev.date, {"tap": None, "essl": None, "other": []})
            if ev.kind == K_TAP:
                d["tap"] = ev  # latest tap on the bar
            elif ev.kind == K_ESSL_TAP:
                d["essl"] = ev
            else:
                d["other"].append(ev)

        for date, d in sorted(by_date.items()):
            # composite: ALL RULES = eSSL tap + footprint tap on the same bar
            if "essl_ob_tap" in want and d["tap"] is not None and d["essl"] is not None:
                tap, essl = d["tap"], d["essl"]
                provisional = not tap.confirmed
                if not (provisional and not sc.provisional_alerts):
                    msg = format_composite(sym, TF, tap, essl)
                    key = f"{sym}|essl_ob_tap|{date}|{tap.confirmed}|{tap.zone_id}|{essl.pool_id}"
                    if self._try_alert(sym, "essl_ob_tap", msg, key):
                        sent += 1
            # individual events
            singles = []
            if d["tap"] is not None and "footprint_tap" in want:
                singles.append(("footprint_tap", d["tap"], d["tap"].zone_id))
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
                msg = format_event(sym, TF, ev)
                key = f"{sym}|{kind}|{date}|{ev.confirmed}|{obj}"
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
        self._save_state()
        log.info("scan finished in %.1fs: %d new alert(s) across %d symbols",
                 time.time() - t0, total, len(self.symbols))
        return total

    def run_forever(self):
        poll = max(1.0, self.cfg.scanner.poll_minutes)
        log.info("starting live scanner: %d symbols, poll every %.1f min (ctrl-c to stop)",
                 len(self.symbols), poll)
        while True:
            try:
                self.scan_once()
            except Exception as e:  # noqa: BLE001
                log.exception("scan pass failed: %s", e)
            time.sleep(poll * 60)
