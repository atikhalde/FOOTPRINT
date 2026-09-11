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
SEED = 40                 # composite at bar 634 (swept+reclaimed eSSL 264.8,
                          # zone born at bar 99, age 535); bar 785 closes below
                          # three eSSL levels = the BREAK bar (never a touch)
SYM = "RELIANCE.NS"
# Pin the synthetic calendar. generate_intraday defaults to Timestamp.today(),
# so a hardcoded "next morning" would silently become *the same session* every
# weekday the suite rolls forward (the 2026-09-11 failure of
# test_no_alerts_for_previous_session_bars).
FIXED_END = pd.Timestamp("2026-09-10")

_ORIG_LOAD = scanner.load_symbol
_ORIG_NOW = scanner.market_now


def _restore_scanner():
    scanner.load_symbol = _ORIG_LOAD
    scanner.market_now = _ORIG_NOW


def gen_intraday(**kw):
    """Offline 15m frame with a pinned last session (calendar-stable)."""
    days = kw.pop("days", SESS_DAYS)
    seed = kw.pop("seed", SEED)
    end = kw.pop("end", FIXED_END)
    cfg = kw.pop("cfg", None) or DataConfig(interval="15m", history_bars=600)
    return generate_intraday(SYM, cfg, days=days, seed=seed, end=end, **kw)


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
    _restore_scanner()
    sc = scanner.LiveScanner(cfg, notifier or Recorder(), symbols=[SYM])
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: now
    return sc


def next_session_morning(bar_time) -> datetime:
    """09:16 IST on the next weekday after `bar_time` (before that session's first print)."""
    t = pd.Timestamp(bar_time).to_pydatetime()
    now = t.replace(hour=9, minute=16, second=0, microsecond=0) + timedelta(days=1)
    while now.weekday() >= 5:
        now += timedelta(days=1)
    return now


# ---------------------------------------------------------------------------
# 1. THE regression test: a zone born > 500 bars ago still alerts
# ---------------------------------------------------------------------------
def test_live_alert_long_lived_zone():
    df = gen_intraday()
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
    df = gen_intraday()
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
    df = gen_intraday()
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
    df = gen_intraday()
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
# 4b. A restarted scanner must not re-announce a previous session's bars
# ---------------------------------------------------------------------------
def test_no_alerts_for_previous_session_bars():
    df = gen_intraday()
    k = composite_bars(df)[0]
    cfg = mk_cfg()
    frame = df.iloc[:k + 1]
    rec = Recorder()
    # next weekday morning, before the first bar of the new session prints.
    # Must be derived from the composite stamp — a hardcoded date becomes the
    # *same* session the moment generate_intraday's calendar rolls forward.
    now = next_session_morning(df.index[k])
    assert now.date() != pd.Timestamp(df.index[k]).date()
    sc = scanner_with(cfg, frame, now, rec)
    assert sc.scan_symbol(SYM) == 0, rec.kinds
    # ... and the same frame during its own session *does* alert
    sc2 = scanner_with(mk_cfg(), frame, (df.index[k] + timedelta(minutes=5)).to_pydatetime(), Recorder())
    assert sc2.scan_symbol(SYM) >= 1

    # Restart after today's 09:15 has printed: yesterday's last bars still sit
    # inside `recent_bars`, but must not be re-announced.
    comp_date = pd.Timestamp(df.index[k]).date()
    j = k + 1
    while j < len(df) and pd.Timestamp(df.index[j]).date() == comp_date:
        j += 1
    assert j < len(df), "fixture needs a following session"
    mixed_cfg = mk_cfg()
    mixed_cfg.scanner.recent_bars = max(3, j - k + 1)  # keep the composite in-window
    rec_m = Recorder()
    sc_m = scanner_with(mixed_cfg, df.iloc[:j + 1],
                        (df.index[j] + timedelta(minutes=5)).to_pydatetime(), rec_m)
    sc_m.scan_symbol(SYM)
    stamp = df.index[k].strftime("%Y-%m-%d %H:%M")
    leaked = [m for m in rec_m.messages if stamp in m]
    assert not leaked, f"previous-session composite re-announced after today's open: {leaked}"
    print("ok test_no_alerts_for_previous_session_bars")


