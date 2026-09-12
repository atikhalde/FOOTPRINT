"""eSSL LEVEL TOUCH alert tests (offline).

Covers the request *"alert should also come when price touches the eSSL level
(not fresh eSSL) — keep all others intact"* and the FMGOETZE 2026-09-10/11
fix *"only the RECLAIMED touch is the correct eSSL level — the scanner must
exactly match the indicator"*:

  1. price touches an eSSL level and NO footprint TAP fires that bar -> 💧 alert
  2. the level does NOT have to be fresh — a ~200-bar-old level still alerts
  3. a composite rejected by tap_first_only / fresh_ob_only still alerts its
     eSSL touch (it used to go silent together with the composite)
  4. when the composite DOES fire, the bare touch alert is not duplicated for
     the level the composite already reported, and repeat passes stay silent
  5. two eSSL levels touched on one bar -> two alerts (per-level cooldown)
  6. the TAP filters never gate the touch alert, and muting `essl_tap` still works
  7. a touch on the forming bar alerts LIVE and gets its confirmed follow-up
  8. engine: an old (non-fresh) level emits its tap on the forming bar and on
     the confirmed bar alike; `essl_tap_max_age` gates both paths identically
  9. config.yaml and the dataclass defaults ship `essl_tap` enabled
 10. a bar that closes BELOW the level is a BREAK (the indicator retires the
     level at that close): it never alerts as a touch — the FMGOETZE 445.65
     "NOT reclaimed" alert cannot happen again
 11. the OTHER half of that incident: the sweep-and-reclaim of the still-active
     432.65 alerts as 💧 RECLAIMED ✅ on the forming bar AND as the
     close-confirmed alert (the 60-min per-level cooldown must not swallow it),
     while ⚠️ `essl_break` stays wired for the retired level

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
from fpfssl.events import K_ESSL_BREAK, K_ESSL_TAP, K_TAP

import fpfssl.scanner as scanner

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from test_live_scanner import composite_bars, gen_intraday  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYM = "RELIANCE.NS"
_ORIG_LOAD = scanner.load_symbol
_ORIG_NOW = scanner.market_now

# Fixture bars (seed 40, see gen_intraday): bar 634 is the only composite bar
# (footprint TAP + swept-and-RECLAIMED eSSL 264.8, OB 535 bars old), bar 302
# touches one eSSL level, bar 790 touches two, bar 327 sweeps a level that is
# 196 bars old, and bar 785 closes BELOW three eSSL levels — a break, which
# must never alert as a touch.
BAR_TOUCH_ONLY = 302
BAR_TWO_LEVELS = 790
BAR_OLD_LEVEL = 327
BAR_BREAK = 785

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
#     is the close-confirmed counterpart of THAT SAME bar, so it always goes
#     out — `provisional_alerts` promises "alert on the still-forming bar too,
#     then again when confirmed", and on a DAILY bar the confirming pass always
#     lands inside `alert_cooldown_minutes` of the last intraday poll (letting
#     the guard eat it is what silenced the FMGOETZE close alert). Every later
#     pass for the same bar stays silent, so a touch is still at most two
#     messages: the guess and the verdict, never a stream.
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
    # shipped 60-min guard -> the confirmed follow-up of the SAME bar still
    # arrives (it is a distinct fact, the close verdict, not a repeat), and
    # nothing else does: dedup keeps the pair at exactly two messages.
    live2, closed2, rec2 = _two_pass_touch(mk_cfg(**SHIPPED_FILTERS), df)
    assert (live2, closed2) == (1, 1), (live2, closed2, rec2.kinds)
    assert rec2.kinds == ["essl_tap", "essl_tap"], rec2.kinds
    assert "LIVE (intraday bar" in rec2.messages[0], rec2.messages[0]
    assert "LIVE (intraday bar" not in rec2.messages[1], rec2.messages[1]
    print("ok test_touch_alert_live_then_confirmed "
          "(LIVE + close-confirmed follow-up; exactly 2 messages per touch)")


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
    assert "essl_tap" in cfg.scanner.alert_events, cfg.scanner.alert_events
    assert "essl_ob_tap" in cfg.scanner.alert_events, cfg.scanner.alert_events
    assert "footprint_tap" in cfg.scanner.alert_events, cfg.scanner.alert_events
    assert "essl_tap" in AppConfig().scanner.alert_events
    print(f"ok test_shipped_config_enables_essl_tap ({cfg.scanner.alert_events})")


# ---------------------------------------------------------------------------
# 8b. THE FMGOETZE REGRESSION (2026-09-10): a bar that closes BELOW the level
# terminally retires it in the indicator (first full penetration, no reclaim).
# That bar is a BREAK — with the shipped config it must not reach the user as
# a 💧 "price touched the eSSL level" alert. (Enable `essl_break` in
# alert_events to get the honest ⚠️ break notice instead.)
# ---------------------------------------------------------------------------
def test_break_bar_never_alerts_a_touch():
    df = gen_intraday()
    evs = [e for e in engine_events(df) if e.kind == K_ESSL_BREAK and e.bar == BAR_BREAK]
    assert evs, "fixture changed: bar no longer breaks an eSSL level"
    assert all(e.extra.get("reclaimed") is False for e in evs)
    taps = essl_touch_events(df, BAR_BREAK)
    assert taps == [], "the engine tapped a bar that closed below the level"

    # LIVE side: bar 785 still forming, provisional close below the levels —
    # a mid-break bar waits for the close, nothing is sent
    sc, rec, sent = scan_bar(df, BAR_BREAK)
    assert not rec.touch_messages, rec.touch_messages
    assert sent == 0, rec.messages

    # confirmed side: bar 785 closed, then the next bar is forming. With the
    # shipped config (essl_break muted) the user hears nothing at all; opting
    # into `essl_break` reports the event honestly, as a ⚠️ BREAK — never as
    # a 💧 touch.
    cfg = mk_cfg(recent_bars=2, **SHIPPED_FILTERS)
    _, rec_conf, _ = scan_bar(df, BAR_BREAK + 1, cfg)
    assert not rec_conf.touch_messages, rec_conf.touch_messages
    assert rec_conf.messages == [], rec_conf.messages
    cfg_brk = mk_cfg(recent_bars=2, alert_events=["essl_break"], **SHIPPED_FILTERS)
    _, rec_brk, _ = scan_bar(df, BAR_BREAK + 1, cfg_brk)
    assert not rec_brk.touch_messages, rec_brk.touch_messages
    assert any("eSSL BREAK" in m for m in rec_brk.messages), rec_brk.messages
    assert not any("touched the eSSL level" in m for m in rec_brk.messages)
    print(f"ok test_break_bar_never_alerts_a_touch "
          f"({len(evs)} level(s) broken on bar {BAR_BREAK}, 0 touch alerts)")


# ---------------------------------------------------------------------------
# 8c. THE FMGOETZE 432.65 HALF OF THE INCIDENT (2026-09-11, the shipped DAILY
# setup). A still-active level that is swept and closed back above must reach
# the user as 💧 … RECLAIMED ✅ — both the provisional LIVE message and, at the
# bar close, the confirmed one. PR #11 made the mid-break forming bar silent and
# deferred the verdict to the close ("a reclaim then alerts via the confirmed
# sweep tap"), but the per-level `alert_cooldown_minutes` guard then ate exactly
# that confirmed follow-up: on a daily bar the confirming pass always runs within
# the cooldown window of the last intraday poll, so the close-confirmed alert
# the indicator actually shows never arrived.
# ---------------------------------------------------------------------------
DAILY_SYM = "FMGOETZE.NS"
ESSL_ACTIVE = 432.65      # origin 2026-07-08 in the incident — still ACTIVE
ESSL_BROKEN = 445.65      # origin 2026-08-17 — retired by the 09-10 close below
DAILY_END = pd.Timestamp("2026-09-11")     # the Friday of the incident
TICK_NS = 0.05


def fmgoetze_daily(last: tuple | None = None) -> pd.DataFrame:
    """63 daily bars replaying the incident at its real prices.

    bar 24 = 432.65 major low  -> eSSL published on bar 34 (stays active)
    bar 44 = 445.65 major low  -> eSSL published on bar 54
    bar 61 = 2026-09-10          low 441.30, close 443.55: full penetration of
                                 445.65 with the close BELOW it -> the indicator
                                 RETIRES that level (a break, never a touch)
    bar 62 = 2026-09-11          low 431.20, close 453.25: sweeps the still
                                 active 432.65 and closes back above it ->
                                 SWEEP + "RECLAIMED ✅" (the alert the user wants)
    `last` replaces the final bar, i.e. an intraday snapshot of that session.
    """
    rows: list[tuple] = []

    def add(o, h, l, c):
        rows.append((float(o), float(h), float(l), float(c), 1_500_000.0))

    for px in (452.0, 454.0, 456.0, 458.0, 460.0,               # 0..12 climb
               461.0, 462.0, 463.0, 464.0, 465.0,
               466.0, 467.0, 468.0):
        add(px, px + 1.5, px - 1.5, px + 1.0)
    add(469.0, 472.0, 468.0, 471.5)                             # 13 pivot high (472)
    add(470.0, 471.0, 468.0, 468.5)                             # 14
    for px in (464.0, 458.0, 452.0, 448.0, 444.0, 441.0, 438.5, 436.0):   # 15..22 fall
        add(px, px + 1.2, px - 1.8, px - 1.2)
    add(434.5, 435.5, 433.2, 433.6)                             # 23
    add(433.5, 436.0, ESSL_ACTIVE, 435.0)                       # 24: 432.65 pivot low
    for px in (436.0, 438.0, 440.5, 443.0, 446.0, 448.5, 450.0, 451.0, 452.0):
        add(px, px + 1.4, px - 1.0, px + 0.8)                   # 25..33 rally
    for px in (451.0, 450.0, 449.2, 448.6, 448.0, 447.6, 447.2, 447.0, 446.8):
        add(px, px + 1.2, px - 0.8, px - 0.4)                   # 34..42 pullback
    add(446.5, 447.0, 446.1, 446.3)                             # 43
    add(446.2, 447.2, ESSL_BROKEN, 446.0)                       # 44: 445.65 pivot low
    for px in (447.5, 448.5, 449.5, 450.5, 451.5, 452.5, 453.5, 454.5, 455.5, 456.0):
        add(px, px + 1.2, px - 0.6, px + 0.8)                   # 45..54 rally
    for px in (456.5, 455.5, 454.5, 453.5, 452.5, 451.5):       # 55..60 hover
        add(px, px + 1.2, px - 1.0, px + 0.4)
    add(450.0, 451.0, 441.30, 443.55)                           # 61 = 09-10 BREAK
    add(442.0, 454.00, 431.20, 453.25)                          # 62 = 09-11 SWEEP+RECLAIM
    if last is not None:
        rows[-1] = tuple(float(x) for x in last) + (1_500_000.0,)
    idx = pd.bdate_range(end=DAILY_END, periods=len(rows))
    df = pd.DataFrame(rows, index=idx,
                      columns=["open", "high", "low", "close", "volume"]).astype(float)
    df.index.name = "date"
    return df


def mk_daily_cfg(**kw) -> AppConfig:
    """The shipped daily setup (config.yaml: interval 1d, the three tap events,
    provisional alerts on, the 60-min per-level cooldown)."""
    cfg = AppConfig()
    cfg.symbols = [DAILY_SYM]
    cfg.data = DataConfig(source="yahoo", interval="1d", history_bars=600, max_bars=0)
    cfg.scanner.min_bars = 10
    cfg.scanner.recent_bars = 3
    cfg.scanner.provisional_alerts = True
    cfg.scanner.alert_cooldown_minutes = 60
    cfg.scanner.alert_events = list(SHIPPED)
    cfg.scanner.state_file = os.path.join(tempfile.mkdtemp(prefix="fpfssl-daily-"),
                                          "scanner_state.json")
    for k, v in kw.items():
        setattr(cfg.scanner, k, v)
    return cfg


def scan_daily(cfg: AppConfig, df: pd.DataFrame, hhmm: tuple[int, int], rec: Recorder) -> int:
    """One scanner pass with the market clock at hhmm IST on the last bar's day."""
    _restore()
    now = DAILY_END.replace(hour=hhmm[0], minute=hhmm[1])
    scanner.load_symbol = lambda sym, d: df
    scanner.market_now = lambda c: now
    try:
        return scanner.LiveScanner(cfg, rec, symbols=[DAILY_SYM]).scan_symbol(DAILY_SYM)
    finally:
        _restore()


