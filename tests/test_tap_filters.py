"""Scanner TAP-filter tests (offline): TAP #1-only + fresh-OB-only.

Covers the `scanner.tap_first_only / fresh_ob_only / fresh_ob_max_age_bars`
toggles added for the "tap #1 and fresh OB alert only" request:

  1. tap_first_only=True  -> TAP 2+ never alerts (composite or standalone)
  2. tap_first_only=True  -> TAP 1 still alerts
  3. fresh_ob_only=True   -> old-zone TAP 1 stays silent (age > window)
  4. fresh_ob_only=True   -> young-zone TAP 1 still alerts
  5. both filters together -> only TAP 1 on a young OB alerts

Run:  .venv/bin/python tests/test_tap_filters.py     (or via pytest)
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from fpfssl.config import AppConfig, DataConfig, EngineConfig
from fpfssl.engine import Engine
from fpfssl.events import K_TAP

import fpfssl.scanner as scanner

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from test_engine import composite_rows, make_df  # noqa: E402
from test_live_scanner import FIXED_END, composite_bars, gen_intraday  # noqa: E402

SYM = "RELIANCE.NS"
_ORIG_LOAD = scanner.load_symbol
_ORIG_NOW = scanner.market_now


def _restore():
    scanner.load_symbol = _ORIG_LOAD
    scanner.market_now = _ORIG_NOW


class Recorder:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def mk_daily_cfg(**kw) -> AppConfig:
    cfg = AppConfig()
    cfg.symbols = [SYM]
    cfg.data = DataConfig(source="yahoo", interval="1d", history_bars=100)
    cfg.scanner.min_bars = 10
    cfg.scanner.provisional_alerts = True
    cfg.scanner.alert_cooldown_minutes = 0  # each scan_symbol call is isolated
    cfg.scanner.recent_bars = 1             # isolate the last bar only
    cfg.scanner.state_file = os.path.join(
        tempfile.mkdtemp(prefix="fpfssl-tapfilter-"), "scanner_state.json")
    for k, v in kw.items():
        setattr(cfg.scanner, k, v)
    return cfg


def scan_last_bar(df: pd.DataFrame, end_bar: int, cfg: AppConfig) -> Recorder:
    """Run one scan_symbol pass whose last bar is df.iloc[end_bar]."""
    _restore()
    frame = df.iloc[: end_bar + 1]
    last = pd.Timestamp(frame.index[-1]).to_pydatetime()
    # daily: same date, after the close+settle -> confirmed bar, zero staleness
    now = last.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.weekday() >= 5:  # synthetic daily index can land on a weekend
        now = last + timedelta(hours=2)
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: now
    try:
        sc.scan_symbol(SYM)
    finally:
        _restore()
    return rec


def test_tap1_passes_first_only_filter():
    df = make_df(composite_rows())
    res = Engine("TEST", EngineConfig(), 0.01).run(df, live_last_bar=False)
    taps = {e.bar: e.extra["taps"] for e in res.events if e.kind == K_TAP}
    assert taps.get(60) == 1, taps  # TAP 1 + eSSL tap on bar 60
    cfg = mk_daily_cfg(tap_first_only=True, fresh_ob_only=False,
                       alert_events=["essl_ob_tap", "footprint_tap"])
    rec = scan_last_bar(df, 60, cfg)
    assert len(rec.messages) == 2, [m.splitlines()[0] for m in rec.messages]
    assert any("ALL RULES MATCH" in m for m in rec.messages)
    assert any("Footprint TAP" in m for m in rec.messages)
    print("ok test_tap1_passes_first_only_filter")


def test_tap2_blocked_by_first_only_filter():
    df = make_df(composite_rows())
    res = Engine("TEST", EngineConfig(), 0.01).run(df, live_last_bar=False)
    taps = {e.bar: e.extra["taps"] for e in res.events if e.kind == K_TAP}
    assert taps.get(61) == 2, taps  # second tap on the next bar
    # without the filter the standalone TAP 2 alerts...
    rec_off = scan_last_bar(
        df, 61, mk_daily_cfg(tap_first_only=False, fresh_ob_only=False,
                             alert_events=["footprint_tap"]))
    assert len(rec_off.messages) == 1, rec_off.messages
    # ...with the filter it stays silent
    rec_on = scan_last_bar(
        df, 61, mk_daily_cfg(tap_first_only=True, fresh_ob_only=False,
                             alert_events=["footprint_tap"]))
    assert rec_on.messages == [], rec_on.messages
    print("ok test_tap2_blocked_by_first_only_filter")


def test_old_zone_tap1_passes_without_fresh_filter():
    # long-lived intraday zone (age 685 bars): no filters -> alerts fire
    df = gen_intraday()
    k = composite_bars(df)[0]
    res = Engine(SYM, EngineConfig(), 0.05, tf="15m").run(df, live_last_bar=False)
    zborn = {z.id: z.born_bar for z in res.zones}
    tap = [e for e in res.events if e.kind == K_TAP and e.bar == k][0]
    age = k - zborn[tap.zone_id]
    assert age > 500, age
    cfg = AppConfig()
    cfg.symbols = [SYM]
    cfg.data = DataConfig(source="yahoo", interval="15m", history_bars=500)
    cfg.scanner.min_bars = 50
    cfg.scanner.tap_first_only = False
    cfg.scanner.fresh_ob_only = False
    cfg.scanner.state_file = os.path.join(
        tempfile.mkdtemp(prefix="fpfssl-tapfilter-"), "scanner_state.json")
    _restore()
    frame = df.iloc[: k + 1]
    now = (df.index[k] + timedelta(minutes=5)).to_pydatetime()
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: now
    try:
        sc.scan_symbol(SYM)
    finally:
        _restore()
    assert any("ALL RULES MATCH" in m for m in rec.messages), \
        [m.splitlines()[0] for m in rec.messages]
    print(f"ok test_old_zone_tap1_passes_without_fresh_filter (zone age {age})")


def test_old_zone_tap1_blocked_by_fresh_filter():
    # same old zone, but fresh_ob_only=True with a 50-bar window -> silent
    df = gen_intraday()
    k = composite_bars(df)[0]
    cfg = AppConfig()
    cfg.symbols = [SYM]
    cfg.data = DataConfig(source="yahoo", interval="15m", history_bars=500)
    cfg.scanner.min_bars = 50
    cfg.scanner.tap_first_only = True
    cfg.scanner.fresh_ob_only = True
    cfg.scanner.fresh_ob_max_age_bars = 50
    cfg.scanner.alert_events = ["essl_ob_tap", "footprint_tap"]
    cfg.scanner.state_file = os.path.join(
        tempfile.mkdtemp(prefix="fpfssl-tapfilter-"), "scanner_state.json")
    _restore()
    frame = df.iloc[: k + 1]
    now = (df.index[k] + timedelta(minutes=5)).to_pydatetime()
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: now
    try:
        sent = sc.scan_symbol(SYM)
    finally:
        _restore()
    assert sent == 0 and rec.messages == [], \
        [m.splitlines()[0] for m in rec.messages]
    print("ok test_old_zone_tap1_blocked_by_fresh_filter")


def test_young_zone_tap1_passes_both_filters():
    # composite_rows: OB born bar 55, TAP 1 at bar 60 -> age 5 <= 50
    df = make_df(composite_rows())
    cfg = mk_daily_cfg(tap_first_only=True, fresh_ob_only=True,
                       fresh_ob_max_age_bars=50,
                       alert_events=["essl_ob_tap", "footprint_tap"])
    rec = scan_last_bar(df, 60, cfg)
    assert len(rec.messages) == 2, [m.splitlines()[0] for m in rec.messages]
    # ...but a window smaller than the 5-bar age suppresses it again
    cfg2 = mk_daily_cfg(tap_first_only=True, fresh_ob_only=True,
                        fresh_ob_max_age_bars=3,
                        alert_events=["essl_ob_tap", "footprint_tap"])
    rec2 = scan_last_bar(df, 60, cfg2)
    assert rec2.messages == [], rec2.messages
    print("ok test_young_zone_tap1_passes_both_filters")


ALL = [
    test_tap1_passes_first_only_filter,
    test_tap2_blocked_by_first_only_filter,
    test_old_zone_tap1_passes_without_fresh_filter,
    test_old_zone_tap1_blocked_by_fresh_filter,
    test_young_zone_tap1_passes_both_filters,
]


def main():
    failed = 0
    for t in ALL:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"ERROR {t.__name__}: {e}")
        finally:
            _restore()
    if failed:
        sys.exit(1)
    print(f"\nAll {len(ALL)} tap-filter tests passed.")


if __name__ == "__main__":
    main()