# ---------------------------------------------------------------------------
# 5. The data layer must not trim history unless asked
# ---------------------------------------------------------------------------
def test_history_is_not_trimmed():
    df = gen_intraday(cfg=DataConfig(interval="15m", history_bars=200))
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
    df = gen_intraday()
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
    df = gen_intraday(cfg=DataConfig(interval="15m", history_bars=100), days=5, seed=7)
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
        _restore_scanner()
    assert passes["n"] > 20, f"too few passes over the session: {passes['n']}"
    # stopped at/after the close, and no later than one poll past it
    stop_min = clock["t"].hour * 60 + clock["t"].minute
    assert 15 * 60 + 30 <= stop_min <= 16 * 60 + 20, clock["t"]
    assert scanner._session_finished(cfg, clock["t"])
    print(f"ok test_run_forever_covers_the_session ({passes['n']} passes, "
          f"until {clock['t'].strftime('%H:%M')})")


# ---------------------------------------------------------------------------
# 9. Stopping the poller (ctrl-c / "Cancel workflow" / runtime budget)
# ---------------------------------------------------------------------------
def _poller_scanner(cfg, clock, passes):
    """A LiveScanner whose clock is fake, sleeps are instant and passes count."""
    import types

    sc = scanner.LiveScanner.__new__(scanner.LiveScanner)
    sc.cfg, sc.symbols = cfg, ["A.NS", "B.NS", "C.NS"]
    sc.state = {"alerted": {}, "cooldown": {}}
    sc.notifier = types.SimpleNamespace(send=lambda *a, **k: True)
    sc.scan_once = lambda: (passes.__setitem__("n", passes["n"] + 1), 0)[1]
    sc._save_state = lambda: None
    scanner.market_now = lambda c: clock["t"]
    return sc


def test_stop_signal_interrupts_the_poll_nap():
    """A stop request during the nap must end the loop — not wait 15 minutes.

    Regression: the poller slept in one `time.sleep(nap)` call, so a cancel
    only took effect when the nap happened to end.
    """
    import time as _time

    cfg = AppConfig()
    cfg.scanner.poll_minutes = 15
    clock = {"t": datetime(2026, 9, 10, 10, 0)}      # market open
    passes = {"n": 0}
    sc = _poller_scanner(cfg, clock, passes)
    real_sleep = _time.sleep
    naps = {"n": 0}

    def fake_sleep(sec):
        naps["n"] += 1
        clock["t"] += timedelta(seconds=min(sec, 600))
        # stop the poller from inside the very first nap, like a signal would
        if naps["n"] == 1:
            sc.request_stop("received SIGINT")

    try:
        _time.sleep = fake_sleep
        reason = sc.run_forever()
    finally:
        _time.sleep = real_sleep
        _restore_scanner()
    assert passes["n"] == 1, f"expected one pass then stop, got {passes['n']}"
    assert "SIGINT" in reason, reason
    assert clock["t"] < datetime(2026, 9, 10, 10, 16), f"kept polling after the stop: {clock['t']}"
    print(f"ok test_stop_signal_interrupts_the_poll_nap (stopped after {naps['n']} sleep chunk(s): {reason})")


def test_stop_signal_is_honoured_even_when_sigint_was_ignored():
    """`install_stop_handlers` must override an inherited SIG_IGN.

    Regression: a scanner started in the background by a non-interactive shell
    (CI runner, `scan &`, nohup) inherits SIGINT set to SIG_IGN, and Python
    then never installs its KeyboardInterrupt handler — Ctrl-C and GitHub's
    "Cancel workflow" were silently swallowed and the job ran on for hours.
    """
    import signal

    cfg = AppConfig()
    clock = {"t": datetime(2026, 9, 10, 10, 0)}
    passes = {"n": 0}
    sc = _poller_scanner(cfg, clock, passes)
    original = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # what a backgrounded job inherits
        sc.install_stop_handlers()
        assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN, \
            "SIGINT is still ignored: cancel would be swallowed"
        os.kill(os.getpid(), signal.SIGINT)            # deliver a real cancel
        assert sc.stop_requested, "SIGINT did not reach the scanner"
        assert "SIGINT" in sc.stop_reason, sc.stop_reason
    finally:
        sc.restore_stop_handlers()
        signal.signal(signal.SIGINT, original)
        _restore_scanner()
    print("ok test_stop_signal_is_honoured_even_when_sigint_was_ignored")