def test_fmgoetze_reclaim_alerts_live_and_at_the_close():
    df = fmgoetze_daily()
    last = len(df) - 1

    # ---- engine: the two halves of the incident, one bar apart -------------
    evs = Engine(DAILY_SYM, EngineConfig(), TICK_NS, tf="1d").run(df, live_last_bar=False).events
    breaks = [e for e in evs if e.kind == K_ESSL_BREAK and e.bar == last - 1]
    assert [round(e.price, 2) for e in breaks] == [ESSL_BROKEN], \
        "the 09-10 bar must retire 445.65 as a break"
    assert [e for e in evs if e.kind == K_ESSL_TAP and e.bar == last - 1] == [], \
        "a level retired at this close is not a touch (the FMGOETZE 445.65 bug)"
    taps = [e for e in evs if e.kind == K_ESSL_TAP and e.bar == last]
    assert [round(e.price, 2) for e in taps] == [ESSL_ACTIVE], taps
    assert taps[0].extra["reclaimed"] is True and taps[0].extra["penetrated"] is True
    # the same level, 28 bars after it was published: old but still ACTIVE
    assert int(taps[0].extra["age_bars"]) > 20, taps[0].extra

    # ---- scanner: the CI poll sequence over that session -------------------
    cfg = mk_daily_cfg()
    rec = Recorder()
    # 15:20 — the bar has already pierced the level and the running close is
    # still under it: a mid-break forming bar waits for the close (PR #11).
    dip = fmgoetze_daily(last=(442.0, 443.0, 431.20, 432.00))
    assert scan_daily(cfg, dip, (15, 20), rec) == 0, rec.messages
    # 15:35 — price is back above 432.65: the LIVE tap goes out.
    assert scan_daily(cfg, df, (15, 35), rec) == 1, rec.messages
    live = rec.messages[-1]
    assert "eSSL level <b>432.65</b>" in live and "RECLAIMED ✅" in live, live
    assert "LIVE (intraday bar" in live, live
    assert "445.65" not in live, live
    # 16:20 — the bar is CLOSED. This is the alert that used to vanish: the
    # per-level 60-min cooldown was still open from the LIVE pass, so the
    # close-confirmed "RECLAIMED ✅" was silently dropped.
    n = len(rec.messages)
    assert scan_daily(cfg, df, (16, 20), rec) == 1, "confirmed close alert swallowed"
    conf = rec.messages[-1]
    assert "RECLAIMED ✅" in conf and "432.65" in conf, conf
    assert "LIVE (intraday bar" not in conf, conf
    assert len(rec.messages) == n + 1
    # 16:35 — a further poll repeats nothing: dedup is per bar+state+level, so
    # bypassing the cooldown cannot turn into a stream.
    assert scan_daily(cfg, df, (16, 35), rec) == 0, rec.messages
    assert len(rec.messages) == n + 1, rec.messages

    # a scanner that only ever sees the closed bar still alerts it
    rec2 = Recorder()
    assert scan_daily(mk_daily_cfg(), df, (16, 20), rec2) == 1, rec2.messages
    assert "RECLAIMED ✅" in rec2.messages[0], rec2.messages[0]

    # ---- the ⚠️ essl_break channel is wired in the scanner ------------------
    # (PR #12's merge replaced this mapping entry with `essl_reclaim` and the
    # documented break alert silently became impossible again.)
    rec3 = Recorder()
    assert scan_daily(mk_daily_cfg(alert_events=["essl_break"]), df, (16, 20), rec3) == 1
    assert "eSSL BREAK" in rec3.messages[0] and "445.65" in rec3.messages[0], rec3.messages
    print("ok test_fmgoetze_reclaim_alerts_live_and_at_the_close "
          "(LIVE tap + close-confirmed RECLAIMED ✅ on 432.65, 445.65 only as a break)")


