"""eSSL LEVEL TOUCH alert tests (offline).

Covers the request *"alert should also come when price touches the eSSL level
(not fresh eSSL) — keep all others intact"*:

  1. price touches an eSSL level and NO footprint TAP fires that bar -> 💧 alert
  2. the level does NOT have to be fresh — a 191-bar-old level still alerts
  3. a composite rejected by tap_first_only / fresh_ob_only still alerts its
     eSSL touch (it used to go silent together with the composite)
  4. when the composite DOES fire, the bare touch alert is not duplicated for
     the level the composite already reported, and repeat passes stay silent
  5. two eSSL levels touched on one bar -> two alerts (per-level cooldown)
  6. the TAP filters never gate the touch alert, and muting `essl_tap` still works
  7. a touch on the forming bar alerts LIVE and gets its confirmed follow-up
  8. engine: an old (non-fresh) level emits its tap on the forming bar and on
     the confirmed bar alike; `essl_tap_max_age` gates both paths identically
  9. config.yaml and the dataclass defaults ship `essl_tap` (and the eSSL
     reclaim family: `essl_sweep` + `essl_reclaim`) enabled

Run:  .venv/bin/python tests/test_essl_touch_alerts.py     (or via pytest)
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from fpfssl.config import AppConfig, DataConfig, EngineConfig, load_config
from fpfssl.engine import Engine
from fpfssl.events import K_ESSL_TAP, K_TAP

import fpfssl.scanner as scanner

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from test_live_scanner import composite_bars, gen_intraday  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYM = "RELIANCE.NS"
_ORIG_LOAD = scanner.load_symbol
_ORIG_NOW = scanner.market_now

# Fixture bars (see gen_intraday): bar 825 is the only composite bar and its
# OB is 685 bars old, bar 86 touches one eSSL level, bar 84 touches two, and
# bar 788 touches a level that is 191 bars old.
BAR_TOUCH_ONLY = 86
BAR_TWO_LEVELS = 84
BAR_OLD_LEVEL = 788

SHIPPED = ["essl_ob_tap", "essl_tap", "footprint_tap"]
SHIPPED_FILTERS = dict(tap_first_only=True, fresh_ob_only=True,
                       fresh_ob_max_age_bars=50)


def _restore():
    scanner.load_symbol = _ORIG_LOAD
    scanner.market_now = _ORIG_NOW


class Recorder:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True

    @property
    def kinds(self) -> list[str]:
        out = []
        for m in self.messages:
            if "ALL RULES MATCH" in m:
                out.append("essl_ob_tap")
            elif "price touched the eSSL level" in m:
                out.append("essl_tap")
            elif "Footprint TAP" in m:
                out.append("footprint_tap")
            else:
                out.append("other")
        return out

    @property
    def touch_messages(self) -> list[str]:
        return [m for m in self.messages if "price touched the eSSL level" in m]


def mk_cfg(**kw) -> AppConfig:
    cfg = AppConfig()
    cfg.symbols = [SYM]
    cfg.data = DataConfig(source="yahoo", interval="15m", history_bars=500)
    cfg.scanner.min_bars = 10
    cfg.scanner.recent_bars = 1                 # isolate the bar under test
    cfg.scanner.alert_events = list(SHIPPED)
    cfg.scanner.alert_cooldown_minutes = 60     # the shipped spam guard
    cfg.scanner.state_file = os.path.join(
        tempfile.mkdtemp(prefix="fpfssl-essltouch-"), "scanner_state.json")
    for k, v in kw.items():
        setattr(cfg.scanner, k, v)
    return cfg


def scan_bar(df: pd.DataFrame, end_bar: int, cfg: AppConfig | None = None):
    """One scan_symbol pass whose last bar is df.iloc[end_bar] (bar forming)."""
    _restore()
    cfg = cfg or mk_cfg(**SHIPPED_FILTERS)
    frame = df.iloc[: end_bar + 1]
    now = (df.index[end_bar] + timedelta(minutes=5)).to_pydatetime()
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: now
    try:
        sent = sc.scan_symbol(SYM)
    finally:
        _restore()
    return sc, rec, sent


def engine_events(df: pd.DataFrame) -> list:
    return Engine(SYM, EngineConfig(), 0.05, tf="15m").run(df, live_last_bar=False).events


def essl_touch_events(df: pd.DataFrame, bar: int) -> list:
    return [e for e in engine_events(df) if e.kind == K_ESSL_TAP and e.bar == bar]


def tap_events(df: pd.DataFrame, bar: int) -> list:
    return [e for e in engine_events(df) if e.kind == K_TAP and e.bar == bar]


# ---------------------------------------------------------------------------
# 1. A touch with no footprint TAP on the bar alerts
# ---------------------------------------------------------------------------
def test_touch_without_footprint_tap_alerts():
    df = gen_intraday()
    evs = essl_touch_events(df, BAR_TOUCH_ONLY)
    assert len(evs) == 1, [e.pool_id for e in evs]
    assert not tap_events(df, BAR_TOUCH_ONLY), \
        "fixture changed: this bar now also carries a footprint TAP"
    sc, rec, sent = scan_bar(df, BAR_TOUCH_ONLY)
    assert rec.kinds == ["essl_tap"], rec.kinds
    msg = rec.touch_messages[0]
    assert f"eSSL level <b>{evs[0].price:g}</b>" in msg, msg
    assert "no footprint TAP required" in msg, msg
    print(f"ok test_touch_without_footprint_tap_alerts (level {evs[0].price:g}, {sent} alert)")


# ---------------------------------------------------------------------------
# 2. The eSSL level does NOT have to be fresh
# ---------------------------------------------------------------------------
def test_old_essl_level_touch_alerts():
    df = gen_intraday()
    evs = essl_touch_events(df, BAR_OLD_LEVEL)
    assert len(evs) == 1, [e.pool_id for e in evs]
    age = int(evs[0].extra["age_bars"])
    assert age > 100, f"fixture changed: level age is only {age} bars"
    assert age > SHIPPED_FILTERS["fresh_ob_max_age_bars"], age
    # the strictest TAP filters money can buy: the touch still alerts
    strict = dict(SHIPPED_FILTERS, fresh_ob_max_age_bars=0)
    sc, rec, sent = scan_bar(df, BAR_OLD_LEVEL, mk_cfg(**strict))
    assert rec.kinds == ["essl_tap"], rec.kinds
    assert f"age {age} bars" in rec.touch_messages[0], rec.touch_messages[0]
    print(f"ok test_old_essl_level_touch_alerts (level {evs[0].price:g} is {age} bars old)")


# ---------------------------------------------------------------------------
# 3. A filtered composite must NOT swallow the eSSL touch
# ---------------------------------------------------------------------------
def test_filtered_composite_still_alerts_the_touch():
    df = gen_intraday()
    k = composite_bars(df)[0]
    res = Engine(SYM, EngineConfig(), 0.05, tf="15m").run(df, live_last_bar=False)
    zborn = {z.id: z.born_bar for z in res.zones}
    tap = [e for e in res.events if e.kind == K_TAP and e.bar == k][0]
    ob_age = k - zborn[tap.zone_id]
    assert ob_age > SHIPPED_FILTERS["fresh_ob_max_age_bars"], ob_age

    sc, rec, sent = scan_bar(df, k)                    # shipped filters: composite dies
    assert "essl_ob_tap" not in rec.kinds, rec.kinds
    assert rec.kinds == ["essl_tap"], rec.kinds
    msg = rec.touch_messages[0]
    assert "was filtered" in msg and "fresh window 50" in msg, msg
    print(f"ok test_filtered_composite_still_alerts_the_touch "
          f"(OB #{tap.zone_id} age {ob_age} > 50, touch still alerted)")


# ---------------------------------------------------------------------------
# 4. A composite that DOES fire is not duplicated by the bare touch alert
# ---------------------------------------------------------------------------
def test_composite_suppresses_duplicate_touch_alert():
    df = gen_intraday()
    k = composite_bars(df)[0]
    cfg = mk_cfg(tap_first_only=False, fresh_ob_only=False)
    _restore()
    frame = df.iloc[: k + 1]
    now = (df.index[k] + timedelta(minutes=5)).to_pydatetime()
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: now
    try:
        first = sc.scan_symbol(SYM)
        again = sc.scan_symbol(SYM)                    # dedup must hold as well
    finally:
        _restore()
    assert "essl_ob_tap" in rec.kinds, rec.kinds
    assert not rec.touch_messages, \
        "the level the composite already reported must not alert twice"
    assert again == 0, (first, again, rec.kinds)
    print(f"ok test_composite_suppresses_duplicate_touch_alert ({rec.kinds}, repeat={again})")


# ---------------------------------------------------------------------------
# 5. Two eSSL levels touched on one bar -> two alerts
# ---------------------------------------------------------------------------
def test_two_levels_touched_on_one_bar_both_alert():
    df = gen_intraday()
    evs = essl_touch_events(df, BAR_TWO_LEVELS)
    assert len(evs) == 2, [e.pool_id for e in evs]
    sc, rec, sent = scan_bar(df, BAR_TWO_LEVELS)
    assert rec.kinds == ["essl_tap", "essl_tap"], rec.kinds
    levels = sorted(f"{e.price:g}" for e in evs)
    for lvl in levels:
        assert any(f"eSSL level <b>{lvl}</b>" in m for m in rec.touch_messages), \
            (lvl, rec.touch_messages)
    # one cooldown per level, so neither can mask the other
    cds = sorted(k for k in sc.state["cooldown"] if k.startswith(f"{SYM}|essl_tap|"))
    assert len(cds) == 2, cds
    print(f"ok test_two_levels_touched_on_one_bar_both_alert ({', '.join(levels)})")


# ---------------------------------------------------------------------------
# 6. Muting `essl_tap` still silences the touch alert (opt-out intact)
# ---------------------------------------------------------------------------
def test_touch_alert_can_be_muted():
    df = gen_intraday()
    sc, rec, sent = scan_bar(
        df, BAR_TOUCH_ONLY,
        mk_cfg(alert_events=["essl_ob_tap", "footprint_tap"], **SHIPPED_FILTERS))
    assert sent == 0 and rec.messages == [], rec.messages
    print("ok test_touch_alert_can_be_muted")


# ---------------------------------------------------------------------------
# 6b. Forming bar alerts LIVE; the confirmed version has its own dedup key and
#     goes out as soon as the per-level cooldown allows (a 15m bar closes 15
#     min later, so the shipped 60-min guard normally still covers it — one
#     alert per touch, which is the point of the spam guard).
# ---------------------------------------------------------------------------
def _two_pass_touch(cfg: AppConfig, df: pd.DataFrame) -> tuple[int, int, Recorder]:
    _restore()
    frame = df.iloc[: BAR_TOUCH_ONLY + 1]
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: (df.index[BAR_TOUCH_ONLY]
                                    + timedelta(minutes=5)).to_pydatetime()
    try:
        live = sc.scan_symbol(SYM)                       # bar still forming
        scanner.market_now = lambda c: (df.index[BAR_TOUCH_ONLY]
                                        + timedelta(minutes=30)).to_pydatetime()
        closed = sc.scan_symbol(SYM)                     # bar final
    finally:
        _restore()
    return live, closed, rec


def test_touch_alert_live_then_confirmed():
    df = gen_intraday()
    # no cooldown -> the confirmed follow-up is a distinct alert
    live, closed, rec = _two_pass_touch(
        mk_cfg(alert_cooldown_minutes=0, **SHIPPED_FILTERS), df)
    assert (live, closed) == (1, 1), (live, closed, rec.kinds)
    assert rec.kinds == ["essl_tap", "essl_tap"], rec.kinds
    assert "LIVE (intraday bar" in rec.messages[0], rec.messages[0]
    assert "LIVE (intraday bar" not in rec.messages[1], rec.messages[1]
    # shipped 60-min guard -> still exactly one alert for that touch
    live2, closed2, rec2 = _two_pass_touch(mk_cfg(**SHIPPED_FILTERS), df)
    assert (live2, closed2) == (1, 0), (live2, closed2, rec2.kinds)
    print("ok test_touch_alert_live_then_confirmed "
          "(LIVE + confirmed follow-up; 1 alert per touch with the 60-min guard)")


# ---------------------------------------------------------------------------
# 7. Engine: old levels tap live and confirmed alike; one age rule for both
# ---------------------------------------------------------------------------
def test_engine_taps_old_level_live_and_confirmed():
    df = gen_intraday()
    frame = df.iloc[: BAR_OLD_LEVEL + 1]
    evs = essl_touch_events(df, BAR_OLD_LEVEL)
    pool_id, age = evs[0].pool_id, int(evs[0].extra["age_bars"])
    assert age > 100, age

    def taps(cfg: EngineConfig, live: bool) -> list:
        res = Engine(SYM, cfg, 0.05, tf="15m").run(frame, live_last_bar=live)
        return [e for e in res.events
                if e.kind == K_ESSL_TAP and e.bar == BAR_OLD_LEVEL
                and e.pool_id == pool_id]

    assert taps(EngineConfig(), live=False), "confirmed bar did not tap the old level"
    live_taps = taps(EngineConfig(), live=True)
    assert live_taps and not live_taps[0].confirmed, "forming bar did not tap the old level"
    # the single age rule applies to BOTH paths (no live-only freshness gate)
    young_only = EngineConfig(essl_tap_max_age=age - 1)
    assert not taps(young_only, live=False) and not taps(young_only, live=True), \
        "essl_tap_max_age must gate the confirmed and the forming bar identically"
    print(f"ok test_engine_taps_old_level_live_and_confirmed (pool #{pool_id}, age {age})")


# ---------------------------------------------------------------------------
# 8. The shipped configuration enables the touch alert
# ---------------------------------------------------------------------------
def test_shipped_config_enables_essl_tap():
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    for ev in ("essl_tap", "essl_ob_tap", "footprint_tap", "essl_sweep", "essl_reclaim"):
        assert ev in cfg.scanner.alert_events, cfg.scanner.alert_events
    assert "essl_tap" in AppConfig().scanner.alert_events
    assert "essl_reclaim" in AppConfig().scanner.alert_events
    print(f"ok test_shipped_config_enables_essl_tap ({cfg.scanner.alert_events})")


ALL = [
    test_touch_without_footprint_tap_alerts,
    test_old_essl_level_touch_alerts,
    test_filtered_composite_still_alerts_the_touch,
    test_composite_suppresses_duplicate_touch_alert,
    test_two_levels_touched_on_one_bar_both_alert,
    test_touch_alert_can_be_muted,
    test_touch_alert_live_then_confirmed,
    test_engine_taps_old_level_live_and_confirmed,
    test_shipped_config_enables_essl_tap,
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
    print(f"\nAll {len(ALL)} eSSL-touch alert tests passed.")


if __name__ == "__main__":
    main()