def test_max_runtime_minutes_stops_the_poller():
    """`max_runtime_minutes` bounds a run so CI never has to kill it mid-pass."""
    import time as _time

    cfg = AppConfig()
    cfg.scanner.poll_minutes = 15
    cfg.scanner.max_runtime_minutes = 30               # 2 passes of nap
    clock = {"t": datetime(2026, 9, 10, 10, 0)}
    passes = {"n": 0}
    sc = _poller_scanner(cfg, clock, passes)
    real_sleep, real_monotonic = _time.sleep, _time.monotonic
    tick = {"t": 0.0}

    def fake_sleep(sec):
        tick["t"] += sec                               # fake clock drives monotonic too
        clock["t"] += timedelta(seconds=min(sec, 600))

    try:
        _time.sleep = fake_sleep
        _time.monotonic = lambda: tick["t"]
        reason = sc.run_forever()
    finally:
        _time.sleep = real_sleep
        _time.monotonic = real_monotonic
        _restore_scanner()
    assert "runtime limit" in reason, reason
    assert passes["n"] >= 1, "no pass ran before the budget check"
    assert clock["t"] < datetime(2026, 9, 10, 11, 0), f"outlived its budget: {clock['t']}"
    print(f"ok test_max_runtime_minutes_stops_the_poller ({passes['n']} pass(es), {reason})")


def test_stop_saves_dedup_state():
    """A cancelled run must persist its dedup state, or the next one re-announces."""
    import json
    import time as _time

    tmp = tempfile.mkdtemp(prefix="fpfssl-stop-")
    cfg = AppConfig()
    cfg.symbols = []
    cfg.data.source = "synthetic"
    cfg.scanner.state_file = os.path.join(tmp, "state", "scanner_state.json")
    clock = {"t": datetime(2026, 9, 10, 10, 0)}
    scanner.market_now = lambda c: clock["t"]
    sc = scanner.LiveScanner(cfg, Recorder())
    self_stop = {"n": 0}
    real_sleep = _time.sleep

    def fake_sleep(sec):
        self_stop["n"] += 1
        clock["t"] += timedelta(seconds=min(sec, 600))
        if self_stop["n"] == 1:
            sc.request_stop("received SIGTERM")

    try:
        _time.sleep = fake_sleep
        reason = sc.run_forever()
    finally:
        _time.sleep = real_sleep
        _restore_scanner()
    assert "SIGTERM" in reason, reason
    with open(cfg.scanner.state_file, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert set(saved) >= {"alerted", "cooldown"}, saved
    print(f"ok test_stop_saves_dedup_state (state written to {cfg.scanner.state_file})")


def test_scan_once_stops_between_symbols():
    """A stop mid-pass must not finish a full-NSE scan (thousands of symbols)."""
    cfg = AppConfig()
    cfg.data.source = "synthetic"        # offline: no yahoo batch fetch
    clock = {"t": datetime(2026, 9, 10, 10, 0)}
    passes = {"n": 0}
    sc = _poller_scanner(cfg, clock, passes)
    scanned: list[str] = []

    def fake_scan_symbol(sym, df=None):
        scanned.append(sym)
        sc.request_stop("received SIGTERM")   # a cancel lands mid-pass
        return 0

    sc.scan_symbol = fake_scan_symbol
    del sc.scan_once                      # exercise the real scan_once loop
    try:
        sc.scan_once()
    finally:
        _restore_scanner()
    assert scanned == ["A.NS"], f"kept scanning after a stop request: {scanned}"
    print(f"ok test_scan_once_stops_between_symbols (stopped after {len(scanned)}/3 symbols)")


ALL = [
    test_live_alert_long_lived_zone,
    test_run_forever_covers_the_session,
    test_stop_signal_interrupts_the_poll_nap,
    test_stop_signal_is_honoured_even_when_sigint_was_ignored,
    test_max_runtime_minutes_stops_the_poller,
    test_stop_saves_dedup_state,
    test_scan_once_stops_between_symbols,
    test_live_alert_confirmed_after_bar_close,
    test_alert_dedup_across_passes,
    test_skips_stale_and_short_history,
    test_no_alerts_for_previous_session_bars,
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
        finally:
            _restore_scanner()
    if failed:
        sys.exit(1)
    print(f"\nAll {len(ALL)} live-scanner tests passed.")


if __name__ == "__main__":
    main()