# ---------------------------------------------------------------------------
# 12. The cooldown is a SPAM guard, not a recency filter: an OLDER bar in the
#     `recent_bars` window must never silence a NEWER touch of the same level.
#     This is the regression behind "the scanner stopped sending valid alerts"
#     on the shipped daily setup. `recent_bars: 3` admits several bars per pass
#     and they used to be walked oldest-first, so the first (oldest) touch
#     opened the 60-minute per-level guard and the newest touch — the only one
#     still actionable — was dropped with "inside the cooldown of the previous
#     alert". The dedup key carries the bar stamp, so nothing ever retried it
#     either: the newest bar's signal was simply lost for the day.
#     Fixture: eSSL 257.7 is touched on bar 1013 (12:30) AND bar 1014 (12:45)
#     of the same session, so both sit inside a 2-bar window.
# ---------------------------------------------------------------------------
BAR_REPEAT_A = 1013
BAR_REPEAT_B = 1014


def _repeat_level(df: pd.DataFrame) -> float:
    evs = essl_touch_events(df, BAR_REPEAT_A)
    assert len(evs) == 1, [e.pool_id for e in evs]
    again = essl_touch_events(df, BAR_REPEAT_B)
    assert len(again) == 1, [e.pool_id for e in again]
    assert evs[0].pool_id == again[0].pool_id, (evs[0].pool_id, again[0].pool_id)
    return float(evs[0].price)


