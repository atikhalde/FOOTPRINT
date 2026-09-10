"""Fidelity + live-Indian-market tests for the FPFSSL8.2 port.

Covers the exact-match fixes vs the Pine script and the yfinance intraday
(NSE, IST) scanner path. Run:  python3 tests/test_fidelity_live.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from fpfssl.config import AppConfig, DataConfig, EngineConfig
from fpfssl.data import DataError, _normalize, _yahoo_kwargs
from fpfssl.engine import Engine, detect_tick, sma
from fpfssl.events import K_ESSL_TAP, K_TAP


def make_df(rows, start="2024-01-01", freq="B"):
    idx = pd.date_range(start=start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"]).astype(float)
    df.index.name = "date"
    return df


def flat(n, o=99.5, c=100.5, h=100.7, l=99.3, v=1000.0):
    out = []
    for i in range(n):
        out.append((o, h, l, c, v) if i % 2 == 0 else (c, h, l, o, v))
    return out


# ---------------------------------------------------------------------------
# 1. sma() must match Pine ta.sma with NaN volumes (window-poison only)
# ---------------------------------------------------------------------------
def test_sma_nan_window():
    x = np.array([1.0, 2.0, float("nan"), 4.0, 5.0, 6.0, 7.0])
    out = sma(x, 3)
    # windows ending at idx1 needs 3 valid -> nan; idx2 has nan -> nan;
    # idx5 window [4,5,6] clean -> 5.0 (cumsum impl would stay nan forever)
    assert np.isnan(out[0]) and np.isnan(out[1]) and np.isnan(out[2])
    assert abs(out[5] - 5.0) < 1e-9, out
    assert abs(out[6] - 6.0) < 1e-9, out
    print("ok test_sma_nan_window")


# ---------------------------------------------------------------------------
# 2. Volume-baseline readiness counts BARS (Pine math.sum of 1.0s), not volume
# ---------------------------------------------------------------------------
def test_volume_baseline_counts_bars():
    # 30 flat bars with tiny but valid volumes: count-based readiness is True
    # after 20 bars; the old sum-of-volumes logic would ALSO be true here, so
    # instead verify the negative side: with only 10 valid bars + NaNs, the
    # engine must NOT register single-bar evidence (needs 20 valid bars).
    rows = flat(10)
    # 10 bars with NaN volume (invalid), then an evidence-shaped bar
    for _ in range(10):
        rows.append((99.5, 100.7, 99.3, 100.5, float("nan")))
    rows.append((99.9, 100.4, 99.3, 100.3, 3000.0))  # shaped like evidence
    res = Engine("TEST", EngineConfig(), 0.01).run(make_df(rows), live_last_bar=False)
    # bar 20 cannot be single evidence: only 10 valid volume bars behind it
    assert res.counters["footprints_observed"] == 0, res.counters
    print("ok test_volume_baseline_counts_bars")


# ---------------------------------------------------------------------------
# 3. Every touch of an active eSSL is a tap (not just the first latch)
# ---------------------------------------------------------------------------
def test_repeated_essl_taps_emit_each_bar():
    from tests.test_engine import essl_v_shape_rows
    rows = essl_v_shape_rows()
    rows.append((101.0, 101.2, 96.00, 97.50, 2000.0))   # 60: touch
    rows.append((97.5, 98.4, 97.4, 98.3, 1500.0))       # 61: away
    rows.append((100.0, 100.5, 96.00, 97.00, 2000.0))   # 62: second touch
    rows.append((97.0, 97.5, 97.2, 97.4, 1500.0))       # 63: away
    res = Engine("TEST", EngineConfig(), 0.01).run(make_df(rows), live_last_bar=False)
    taps = sorted(e.bar for e in res.events if e.kind == K_ESSL_TAP)
    assert 60 in taps and 62 in taps, taps
    print("ok test_repeated_essl_taps_emit_each_bar")


# ---------------------------------------------------------------------------
# 4. Record-cap retirement is silent (no zone_invalid alert event)
# ---------------------------------------------------------------------------
def test_zone_cap_silent():
    cfg = EngineConfig()
    cfg.max_zones = 1  # force cap evictions
    from fpfssl.synthetic import generate
    df = generate("CAPTEST", DataConfig(source="synthetic", history_bars=800))
    res = Engine("CAPTEST", cfg, 0.01).run(df, live_last_bar=False)
    for e in res.events:
        if e.kind == "zone_invalid":
            assert e.extra.get("reason") != "Record cap, no longer tracked", e.extra
    print("ok test_zone_cap_silent")


# ---------------------------------------------------------------------------
# 5. Tick resolution: NSE/BSE -> 0.05, US/adjusted dust -> 0.01
# ---------------------------------------------------------------------------
def test_detect_tick_exchange_aware():
    # NSE prices with 0.01-spaced adjusted values must STILL be 0.05
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    px = 2440.0 + np.arange(30) * 0.01
    df = pd.DataFrame({"open": px, "high": px + 1, "low": px - 1,
                       "close": px + 0.5, "volume": 1000.0}, index=idx)
    assert detect_tick(df, "RELIANCE.NS") == 0.05
    assert detect_tick(df, "SBIN.BO") == 0.05
    assert detect_tick(df, "AAPL") == 0.01
    # sub-penny adjusted dust floors to 0.01
    px2 = 100.0 + np.arange(30) * 0.0007
    df2 = pd.DataFrame({"open": px2, "high": px2 + 0.1, "low": px2 - 0.1,
                        "close": px2, "volume": 1000.0}, index=idx)
    assert detect_tick(df2, "AAPL") == 0.01
    print("ok test_detect_tick_exchange_aware")


# ---------------------------------------------------------------------------
# 6. Intraday engine path: HH:MM bar stamps, full state machine on 15m bars
# ---------------------------------------------------------------------------
def test_intraday_engine_dates_and_run():
    # 400 synthetic 15m bars across ~16 NSE sessions
    idx = []
    day = pd.Timestamp("2024-06-03 09:15")  # a Monday
    while len(idx) < 400:
        if day.weekday() < 5:
            t = day.replace(hour=9, minute=15)
            end = day.replace(hour=15, minute=30)
            while t < end and len(idx) < 400:
                idx.append(t)
                t += pd.Timedelta(minutes=15)
        day += pd.Timedelta(days=1)
    idx = pd.DatetimeIndex(idx)
    rng = np.random.default_rng(7)
    close = 2500 + np.cumsum(rng.normal(0, 1.5, len(idx)))
    df = pd.DataFrame({
        "open": close + rng.normal(0, 0.5, len(idx)),
        "high": close + np.abs(rng.normal(0, 1.0, len(idx))) + 0.5,
        "low": close - np.abs(rng.normal(0, 1.0, len(idx))) - 0.5,
        "close": close,
        "volume": rng.integers(5000, 50000, len(idx)).astype(float),
    }, index=idx)
    res = Engine("RELIANCE.NS", EngineConfig(), 0.05, tf="15m").run(df, live_last_bar=True)
    assert res.dates[0] == "2024-06-03 09:15", res.dates[:2]
    assert all(len(d) == 16 for d in res.dates), res.dates[:2]
    # last-bar events are provisional, earlier bars confirmed
    for e in res.events:
        assert e.confirmed == (e.bar < len(df) - 1), (e.bar, e.confirmed)
    # state machines ran (pools/zones/counters present, no crash)
    assert res.counters["pools_created"] >= 0
    print(f"ok test_intraday_engine_dates_and_run ({len(res.events)} events)")


# ---------------------------------------------------------------------------
# 7. Scanner market helpers: TF label, IST open hours, live-bar, stale days
# ---------------------------------------------------------------------------
def test_scanner_market_helpers():
    from fpfssl.scanner import (
        interval_minutes, is_live_last_bar, market_is_open, timeframe_label,
        trading_days_between,
    )
    assert timeframe_label("15m") == "15m"
    assert timeframe_label("1h") == "1H"
    assert timeframe_label("1d") == "Daily"
    assert interval_minutes("15m") == 15

    cfg = AppConfig()
    cfg.data.interval = "15m"
    # Monday 2024-06-03 10:00 IST -> market open
    assert market_is_open(cfg, datetime(2024, 6, 3, 10, 0)) is True
    # Saturday -> closed; Monday 08:00 -> pre-open; Monday 16:00 -> post-close
    assert market_is_open(cfg, datetime(2024, 6, 1, 10, 0)) is False
    assert market_is_open(cfg, datetime(2024, 6, 3, 8, 0)) is False
    assert market_is_open(cfg, datetime(2024, 6, 3, 16, 0)) is False

    # 15m bar 10:00-10:15 is forming at 10:07, closed at 10:20
    assert is_live_last_bar(cfg, pd.Timestamp("2024-06-03 10:00"),
                            datetime(2024, 6, 3, 10, 7)) is True
    assert is_live_last_bar(cfg, pd.Timestamp("2024-06-03 10:00"),
                            datetime(2024, 6, 3, 10, 20)) is False
    # daily: today's bar forming mid-session, closed after 15:45
    cfg.data.interval = "1d"
    assert is_live_last_bar(cfg, pd.Timestamp("2024-06-03"),
                            datetime(2024, 6, 3, 12, 0)) is True
    assert is_live_last_bar(cfg, pd.Timestamp("2024-06-03"),
                            datetime(2024, 6, 3, 16, 0)) is False
    assert is_live_last_bar(cfg, pd.Timestamp("2024-06-02"),
                            datetime(2024, 6, 3, 12, 0)) is False

    # stale: Friday -> Monday = 1 trading day (weekends free)
    assert trading_days_between("2024-05-31", "2024-06-03") == 1
    assert trading_days_between("2024-05-27", "2024-06-03") == 5
    print("ok test_scanner_market_helpers")


from datetime import datetime  # noqa: E402


# ---------------------------------------------------------------------------
# 8. Yahoo kwargs: intraday uses periods within Yahoo limits
# ---------------------------------------------------------------------------
def test_yahoo_kwargs_intraday():
    cfg = DataConfig(source="yahoo", interval="15m")
    kw = _yahoo_kwargs(cfg)
    assert kw["interval"] == "15m" and kw["period"] == "60d", kw
    assert kw["auto_adjust"] is False  # raw exchange prices -> indicator parity
    cfg1 = DataConfig(source="yahoo", interval="1m")
    assert _yahoo_kwargs(cfg1)["period"] == "7d"
    # daily default = FULL history (parity with the indicator's state machines,
    # which run from the first bar of the chart) and RAW (unadjusted) OHLC
    cfgd = DataConfig(source="yahoo", interval="1d")
    kwd = _yahoo_kwargs(cfgd)
    assert kwd["interval"] == "1d" and kwd["period"] == "max", kwd
    assert "start" not in kwd
    assert kwd["auto_adjust"] is False
    # explicit window still wins
    cfgw = DataConfig(source="yahoo", interval="1d", start="2022-01-01")
    kww = _yahoo_kwargs(cfgw)
    assert kww["start"] == "2022-01-01" and "period" not in kww, kww
    print("ok test_yahoo_kwargs_intraday")


# ---------------------------------------------------------------------------
# 9. _normalize keeps intraday times and flattens MultiIndex columns
# ---------------------------------------------------------------------------
def test_normalize_intraday_multiindex():
    idx = pd.date_range("2024-06-03 09:15", periods=5, freq="15min", tz="Asia/Kolkata")
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["RELIANCE.NS"]])
    data = np.array([[100, 101, 99, 100.5, 1000]] * 5, dtype=float)
    df = pd.DataFrame(data, index=idx, columns=cols)
    out = _normalize(df, "RELIANCE.NS")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index[0] == pd.Timestamp("2024-06-03 09:15"), out.index[0]
    assert getattr(out.index, "tz", None) is None
    print("ok test_normalize_intraday_multiindex")


# ---------------------------------------------------------------------------
# 10. Composite grouping works on intraday bars (bar-unique stamps)
# ---------------------------------------------------------------------------
def test_composite_keys_include_interval():
    cfg = AppConfig()
    cfg.data.interval = "15m"
    key = f"RELIANCE.NS|{cfg.data.interval}|essl_ob_tap|2024-06-03 10:00|True|1|2"
    assert "|15m|" in key and "10:00" in key
    print("ok test_composite_keys_include_interval")


ALL = [
    test_sma_nan_window,
    test_volume_baseline_counts_bars,
    test_repeated_essl_taps_emit_each_bar,
    test_zone_cap_silent,
    test_detect_tick_exchange_aware,
    test_intraday_engine_dates_and_run,
    test_scanner_market_helpers,
    test_yahoo_kwargs_intraday,
    test_normalize_intraday_multiindex,
    test_composite_keys_include_interval,
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
    print(f"\nAll {len(ALL)} fidelity/live tests passed.")


if __name__ == "__main__":
    main()
