"""Live-path scanner tests (offline): does the scanner fire in a live market?

These tests exercise the *whole* live pipeline without the network:

    fake feed -> LiveScanner.scan_symbol -> engine (live_last_bar) -> alert text

The critical regression here is **history trimming**: the scanner used to run
the engine on `df.tail(history_bars)` (500 bars by default).  The engine's
footprint/TAP state machine is path-dependent — a zone born several hundred
bars ago is still the live TAP reference today — so trimming silently deleted
exactly the signals the scanner exists to catch.  `test_live_alert_long_lived_zone`
fails against that old behaviour and pins the fix.

Run:  .venv/bin/python tests/test_live_scanner.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from fpfssl.config import AppConfig, DataConfig, EngineConfig
from fpfssl.data import _cap
from fpfssl.engine import Engine
from fpfssl.events import K_ESSL_TAP, K_TAP
from fpfssl.synthetic import generate_intraday

import fpfssl.scanner as scanner

SESS_DAYS = 60            # 1500 15m bars
SEED = 45                 # composite at bar 825, zone born at bar 140 (age 685)
SYM = "RELIANCE.NS"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class Recorder:
    """Stands in for TelegramNotifier; keeps every message it is asked to send."""

    def __init__(self, ok: bool = True):
        self.messages: list[str] = []
        self.ok = ok
        self.cfg = type("C", (), {"dry_run": False})()

    def send(self, text: str) -> bool:
        if self.ok:
            self.messages.append(text)
        return self.ok

    @property
    def kinds(self) -> list[str]:
        out = []
        for m in self.messages:
            if "ALL RULES MATCH" in m:
                out.append("essl_ob_tap")
            elif "Footprint TAP" in m:
                out.append("footprint_tap")
            elif "eSSL SWEEP" in m:
                out.append("essl_sweep")
            elif "INVALID" in m:
                out.append("zone_invalid")
            elif "DEFENCE" in m:
                out.append("defence")
            elif "New eSSL" in m:
                out.append("essl_created")
            else:
                out.append("other")
        return out


def composite_bars(df, tick: float = 0.05) -> list[int]:
    """Bar indices where a footprint TAP and an eSSL tap fired together."""
    res = Engine(SYM, EngineConfig(), tick, tf="15m").run(df, live_last_bar=False)
    per: dict[str, set] = {}
    for e in res.events:
        per.setdefault(e.date, set()).add(e.kind)
    idx = {d: i for i, d in enumerate(res.dates)}
    return sorted(idx[d] for d, kinds in per.items()
                  if K_TAP in kinds and K_ESSL_TAP in kinds)


def mk_cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.symbols = [SYM]
    cfg.data = DataConfig(source="yahoo", interval="15m", history_bars=500)
    cfg.scanner.min_bars = 50          # the short frames used here are deliberate
    cfg.scanner.provisional_alerts = True
    cfg.scanner.alert_cooldown_minutes = 60
    # never read or write the repo's live dedup state from a test
    cfg.scanner.state_file = os.path.join(tempfile.mkdtemp(prefix="fpfssl-test-"),
                                          "scanner_state.json")
    return cfg


def scanner_with(cfg: AppConfig, frame: pd.DataFrame, now: datetime, notifier=None):
    """A LiveScanner wired to an in-memory feed and clock."""
    sc = scanner.LiveScanner(cfg, notifier or Recorder(), symbols=[SYM])
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: now
    return sc


# ---------------------------------------------------------------------------
# 1. THE regression test: a zone born > 500 bars ago still alerts
# ---------------------------------------------------------------------------
def test_live_alert_long_lived_zone():
    df = generate_intraday(SYM, DataConfig(interval="15m", history_bars=600),
                           days=SESS_DAYS, seed=SEED)
    bars = composite_bars(df)
    assert bars, "generator/engine no longer produce a composite bar for this seed"
    k = bars[0]
    comp_date = df.index[k]

    # the tapped zone must be OLDER than the old 500-bar trim window
    res_full = Engine(SYM, EngineConfig(), 0.05, tf="15m").run(df, live_last_bar=False)
    zborn = {z.id: z.born_bar for z in res_full.zones}
    tap = [e for e in res_full.events if e.kind == K_TAP and e.bar == k][0]
    age = k - zborn[tap.zone_id]
    assert age > 500, f"scenario no longer covers the regression (zone age {age})"

    # ... and prove the old trimming really did destroy it
    trimmed = df.tail(500)
    res_trim = Engine(SYM, EngineConfig(), 0.05, tf="15m").run(trimmed, live_last_bar=False)
    kinds = {e.kind for e in res_trim.events if e.bar >= len(trimmed) - 3}
    assert not (K_TAP in kinds and K_ESSL_TAP in kinds), \
        "the 500-bar trim unexpectedly keeps the composite; pick another scenario"

    cfg = mk_cfg()
    live_frame = df.iloc[:k + 1]                      # composite bar is the live bar
    now = (comp_date + timedelta(minutes=5)).to_pydatetime()
    rec = Recorder()
    sc = scanner_with(cfg, live_frame, now, rec)
    sent = sc.scan_symbol(SYM)

    assert sent >= 1, f"live scanner dropped the signal (frame {len(live_frame)} bars)"
    assert "essl_ob_tap" in rec.kinds, rec.kinds
    composite = next(m for m in rec.messages if "ALL RULES MATCH" in m)
    assert "LIVE (intraday bar" in composite, composite
    assert comp_date.strftime("%Y-%m-%d %H:%M") in composite, composite
    print(f"ok test_live_alert_long_lived_zone (zone age {age} bars, "
          f"{sent} alert(s): {rec.kinds})")


# ---------------------------------------------------------------------------
# 2. Same bar, but closed: the follow-up alert is tagged confirmed
# ---------------------------------------------------------------------------
def test_live_alert_confirmed_after_bar_close():
    df = generate_intraday(SYM, DataConfig(interval="15m", history_bars=600),
                           days=SESS_DAYS, seed=SEED)
    k = composite_bars(df)[0]
    cfg = mk_cfg()
    frame = df.iloc[:k + 1]
    # 30 min after the bar opened: the 15m bar is final, the feed is settled
    now = (df.index[k] + timedelta(minutes=30)).to_pydatetime()
    rec = Recorder()
    sc = scanner_with(cfg, frame, now, rec)
    sent = sc.scan_symbol(SYM)
    assert sent >= 1, "confirmed composite was not alerted"
    composite = next(m for m in rec.messages if "ALL RULES MATCH" in m)
    assert "LIVE (intraday bar" not in composite, composite
    # dedup key records the confirmed state
    assert any("|True|" in key for key in sc.state["alerted"]), sc.state["alerted"]
    print(f"ok test_live_alert_confirmed_after_bar_close ({rec.kinds})")


# ---------------------------------------------------------------------------
# 3. Dedup: the same pass twice sends one message; a later pass re-sends nothing
# ---------------------------------------------------------------------------
def test_alert_dedup_across_passes():
    df = generate_intraday(SYM, DataConfig(interval="15m", history_bars=600),
                           days=SESS_DAYS, seed=SEED)
    k = composite_bars(df)[0]
    cfg = mk_cfg()
    frame = df.iloc[:k + 1]
    now = (df.index[k] + timedelta(minutes=5)).to_pydatetime()
    rec = Recorder()
    sc = scanner_with(cfg, frame, now, rec)
    first = sc.scan_symbol(SYM)
    second = sc.scan_symbol(SYM)
    assert first >= 1 and second == 0, (first, second, rec.kinds)
    assert len(rec.messages) == first
    # later poll of the SAME bar: still silent
    scanner.market_now = lambda c: now + timedelta(minutes=5)
    third = sc.scan_symbol(SYM)
    assert third == 0, (third, rec.kinds)
    print(f"ok test_alert_dedup_across_passes (first={first}, repeat={second + third})")


# ---------------------------------------------------------------------------
# 4. Scanner filters still skip what they should (and say why)
# ---------------------------------------------------------------------------
def test_skips_stale_and_short_history():
    df = generate_intraday(SYM, DataConfig(interval="15m", history_bars=600),
                           days=SESS_DAYS, seed=SEED)
    cfg = mk_cfg()

    # stale: last bar two weeks old
    old_frame = df.iloc[:600]
    now = (old_frame.index[-1] + timedelta(days=14)).to_pydatetime()
    rec = Recorder()
    sc = scanner_with(cfg, old_frame, now, rec)
    assert sc.scan_symbol(SYM) == 0 and not rec.messages

    # too little history
    cfg2 = mk_cfg()
    cfg2.scanner.min_bars = 1000
    sc2 = scanner_with(cfg2, df, now, Recorder())
    assert sc2.scan_symbol(SYM) == 0

    # intraday feed lag past max_lag_minutes while the market is open
    cfg3 = mk_cfg()
    cfg3.scanner.max_lag_minutes = 5
    lag_now = (df.index[600] + timedelta(minutes=60)).replace(hour=12, minute=0)
    rec3 = Recorder()
    sc3 = scanner_with(cfg3, df.iloc[:601], lag_now, rec3)
    assert sc3.scan_symbol(SYM) == 0 and not rec3.messages
    print("ok test_skips_stale_and_short_history")


# ---------------------------------------------------------------------------
# 5. The data layer must not trim history unless asked
# ---------------------------------------------------------------------------
def test_history_is_not_trimmed():
    df = generate_intraday(SYM, DataConfig(interval="15m", history_bars=200),
                           days=SESS_DAYS, seed=SEED)
    cfg = DataConfig(source="yahoo", interval="15m", history_bars=200)
    assert len(_cap(df, cfg)) == len(df), "history was cut to history_bars"
    cfg.max_bars = 300
    assert len(_cap(df, cfg)) == 300
    cfg.max_bars = 0
    assert len(_cap(df, cfg)) == len(df)
    print(f"ok test_history_is_not_trimmed ({len(df)} bars kept)")


# ---------------------------------------------------------------------------
# 6. Session-clock helpers used by the long-running scanner
# ---------------------------------------------------------------------------
def test_session_clock_helpers():
    cfg = AppConfig()
    assert scanner.market_is_open(cfg, datetime(2026, 9, 10, 10, 0)) is True
    assert scanner.market_is_open(cfg, datetime(2026, 9, 12, 10, 0)) is False   # Sat
    assert scanner.minutes_until_close(cfg, datetime(2026, 9, 10, 15, 0)) == 30
    assert scanner.minutes_until_open(cfg, datetime(2026, 9, 10, 8, 0)) == 75
    assert scanner.minutes_until_open(cfg, datetime(2026, 9, 10, 16, 0)) == 17 * 60 + 15
    assert scanner._session_finished(cfg, datetime(2026, 9, 10, 15, 40)) is False  # grace
    assert scanner._session_finished(cfg, datetime(2026, 9, 10, 15, 50)) is True
    assert scanner._session_finished(cfg, datetime(2026, 9, 12, 12, 0)) is True
    print("ok test_session_clock_helpers")


# ---------------------------------------------------------------------------
# 7. `watch_lines` explains an armed (or empty) scan pass
# ---------------------------------------------------------------------------
def test_watch_lines_report_armed_references():
    df = generate_intraday(SYM, DataConfig(interval="15m", history_bars=600),
                           days=SESS_DAYS, seed=SEED)
    cfg = mk_cfg()
    k = composite_bars(df)[0]
    cfg.scanner.min_bars = 50
    res = Engine(SYM, EngineConfig(), 0.05, tf="15m").run(
        df.iloc[:k + 1], live_last_bar=True)
    lines = scanner.watch_lines(res, cfg, 0.05)
    assert any("armed FP-OBs: FP-OB" in ln for ln in lines), lines
    assert any("armed eSSL:   eSSL" in ln for ln in lines), lines
    # a frame with nothing armed must say so instead of staying silent
    res_dead = Engine(SYM, EngineConfig(), 0.05, tf="15m").run(df, live_last_bar=False)
    dead = scanner.watch_lines(res_dead, cfg, 0.05)
    assert dead and ("nothing" in dead[0] or "none" in dead[0] or "none" in dead[1]), dead
    print(f"ok test_watch_lines_report_armed_references ({len(lines)} lines)")


# ---------------------------------------------------------------------------
# 8. Intraday synthetic bars look like a real session feed
# ---------------------------------------------------------------------------
def test_generate_intraday_sessions():
    df = generate_intraday(SYM, DataConfig(interval="15m", history_bars=100), days=5, seed=7)
    per_day = df.groupby(df.index.date).size()
    assert set(per_day.unique()) == {25}, per_day.to_dict()
    assert all(t.hour == 9 and t.minute == 15 for t in
               [df.index[0]]), df.index[0]
    assert {i.weekday() for i in df.index} <= {0, 1, 2, 3, 4}
    assert ((df["high"] >= df[["open", "close", "low"]].max(axis=1) - 1e-9).all())
    print(f"ok test_generate_intraday_sessions ({len(df)} bars, {len(per_day)} sessions)")


def test_run_forever_covers_the_session():
    """`scan` without --once must cover the whole session with one trigger."""
    import types
    import time as _time

    cfg = AppConfig()
    sc = scanner.LiveScanner.__new__(scanner.LiveScanner)
    sc.cfg, sc.symbols = cfg, []
    sc.state = {"alerted": {}, "cooldown": {}}
    sc.notifier = types.SimpleNamespace(send=lambda *a, **k: True)
    passes = {"n": 0}
    sc.scan_once = lambda: (passes.__setitem__("n", passes["n"] + 1), 0)[1]
    sc._save_state = lambda: None

    clock = {"t": datetime(2026, 9, 10, 9, 5)}       # 10 min before the open
    scanner.market_now = lambda c: clock["t"]
    real_sleep = _time.sleep

    def fake_sleep(sec):
        clock["t"] += timedelta(seconds=min(sec, 600))
        assert passes["n"] < 150, "run_forever is spinning"

    try:
        _time.sleep = fake_sleep
        sc.run_forever()
    finally:
        _time.sleep = real_sleep
    assert passes["n"] > 20, f"too few passes over the session: {passes['n']}"
    assert clock["t"].hour == 15 and clock["t"].minute >= 30, clock["t"]
    assert scanner._session_finished(cfg, clock["t"])
    print(f"ok test_run_forever_covers_the_session ({passes['n']} passes, "
          f"until {clock['t'].strftime('%H:%M')})")


ALL = [
    test_live_alert_long_lived_zone,
    test_run_forever_covers_the_session,
    test_live_alert_confirmed_after_bar_close,
    test_alert_dedup_across_passes,
    test_skips_stale_and_short_history,
    test_history_is_not_trimmed,
    test_session_clock_helpers,
    test_watch_lines_report_armed_references,
    test_generate_intraday_sessions,
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
    if failed:
        sys.exit(1)
    print(f"\nAll {len(ALL)} live-scanner tests passed.")


if __name__ == "__main__":
    main()