def _two_pass_repeat(cfg: AppConfig, df: pd.DataFrame):
    """Pass 1 ends on the older touch, pass 2 on the newer one (same scanner)."""
    _restore()
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: df.iloc[: BAR_REPEAT_A + 1]
    scanner.market_now = lambda c: (df.index[BAR_REPEAT_A]
                                    + timedelta(minutes=5)).to_pydatetime()
    try:
        first = sc.scan_symbol(SYM)
        scanner.load_symbol = lambda sym, d: df.iloc[: BAR_REPEAT_B + 1]
        scanner.market_now = lambda c: (df.index[BAR_REPEAT_B]
                                        + timedelta(minutes=5)).to_pydatetime()
        second = sc.scan_symbol(SYM)
    finally:
        _restore()
    return first, second, sc, rec


def test_newer_bar_not_swallowed_by_older_bars_cooldown():
    df = gen_intraday()
    level = _repeat_level(df)
    stamp_a = df.index[BAR_REPEAT_A].strftime("%Y-%m-%d %H:%M")
    stamp_b = df.index[BAR_REPEAT_B].strftime("%Y-%m-%d %H:%M")
    first, second, sc, rec = _two_pass_repeat(mk_cfg(recent_bars=2, **SHIPPED_FILTERS), df)
    # pass 1: the older touch alerts and opens the per-level cooldown
    assert first == 1, (first, rec.kinds)
    # pass 2, minutes later: the window holds BOTH bars and the 60-min guard is
    # wide open. The NEWER bar is news, so its touch must still be delivered.
    assert second >= 1, (second, rec.kinds, sc.suppressed_examples)
    newest = [m for m in rec.messages if stamp_b in m and f"eSSL level <b>{level:g}</b>" in m]
    assert newest, (stamp_b, level, [m.splitlines()[1] for m in rec.messages])
    # the guard now records WHICH BAR opened it, which is what makes the
    # exemption above evidence-based rather than a blanket bypass
    pool = int(essl_touch_events(df, BAR_REPEAT_A)[0].pool_id)
    cd = sc.state["cooldown"][f"{SYM}|essl_tap|{pool}"]
    assert scanner.LiveScanner._cooldown_stamp(cd)[1] in (stamp_a, stamp_b), cd
    print("ok test_newer_bar_not_swallowed_by_older_bars_cooldown "
          "(level %g: %s alerts, then %d more; the %s touch arrived)"
          % (level, first, second, stamp_b[-5:]))


