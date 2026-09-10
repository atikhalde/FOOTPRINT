"""Scenario tests for the FPFSSL8.2 engine port.

Each test builds a hand-crafted daily OHLCV sequence so that exactly one
feature of the state machine is exercised, and asserts on the emitted events.
Run:  .venv/bin/python tests/test_engine.py     (or via pytest)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from fpfssl.config import EngineConfig
from fpfssl.engine import (
    CLUSTERED,
    SWEEP,
    Engine,
    Event,
)
from fpfssl.events import (
    K_DEFENCE,
    K_ESSL_BREAK,
    K_ESSL_SWEEP,
    K_ESSL_TAP,
    K_FOOTPRINT,
    K_SSL_CREATED,
    K_TAP,
    K_ZONE_INVALID,
)

TICK = 0.01


def make_df(rows: list[tuple], start: str = "2024-01-01") -> pd.DataFrame:
    """rows: (open, high, low, close, volume) tuples, one per daily bar."""
    idx = pd.date_range(start=start, periods=len(rows), freq="B")
    df = pd.DataFrame(
        rows, index=idx, columns=["open", "high", "low", "close", "volume"]
    ).astype(float)
    df.index.name = "date"
    return df


def run(rows: list[tuple], live_last: bool = False):
    cfg = EngineConfig()
    df = make_df(rows)
    return Engine("TEST", cfg, TICK).run(df, live_last_bar=live_last)


def evs(res, kind) -> list[Event]:
    return [e for e in res.events if e.kind == kind]


def flat(n: int, o=99.5, c=100.5, h=100.7, l=99.3, v=1000.0) -> list[tuple]:
    """Alternating up/down candles keeping a steady ~1.4 ATR range."""
    out = []
    for i in range(n):
        if i % 2 == 0:
            out.append((o, h, l, c, v))
        else:
            out.append((c, h, l, o, v))
    return out


# ---------------------------------------------------------------------------
# Test 2: full chain — evidence -> departure -> displacement -> break -> OB
#          -> TAP 1 (with first-tap adjustment) -> DEFENCE -> stop invalidation
# ---------------------------------------------------------------------------
def full_chain_rows() -> list[tuple]:
    rows = flat(50)
    rows.append((99.9, 100.4, 99.3, 100.3, 3000.0))   # 50: evidence bar (3x RVOL near support)
    rows.append((100.4, 100.8, 100.2, 100.6, 1500.0)) # 51: departure (freezes bearish bar 49)
    rows.append((100.5, 101.7, 100.4, 101.6, 1500.0)) # 52: displacement + break -> OB created
    rows.append((101.5, 101.9, 101.4, 101.8, 1500.0)) # 53
    rows.append((101.8, 102.1, 101.6, 102.0, 1500.0)) # 54
    rows.append((102.0, 102.05, 100.20, 101.9, 1500.0))  # 55: TAP 1 (low <= ref)
    rows.append((101.9, 102.7, 101.8, 102.6, 2600.0)) # 56: DEFENCE
    rows.append((99.0, 99.2, 98.8, 98.9, 2000.0))     # 57: close < stop -> invalid
    return rows


def test_full_chain():
    res = run(full_chain_rows())
    n = len(res.dates)
    assert n == 58

    # evidence observed at bar 50
    assert res.counters["footprints_observed"] >= 1, res.counters

    # exactly one confirmed OB, at bar 52
    fps = evs(res, K_FOOTPRINT)
    assert len(fps) == 1, [ (e.bar, e.extra) for e in fps ]
    fp = fps[0]
    assert fp.bar == 52
    assert abs(fp.extra["top"] - 100.0) < 0.01, fp.extra
    assert abs(fp.extra["bottom"] - 99.3) < 0.01, fp.extra
    assert fp.extra["reference"] > 100.0, fp.extra
    assert fp.extra["invalidation"] < 99.3, fp.extra

    # TAP 1 at bar 55 with first-tap adjustment
    taps = evs(res, K_TAP)
    assert len(taps) == 1, [e.bar for e in taps]
    tap = taps[0]
    assert tap.bar == 55
    assert tap.extra["taps"] == 1
    assert tap.extra["adjusted"] is True
    assert abs(tap.extra["reference"] - 100.24) < 0.06, tap.extra
    assert tap.extra["new_reference"] > tap.extra["reference"], tap.extra
    assert tap.zone_id == fp.zone_id

    # DEFENCE at bar 56
    defs = evs(res, K_DEFENCE)
    assert len(defs) == 1, [e.bar for e in defs]
    assert defs[0].bar == 56
    assert defs[0].extra["rvol_def"] >= 1.3

    # invalidation at bar 57 (close 98.9 < stop ~99.10)
    inv = evs(res, K_ZONE_INVALID)
    assert len(inv) == 1, [ (e.bar, e.extra.get("reason")) for e in inv ]
    assert inv[0].bar == 57
    assert "stop" in inv[0].extra["reason"]

    # final zone state
    z = res.zones[0]
    assert z.source_state == -1 and not z.active
    assert z.source_taps == 1
    assert z.source_adjustment_bar == 55
    print("ok test_full_chain")


# ---------------------------------------------------------------------------
# Test 3: eSSL pool creation + touch tap + SWEEP (penetration + reclaim)
# ---------------------------------------------------------------------------
def essl_v_shape_rows() -> list[tuple]:
    rows = flat(20)  # 0..19 flat around 100 (highs 100.7 -> early major high pivot)
    # descent 20..25, low of 25 = 96.00 (major low, confirmed at bar 35)
    rows += [
        (99.7, 99.9, 98.8, 98.9, 1200.0),
        (98.9, 99.1, 98.0, 98.1, 1200.0),
        (98.1, 98.3, 97.4, 97.5, 1200.0),
        (97.5, 97.7, 96.8, 96.9, 1200.0),
        (96.9, 97.1, 96.2, 96.3, 1200.0),
        (96.3, 96.6, 96.0, 96.1, 2500.0),
    ]
    # rally 26..39
    rows += [
        (96.1, 96.9, 96.2, 96.8, 1500.0),
        (96.8, 97.6, 96.7, 97.5, 1500.0),
        (97.5, 98.3, 97.4, 98.2, 1400.0),
        (98.2, 99.0, 98.1, 98.9, 1400.0),
        (98.9, 99.8, 98.8, 99.7, 1400.0),
        (99.7, 100.5, 99.6, 100.4, 1400.0),
        (100.4, 101.2, 100.3, 101.1, 1300.0),
        (101.1, 101.9, 101.0, 101.8, 1300.0),
        (101.8, 102.6, 101.7, 102.5, 1300.0),
        (102.5, 103.3, 102.4, 103.2, 1200.0),
        (103.2, 104.0, 103.1, 103.9, 1200.0),
        (103.9, 104.7, 103.8, 104.6, 1200.0),
        (104.6, 105.4, 104.5, 105.3, 1200.0),
        (105.3, 106.0, 105.2, 105.9, 1100.0),
    ]
    # bar 40 = top (106.5), consolidation 41..50 keeps highs < 106.5
    rows += [(105.9, 106.5, 105.8, 106.3, 1100.0)]
    cons = [
        (106.3, 106.4, 105.6, 105.7, 1100.0),
        (105.7, 106.3, 105.5, 106.2, 1100.0),
        (106.2, 106.4, 105.7, 105.8, 1100.0),
        (105.8, 106.3, 105.6, 106.1, 1100.0),
        (106.1, 106.4, 105.8, 105.9, 1100.0),
        (105.9, 106.2, 105.5, 105.6, 1100.0),
        (105.6, 106.1, 105.4, 106.0, 1100.0),
        (106.0, 106.3, 105.6, 105.7, 1100.0),
        (105.7, 106.2, 105.5, 105.9, 1100.0),
        (105.9, 106.4, 105.6, 106.1, 1100.0),
    ]
    rows += cons
    # 51..59 drift (lows well above 96)
    rows += [
        (106.1, 106.3, 105.5, 105.8, 1100.0),
        (105.8, 106.0, 105.2, 105.5, 1100.0),
        (105.5, 105.8, 105.0, 105.6, 1100.0),
        (105.6, 105.9, 105.1, 105.3, 1100.0),
        (105.3, 105.7, 104.9, 105.5, 1100.0),
        (105.5, 105.8, 105.0, 105.2, 1100.0),
        (105.2, 105.6, 104.8, 105.4, 1100.0),
        (105.4, 105.7, 104.9, 105.1, 1100.0),
        (105.1, 105.5, 104.7, 105.3, 1100.0),
    ]
    return rows  # 60 bars total (0..59)


def test_essl_pool_touch_and_sweep():
    rows = essl_v_shape_rows()
    # touch: low exactly at the eSSL level (96.00), close back above
    rows.append((101.0, 101.2, 96.00, 97.50, 2000.0))   # bar 60: TOUCH
    rows.append((97.5, 98.4, 97.4, 98.3, 1500.0))       # bar 61
    rows += [
        (98.3, 99.0, 98.1, 98.9, 1400.0),               # 62
        (98.9, 99.5, 98.7, 99.4, 1400.0),               # 63
        (99.4, 100.0, 99.2, 99.9, 1300.0),              # 64
        (99.9, 100.4, 99.7, 100.3, 1300.0),             # 65
        (100.3, 100.8, 100.1, 100.7, 1200.0),           # 66
        (100.7, 101.2, 100.5, 101.1, 1200.0),           # 67
        (101.1, 101.5, 100.8, 101.3, 1200.0),           # 68
        (101.3, 101.7, 101.0, 101.5, 1200.0),           # 69
    ]
    # sweep: penetration >= 1 tick below level, close back above
    rows.append((98.0, 98.2, 95.85, 96.50, 2500.0))     # bar 70: SWEEP
    res = run(rows)

    created = [e for e in evs(res, K_SSL_CREATED) if e.extra.get("members") is not None]
    ext_created = [e for e in created if e.price == 96.0]
    assert ext_created, "eSSL pool at 96.0 was never published"
    first = ext_created[0]
    assert first.bar == 35, (first.bar, first.extra)
    pool_id = first.pool_id
    assert first.extra["members"] == 1

    # FRESH registry: origin 25 was first seen as a minor low (bar 28); the
    # later major recognition of the SAME origin only flips major_seen
    # (Pine: "major recognition of the same origin does not invent a new low")
    refs = [r for r in res.fresh_refs if abs(r.price - 96.0) < 1e-9]
    assert refs, "96.0 never entered the FRESH registry"
    assert refs[0].major_seen is True
    assert refs[0].active is False and refs[0].breached_bar == 70

    # touch at bar 60
    taps = [e for e in evs(res, K_ESSL_TAP) if e.pool_id == pool_id]
    assert any(e.bar == 60 for e in taps), [(e.bar, e.extra.get("classification")) for e in taps]
    touch = next(e for e in taps if e.bar == 60)
    assert touch.extra["penetrated"] is False
    assert touch.extra["reclaimed"] is True

    # sweep at bar 70
    sweeps = [e for e in evs(res, K_ESSL_SWEEP) if e.pool_id == pool_id]
    assert len(sweeps) == 1, [(e.bar, e.extra) for e in sweeps]
    assert sweeps[0].bar == 70
    assert sweeps[0].extra["reclaimed"] is True
    assert abs(sweeps[0].extra["depth"] - 0.15) < 1e-6

    p = next(p for p in res.pools if p.id == pool_id)
    assert not p.active and p.state == SWEEP
    print("ok test_essl_pool_touch_and_sweep")


def test_essl_break():
    rows = essl_v_shape_rows()
    rows.append((101.0, 101.2, 96.00, 97.50, 2000.0))   # bar 60: TOUCH
    rows.append((97.5, 98.4, 97.4, 98.3, 1500.0))       # bar 61
    # bar 62: penetration with close STAYS below the level -> break
    rows.append((97.0, 96.8, 95.80, 95.95, 2500.0))
    res = run(rows)

    created = [e for e in evs(res, K_SSL_CREATED) if e.price == 96.0]
    assert created
    pool_id = created[0].pool_id
    breaks = [e for e in evs(res, K_ESSL_BREAK) if e.pool_id == pool_id]
    assert len(breaks) == 1, [(e.bar, e.extra) for e in breaks]
    assert breaks[0].bar == 62
    assert breaks[0].extra["reclaimed"] is False
    p = next(p for p in res.pools if p.id == pool_id)
    assert not p.active
    print("ok test_essl_break")


# ---------------------------------------------------------------------------
# Test 5: EQL clustering — equal major lows merge into a 2-member band
# ---------------------------------------------------------------------------
def test_essl_eql_clustering():
    rows = flat(20)
    rows += [
        (99.7, 99.9, 98.8, 98.9, 1200.0),   # 20
        (98.9, 99.1, 98.0, 98.1, 1200.0),   # 21
        (98.1, 98.3, 97.4, 97.5, 1200.0),   # 22
        (97.5, 97.7, 96.8, 96.9, 1200.0),   # 23
        (96.9, 97.1, 96.2, 96.3, 1200.0),   # 24
        (96.3, 96.6, 96.0, 96.1, 2500.0),   # 25  major low #1 = 96.00
        (96.1, 96.9, 96.2, 96.8, 1500.0),   # 26
        (96.8, 97.6, 96.7, 97.5, 1500.0),   # 27
        (97.5, 98.3, 97.4, 98.2, 1400.0),   # 28
        (98.2, 99.0, 98.1, 98.9, 1400.0),   # 29
        (98.9, 99.8, 98.8, 99.7, 1400.0),   # 30
        (99.7, 100.5, 99.6, 100.4, 1400.0), # 31
        (100.4, 101.2, 100.3, 101.1, 1300.0),  # 32
        (100.5, 100.7, 96.0, 100.4, 2000.0),   # 33  EQUAL low retest = 96.00
        (100.4, 101.2, 100.3, 101.1, 1300.0),  # 34
        (101.1, 101.9, 101.0, 101.8, 1300.0),  # 35
        (101.8, 102.6, 101.7, 102.5, 1300.0),  # 36
        (102.5, 103.3, 102.4, 103.2, 1200.0),  # 37
        (103.2, 104.0, 103.1, 103.9, 1200.0),  # 38
        (103.9, 104.7, 103.8, 104.6, 1200.0),  # 39
        (104.6, 106.5, 104.5, 106.3, 1100.0),  # 40  major high origin
    ]
    rows += [
        (106.3, 106.4, 105.6, 105.7, 1100.0),
        (105.7, 106.3, 105.5, 106.2, 1100.0),
        (106.2, 106.4, 105.7, 105.8, 1100.0),
        (105.8, 106.3, 105.6, 106.1, 1100.0),
        (106.1, 106.4, 105.8, 105.9, 1100.0),
        (105.9, 106.2, 105.5, 105.6, 1100.0),
        (105.6, 106.1, 105.4, 106.0, 1100.0),
        (106.0, 106.3, 105.6, 105.7, 1100.0),
        (105.7, 106.2, 105.5, 105.9, 1100.0),
        (105.9, 106.4, 105.6, 106.1, 1100.0),
    ]  # 41..50
    res = run(rows)

    created = [e for e in evs(res, K_SSL_CREATED) if e.price == 96.0]
    # first pool at bar 35 (major low confirmed), EQL-merged pool at bar 43
    assert any(e.bar == 35 and e.extra["members"] == 1 for e in created), \
        [(e.bar, e.extra.get("members")) for e in created]
    assert any(e.bar == 43 and e.extra["members"] == 2 for e in created), \
        [(e.bar, e.extra.get("members")) for e in created]
    old = next(p for p in res.pools if p.id == created[0].pool_id)
    assert not old.active and old.terminal_state == CLUSTERED
    new = next(p for p in res.pools if p.members == 2)
    assert new.active and new.first_origin == 25
    print("ok test_essl_eql_clustering")


# ---------------------------------------------------------------------------
# Test 6: synthetic integration — event structure invariants
# ---------------------------------------------------------------------------
def test_synthetic_invariants():
    from fpfssl.config import DataConfig
    from fpfssl.synthetic import generate

    cfg = DataConfig(source="synthetic", history_bars=800)
    for sym in ("AAA", "BBB", "CCC"):
        df = generate(sym, cfg)
        res = Engine(sym, EngineConfig(), TICK).run(df, live_last_bar=False)
        for e in res.events:
            if e.kind == K_TAP:
                assert e.zone_id and e.extra["taps"] >= 1
                assert e.extra["reference"] >= e.extra["bottom"] - 1e-9
            if e.kind == K_ESSL_SWEEP:
                assert e.extra["reclaimed"] is True
            if e.kind == K_ESSL_BREAK:
                assert e.extra["reclaimed"] is False
        for z in res.zones:
            assert z.top > z.bottom
            assert z.invalidation < z.bottom
            assert z.source_initial_reference > z.bottom
        for p in res.pools:
            assert p.lower <= p.upper
        assert res.counters["footprints_observed"] >= 0
        assert res.counters["essl_taps"] >= res.counters["essl_sweeps"]
    print("ok test_synthetic_invariants")


# ---------------------------------------------------------------------------
# Test 7: the composite ALL-RULES condition — one bar carries BOTH an eSSL
# tap/sweep and a footprint-source TAP (the primary live alert).
# Timeline: V-bottom (major low 96.0 -> eSSL pool at bar 35), rally, evidence
# at 54, OB created at 55 (zone 103.4-104.2, ref ~104.38), then bar 60
# wicks through 95.90 (eSSL penetration + reclaim = SWEEP) while also
# satisfying the footprint TAP reference condition.
# ---------------------------------------------------------------------------
def composite_rows() -> list[tuple]:
    rows = flat(20)                                   # 0..19
    rows += [                                         # 20..25 descent, low 96.0 @25
        (99.7, 99.9, 98.8, 98.9, 1200.0),
        (98.9, 99.1, 98.0, 98.1, 1200.0),
        (98.1, 98.3, 97.4, 97.5, 1200.0),
        (97.5, 97.7, 96.8, 96.9, 1200.0),
        (96.9, 97.1, 96.2, 96.3, 1200.0),
        (96.3, 96.6, 96.0, 96.1, 2500.0),
    ]
    rows += [                                         # 26..34 rally
        (96.1, 96.9, 96.2, 96.8, 1500.0),
        (96.8, 97.6, 96.7, 97.5, 1500.0),
        (97.5, 98.3, 97.4, 98.2, 1400.0),
        (98.2, 99.0, 98.1, 98.9, 1400.0),
        (98.9, 99.8, 98.8, 99.7, 1400.0),
        (99.7, 100.5, 99.6, 100.4, 1400.0),
        (100.4, 101.2, 100.3, 101.1, 1300.0),
        (101.1, 101.9, 101.0, 101.8, 1300.0),
        (101.8, 102.6, 101.7, 102.5, 1300.0),
    ]
    rows += [                                         # 35..39
        (102.5, 103.3, 102.4, 103.2, 1200.0),
        (103.2, 104.0, 103.1, 103.9, 1200.0),
        (103.9, 104.7, 103.8, 104.6, 1200.0),
        (104.6, 105.4, 104.5, 105.3, 1200.0),
        (105.3, 106.0, 105.2, 105.9, 1100.0),
    ]
    rows += [(105.9, 106.5, 105.8, 106.3, 1100.0)]    # 40 top (major high origin)
    rows += [                                         # 41..49 consolidation
        (106.3, 106.4, 105.6, 105.7, 1100.0),
        (105.7, 106.3, 105.5, 106.2, 1100.0),
        (106.2, 106.4, 105.7, 105.8, 1100.0),
        (105.8, 106.3, 105.6, 106.1, 1100.0),
        (106.1, 106.4, 105.8, 105.9, 1100.0),
        (105.9, 106.2, 105.5, 105.6, 1100.0),
        (105.6, 106.1, 105.4, 106.0, 1100.0),
        (106.0, 106.3, 105.6, 105.7, 1100.0),
        (105.7, 106.2, 105.5, 105.9, 1100.0),
    ]
    rows += [(105.9, 106.4, 105.6, 106.1, 1100.0)]    # 50
    rows += [                                         # 51..53 pullback
        (106.1, 106.2, 105.3, 105.5, 1200.0),
        (105.5, 105.7, 104.6, 104.9, 1200.0),
        (104.9, 105.0, 103.4, 103.6, 1300.0),
    ]
    rows += [(103.7, 104.2, 103.4, 104.0, 3500.0)]    # 54 evidence (3x RVOL @ support)
    rows += [(104.1, 106.8, 104.0, 106.7, 1500.0)]    # 55 break+displacement -> OB
    rows += [                                         # 56..59 rally
        (106.7, 107.3, 106.5, 107.2, 1400.0),
        (107.2, 107.8, 107.0, 107.6, 1400.0),
        (107.6, 108.0, 107.3, 107.9, 1400.0),
        (107.9, 108.3, 107.6, 108.1, 1400.0),
    ]
    rows += [(108.0, 108.1, 95.90, 103.50, 4000.0)]   # 60 eSSL sweep + footprint TAP
    rows += [(103.5, 104.7, 103.4, 104.6, 3000.0)]    # 61
    return rows


def test_composite_all_rules():
    res = run(composite_rows())

    # eSSL pool at 96.0 published at bar 35
    created = [e for e in evs(res, K_SSL_CREATED) if e.price == 96.0]
    assert any(e.bar == 35 for e in created), [(e.bar, e.extra) for e in created]
    pool_id = next(e for e in created if e.bar == 35).pool_id

    # OB created at bar 55
    fps = evs(res, K_FOOTPRINT)
    assert any(e.bar == 55 for e in fps), [(e.bar, e.extra) for e in fps]
    fp = next(e for e in fps if e.bar == 55)
    assert abs(fp.extra["top"] - 104.2) < 0.01
    assert abs(fp.extra["bottom"] - 103.4) < 0.01

    # bar 60 carries BOTH a footprint TAP and an eSSL tap (with sweep)
    by_date: dict[str, dict] = {}
    for e in res.events:
        d = by_date.setdefault(e.date, {})
        if e.kind == K_TAP:
            d["tap"] = e
        if e.kind == K_ESSL_TAP:
            d["essl"] = e
        if e.kind == K_ESSL_SWEEP:
            d["sweep"] = e
    composites = [date for date, d in by_date.items() if d.get("tap") and d.get("essl")]
    assert len(composites) == 1, [(d, e.bar, e.kind) for d in by_date for e in by_date.values() if isinstance(e, Event)]
    bar60 = by_date[composites[0]]
    assert bar60["tap"].bar == 60
    assert bar60["tap"].zone_id == fp.zone_id
    assert bar60["tap"].extra["taps"] == 1
    assert bar60["essl"].pool_id == pool_id
    assert bar60["essl"].bar == 60
    assert "sweep" in bar60, "expected the eSSL tap bar to be a confirmed sweep+reclaim"
    assert bar60["sweep"].extra["reclaimed"] is True

    # zone survives the sweep bar (close 103.50 > stop ~103.25); a second
    # qualifying tap also fires on bar 61 (source state machine keeps counting)
    z = next(z for z in res.zones if z.id == fp.zone_id)
    assert z.active, z.terminal_reason
    assert z.source_taps >= 1
    print("ok test_composite_all_rules")


# ---------------------------------------------------------------------------
# Test 8: provisional (forming-bar) semantics — the live scanner path.
# The sweep+tap bar is the still-forming last bar: TAP + eSSL tap fire
# provisionally, the pool is NOT yet terminated (sweep classification waits
# for the close), and the scanner would tag the alert LIVE.
# ---------------------------------------------------------------------------
def test_provisional_forming_bar():
    rows = composite_rows()[:61]  # bar 60 (sweep+tap) is still forming
    res = Engine("TEST", EngineConfig(), TICK).run(make_df(rows), live_last_bar=True)
    taps = [e for e in res.events if e.kind == K_TAP]
    ettaps = [e for e in res.events if e.kind == K_ESSL_TAP]
    sweeps = [e for e in res.events if e.kind == K_ESSL_SWEEP]
    assert taps and taps[0].bar == 60 and taps[0].confirmed is False
    assert ettaps and ettaps[0].bar == 60 and ettaps[0].confirmed is False
    assert ettaps[0].extra["penetrated"] is True
    assert ettaps[0].extra["reclaimed"] is True
    assert sweeps == [], "sweep may only be classified on the confirmed bar"
    z = res.zones[0]
    assert z.source_taps == 1 and z.source_state == 1 and z.active
    pool = next(p for p in res.pools if p.id == ettaps[0].pool_id)
    assert pool.active, "pool must survive the forming bar (termination waits for close)"
    print("ok test_provisional_forming_bar")


# ---------------------------------------------------------------------------
# Test 8: repeated (clustered) evidence — Pine widens the base to the union of
# ALL intervening candles and moves evidenceStart to the OLDEST matched bar.
# ---------------------------------------------------------------------------
def test_repeated_evidence_union_and_start():
    from fpfssl.config import DataConfig
    from fpfssl.synthetic import generate

    cfg = DataConfig(source="synthetic", history_bars=800)
    seen = 0
    for sym in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"):
        df = generate(sym, cfg)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        res = Engine(sym, EngineConfig(), TICK).run(df, live_last_bar=False)
        for s in res.setups:
            if "Repeated" not in s.rule:
                continue
            seen += 1
            # oldest matched bar is strictly before the bar that closed the cluster
            assert s.start_bar < s.known_bar, (s.start_bar, s.known_bar)
            assert s.start_bar >= s.known_bar - 4  # evidenceWindow = 5
            # union semantics: base spans at least the current bar's extremes
            assert s.top >= round(h[s.known_bar], 2) - 1e-9, (s.top, h[s.known_bar])
            assert s.bottom <= round(l[s.known_bar], 2) + 1e-9, (s.bottom, l[s.known_bar])
            assert s.top - s.bottom > 0
    assert seen >= 3, f"expected repeated-evidence setups on synthetic data, saw {seen}"
    print(f"ok test_repeated_evidence_union_and_start ({seen} repeated setups)")


ALL = [
    test_full_chain,
    test_essl_pool_touch_and_sweep,
    test_essl_break,
    test_essl_eql_clustering,
    test_synthetic_invariants,
    test_composite_all_rules,
    test_provisional_forming_bar,
    test_repeated_evidence_union_and_start,
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
    print(f"\nAll {len(ALL)} tests passed.")


if __name__ == "__main__":
    main()
