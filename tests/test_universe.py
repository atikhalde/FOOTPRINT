"""Full-NSE universe + batched yahoo loading tests (offline).

Covers the `full_nse` marker expansion (cache, refresh, fallback chain, hard
failure), yf.download result splitting and the batch scanner path. No network:
all fetchers are monkeypatched.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from fpfssl.config import AppConfig, DataConfig, EngineConfig
from fpfssl.data import DataError, _split_download, load_all
from fpfssl import universe as uni


def mk_data(tmpdir, **kw) -> DataConfig:
    cfg = DataConfig(source="yahoo", interval="1d", **kw)
    cfg.universe_cache_file = os.path.join(tmpdir, "nse_universe.csv")
    return cfg


# ---------------------------------------------------------------------------
# 1. marker parsing / normalization
# ---------------------------------------------------------------------------
def test_normalize_and_markers():
    assert uni.normalize_nse_symbol("reliance") == "RELIANCE.NS"
    assert uni.normalize_nse_symbol("TCS.NS") == "TCS.NS"
    assert uni.normalize_nse_symbol(' "SBIN.NS" ') == "SBIN.NS"
    assert "full_nse" in uni.UNIVERSE_MARKERS
    assert "*" in uni.UNIVERSE_MARKERS
    print("ok test_normalize_and_markers")


# ---------------------------------------------------------------------------
# 2. expansion: explicit list untouched, marker -> cached list (no network)
# ---------------------------------------------------------------------------
def test_expand_explicit_list_no_fetch():
    tmp = tempfile.mkdtemp(prefix="fpfssl-uni-")
    cfg = mk_data(tmp)
    out = uni.expand_universe(["RELIANCE.NS", "TCS.NS"], cfg)
    assert out == ["RELIANCE.NS", "TCS.NS"], out
    print("ok test_expand_explicit_list_no_fetch")


def test_expand_marker_uses_cache():
    tmp = tempfile.mkdtemp(prefix="fpfssl-uni-")
    cfg = mk_data(tmp)
    uni._write_cache(cfg.universe_cache_file, ["RELIANCE.NS", "TCS.NS", "INFY.NS"])

    def fail(c):
        raise AssertionError("network used while a fresh cache exists!")

    uni._fetch_nse_official = fail
    out = uni.expand_universe(["full_nse", "HDFCBANK.NS"], cfg)
    assert out[:1] == ["HDFCBANK.NS"], out  # explicit kept first
    assert set(out) == {"HDFCBANK.NS", "RELIANCE.NS", "TCS.NS", "INFY.NS"}
    assert len(out) == 4  # no duplicates
    print("ok test_expand_marker_uses_cache")


def test_stale_cache_refetches():
    tmp = tempfile.mkdtemp(prefix="fpfssl-uni-")
    cfg = mk_data(tmp, universe_max_age_days=7.0)
    uni._write_cache(cfg.universe_cache_file, ["OLD.NS"])
    # make the cache old
    old = datetime.now() - timedelta(days=8)
    os.utime(cfg.universe_cache_file, (old.timestamp(), old.timestamp()))
    calls = []

    def fake_official(c):
        calls.append("official")
        return ["RELIANCE.NS", "TATASTEEL.NS"]

    uni._fetch_nse_official = fake_official
    out = uni.expand_universe(["full_nse"], cfg)
    assert calls == ["official"], calls
    assert out == ["RELIANCE.NS", "TATASTEEL.NS"], out
    # and the fresh result got cached
    again = uni.expand_universe(["full_nse"], cfg)
    assert again == out
    assert len(calls) == 1  # second call served from the fresh cache
    print("ok test_stale_cache_refetches")


def test_fetch_falls_back_then_fails_loud():
    tmp = tempfile.mkdtemp(prefix="fpfssl-uni-")
    cfg = mk_data(tmp)

    def boom(c):
        raise RuntimeError("down")

    uni._fetch_nse_official = boom
    uni._fetch_yahoo_screener = boom
    try:
        uni.fetch_nse_universe(cfg, refresh=True)
        raise AssertionError("expected DataError")
    except DataError as e:
        assert "full-NSE symbol list" in str(e), e
    print("ok test_fetch_falls_back_then_fails_loud")


# ---------------------------------------------------------------------------
# 3. yf.download result splitting (both column level orders + single symbol)
# ---------------------------------------------------------------------------
def test_split_download_ticker_price():
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    cols = pd.MultiIndex.from_product(
        [["RELIANCE.NS", "TCS.NS"], ["Open", "High", "Low", "Close", "Volume"]],
        names=["Ticker", "Price"])
    raw = pd.DataFrame(np.arange(50, dtype=float).reshape(5, 10), index=idx, columns=cols)
    frames = _split_download(raw, ["RELIANCE.NS", "TCS.NS"])
    assert set(frames) == {"RELIANCE.NS", "TCS.NS"}
    assert list(frames["RELIANCE.NS"].columns) == ["Open", "High", "Low", "Close", "Volume"]
    print("ok test_split_download_ticker_price")


def test_split_download_price_ticker_and_missing():
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    cols = pd.MultiIndex.from_product(
        [["open", "high", "low", "close", "volume"], ["RELIANCE.NS", "DEAD.NS"]],
        names=["Price", "Ticker"])
    raw = pd.DataFrame(np.arange(50, dtype=float).reshape(5, 10), index=idx, columns=cols)
    frames = _split_download(raw, ["RELIANCE.NS", "DEAD.NS", "GONE.NS"])
    assert set(frames) == {"RELIANCE.NS", "DEAD.NS"}  # GONE.NS absent, not error
    assert list(frames["RELIANCE.NS"].columns) == ["open", "high", "low", "close", "volume"]
    print("ok test_split_download_price_ticker_and_missing")


# ---------------------------------------------------------------------------
# 4. batch loader: paced groups, normalization, dead tickers skipped
# ---------------------------------------------------------------------------
def test_load_yahoo_batch_paces_and_normalizes():
    tmp = tempfile.mkdtemp(prefix="fpfssl-uni-")
    cfg = mk_data(tmp, batch_size=2, batch_threads=1, batch_delay_sec=0)
    calls = []

    def fake_download(group, **kw):
        calls.append(list(group))
        assert kw.get("auto_adjust") is False, kw  # raw prices for parity
        idx = pd.date_range("2024-01-01", periods=30, freq="B")
        cols = pd.MultiIndex.from_product(
            [group, ["Open", "High", "Low", "Close", "Volume"]])
        data = np.random.default_rng(1).uniform(100, 110, (30, 5 * len(group)))
        return pd.DataFrame(data, index=idx, columns=cols)

    import yfinance as yf
    orig = yf.download
    yf.download = fake_download
    try:
        frames = load_all(["A.NS", "B.NS", "C.NS"], cfg)
    finally:
        yf.download = orig
    assert len(frames) == 3 and len(calls) == 2  # chunks of 2 -> two calls
    for sym, df in frames.items():
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    print("ok test_load_yahoo_batch_paces_and_normalizes")


def test_load_yahoo_batch_total_failure_raises():
    tmp = tempfile.mkdtemp(prefix="fpfssl-uni-")
    cfg = mk_data(tmp, batch_size=2)

    def fake_download(group, **kw):
        raise RuntimeError("yahoo down")

    import yfinance as yf
    orig = yf.download
    yf.download = fake_download
    try:
        try:
            load_all(["A.NS", "B.NS"], cfg)
            raise AssertionError("expected DataError")
        except DataError as e:
            assert "no usable bars" in str(e), e
    finally:
        yf.download = orig
    print("ok test_load_yahoo_batch_total_failure_raises")


# ---------------------------------------------------------------------------
# 5. scanner batch path: same signals as per-symbol path (parity guard)
# ---------------------------------------------------------------------------
def test_scanner_batch_path_same_alerts():
    from fpfssl import scanner as scn
    from fpfssl.engine import Engine
    from fpfssl.synthetic import generate
    from tests.test_live_scanner import Recorder

    sym = "SBIN.NS"
    # deterministic daily frame ENDING TODAY (the yahoo daily scanner path)
    df = generate(sym, DataConfig(source="synthetic", history_bars=700))
    res = Engine(sym, EngineConfig(), 0.05, tf="1d").run(df, live_last_bar=False)
    recent = {e.kind for e in res.events if e.bar >= len(df) - 3}
    assert recent, "fixture must produce recent events for this parity test"

    def mk_cfg_daily():
        cfg = AppConfig()
        cfg.symbols = [sym]
        cfg.data = DataConfig(source="yahoo", interval="1d", history_bars=600)
        cfg.scanner.min_bars = 100
        cfg.scanner.provisional_alerts = True
        cfg.scanner.alert_cooldown_minutes = 60
        cfg.scanner.state_file = os.path.join(tempfile.mkdtemp(prefix="fpfssl-uni-"),
                                              "scanner_state.json")
        return cfg

    # path A: per-symbol fetch (load_symbol monkeypatched with the same frame)
    rec_a = Recorder()
    sc_a = scn.LiveScanner(mk_cfg_daily(), rec_a, symbols=[sym])
    scn.load_symbol = lambda s, d: df
    sc_a.scan_symbol(sym)

    # path B: batch-fetched frame handed to scan_symbol directly
    rec_b = Recorder()
    sc_b = scn.LiveScanner(mk_cfg_daily(), rec_b, symbols=[sym])
    sc_b.scan_symbol(sym, df)

    assert rec_a.messages, "fixture should alert through the scanner"
    assert rec_a.messages == rec_b.messages, (rec_a.messages, rec_b.messages)
    print("ok test_scanner_batch_path_same_alerts")


# ---------------------------------------------------------------------------
# 6. diagnose over a batch-fetched universe emits clean machine-readable JSON
# ---------------------------------------------------------------------------
def test_diag_json_daily_full_universe():
    import json
    from fpfssl.diag import format_diag_many, run_diag

    syms = ["RELIANCE.NS", "TCS.NS"]
    cfg = AppConfig()
    cfg.symbols = syms
    cfg.data = DataConfig(source="synthetic", interval="1d")
    cfg.scanner.min_bars = 100
    diags = run_diag(cfg, syms)
    payload = json.loads(format_diag_many(diags, as_json=True))
    assert {d["symbol"] for d in payload} == set(syms), payload
    print("ok test_diag_json_daily_full_universe")


if __name__ == "__main__":
    import traceback
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception:
                print(f"FAIL {name}")
                traceback.print_exc()
                sys.exit(1)
    print("All universe tests passed.")