def test_cold_start_reports_the_newest_bar_not_the_stale_one():
    """One cold pass over a window holding two touches of one level.

    Both bars are new to the state file, so dedup cannot separate them — only
    the ordering + the bar stamp can. The newest bar must be the one delivered:
    before the fix the window was walked oldest-first, so a two-day-old touch
    opened the guard and today's touch was dropped as "inside the cooldown of
    the previous alert". That is the "not receiving a valid alert" symptom.
    """
    df = gen_intraday()
    level = _repeat_level(df)
    stamp_a = df.index[BAR_REPEAT_A].strftime("%Y-%m-%d %H:%M")
    stamp_b = df.index[BAR_REPEAT_B].strftime("%Y-%m-%d %H:%M")
    _restore()
    rec = Recorder()
    sc = scanner.LiveScanner(mk_cfg(recent_bars=2, **SHIPPED_FILTERS), rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: df.iloc[: BAR_REPEAT_B + 1]
    scanner.market_now = lambda c: (df.index[BAR_REPEAT_B]
                                    + timedelta(minutes=5)).to_pydatetime()
    try:
        sent = sc.scan_symbol(SYM)
    finally:
        _restore()
    assert sent == 1, (sent, rec.kinds)
    assert stamp_b in rec.messages[0] and stamp_a not in rec.messages[0], \
        [m.splitlines()[1] for m in rec.messages]
    assert sc.stats["suppressed_cooldown"] == 1, sc.stats
    print("ok test_cold_start_reports_the_newest_bar_not_the_stale_one "
          "(sent %s, suppressed the %s bar)" % (stamp_b[-5:], stamp_a[-5:]))


def test_same_bar_still_collapses_into_one_alert():
    """The guard still does its actual job: repeats of the SAME bar stay silent.

    Exempting newer bars must not switch the cooldown off. Re-running the very
    pass that just alerted has to produce nothing at all.
    """
    df = gen_intraday()
    _repeat_level(df)
    _restore()
    rec = Recorder()
    sc = scanner.LiveScanner(mk_cfg(recent_bars=2, **SHIPPED_FILTERS), rec, symbols=[SYM])
    scanner.load_symbol = lambda sym, d: df.iloc[: BAR_REPEAT_B + 1]
    scanner.market_now = lambda c: (df.index[BAR_REPEAT_B]
                                    + timedelta(minutes=5)).to_pydatetime()
    try:
        first = sc.scan_symbol(SYM)
        again = sc.scan_symbol(SYM)          # identical pass, nothing is newer
        and_again = sc.scan_symbol(SYM)
    finally:
        _restore()
    assert first == 1, (first, rec.kinds)
    assert (again, and_again) == (0, 0), (again, and_again, rec.kinds)
    assert len(rec.messages) == 1, rec.messages
    print("ok test_same_bar_still_collapses_into_one_alert "
          "(%d alert, then %d/%d on repeat passes)" % (first, again, and_again))


# ---------------------------------------------------------------------------
# 13. Bar-stamp comparison: the exemption is evidence-based, so an unknown
#     stamp (a state file written before it existed) keeps the old behaviour
#     instead of silently disabling the spam guard.
# ---------------------------------------------------------------------------
def test_bar_stamp_comparison_and_legacy_state():
    newer = scanner.LiveScanner._bar_is_newer
    assert newer("2026-09-11", "2026-09-09") is True
    assert newer("2026-09-09", "2026-09-11") is False
    assert newer("2026-09-09", "2026-09-09") is False          # same bar: guarded
    assert newer("2026-09-11 09:30", "2026-09-11 09:15") is True
    assert newer("2026-09-11", "") is False                    # unknown -> guarded
    assert newer("", "2026-09-09") is False
    # a pre-upgrade state file holds bare ISO strings; both readers agree
    ts, bar = scanner.LiveScanner._cooldown_stamp("2026-09-11T10:00:00")
    assert ts == "2026-09-11T10:00:00" and bar == "", (ts, bar)
    ts2, bar2 = scanner.LiveScanner._cooldown_stamp(
        {"ts": "2026-09-11T10:00:00", "bar": "2026-09-11"})
    assert (ts2, bar2) == ("2026-09-11T10:00:00", "2026-09-11"), (ts2, bar2)
    assert scanner.LiveScanner._cooldown_stamp(None) == ("", "")
    print("ok test_bar_stamp_comparison_and_legacy_state")


def test_legacy_cooldown_entry_still_suppresses():
    """A restored cache written before the bar stamp must still guard."""
    from datetime import datetime as _dt
    df = gen_intraday()
    pool = essl_touch_events(df, BAR_REPEAT_A)[0].pool_id
    cfg = mk_cfg(recent_bars=2, **SHIPPED_FILTERS)
    _restore()
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM])
    sc.state["cooldown"][f"{SYM}|essl_tap|{pool}"] = _dt.now().isoformat()  # legacy form
    scanner.load_symbol = lambda sym, d: df.iloc[: BAR_REPEAT_A + 1]
    scanner.market_now = lambda c: (df.index[BAR_REPEAT_A]
                                    + timedelta(minutes=5)).to_pydatetime()
    try:
        sent = sc.scan_symbol(SYM)
    finally:
        _restore()
    assert sent == 0, (sent, rec.messages)
    assert sc.stats["suppressed_cooldown"] == 1, sc.stats
    print("ok test_legacy_cooldown_entry_still_suppresses")


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
    test_break_bar_never_alerts_a_touch,
    test_fmgoetze_reclaim_alerts_live_and_at_the_close,
    test_newer_bar_not_swallowed_by_older_bars_cooldown,
    test_cold_start_reports_the_newest_bar_not_the_stale_one,
    test_same_bar_still_collapses_into_one_alert,
    test_bar_stamp_comparison_and_legacy_state,
    test_legacy_cooldown_entry_still_suppresses,
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
