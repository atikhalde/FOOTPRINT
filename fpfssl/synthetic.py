"""Deterministic synthetic daily OHLCV generator.

Purpose: offline demos and engine tests in environments without market-data
access (e.g. CI sandboxes). Produces regime-switching random walks with
volume clustering, so footprint/evidence bars, pivot lows and deep pullbacks
all occur. NOT real market data.
"""
from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from .config import DataConfig

SEED_BASE = 20260910


def generate(symbol: str, cfg: DataConfig, bars: int | None = None) -> pd.DataFrame:
    # stable across processes (hash() is salted in Python 3)
    seed = SEED_BASE + int(zlib.crc32(symbol.encode("utf-8")) % 100_000)
    rng = np.random.default_rng(seed)
    n = bars or cfg.history_bars * 2
    p0 = 50.0 + (seed % 950)

    # regime-switching drift/vol per day
    days = 0
    mu, sig = 0.0004, 0.016
    closes = [p0]
    while len(closes) < n:
        if days % 18 == 0:  # switch regime every ~3 weeks
            mu = rng.choice([-0.0035, -0.001, 0.0, 0.001, 0.003, 0.0045])
            sig = rng.uniform(0.010, 0.030)
        days += 1
        ret = rng.normal(mu, sig)
        closes.append(max(closes[-1] * (1.0 + ret), 1.0))
    closes = np.array(closes)

    # build OHLC around closes with intrabar extremes
    opens = np.empty(n)
    highs = np.empty(n)
    lows = np.empty(n)
    opens[0] = closes[0] * (1 + rng.normal(0, 0.003))
    for i in range(1, n):
        gap = rng.normal(0, 0.0035)
        opens[i] = closes[i - 1] * (1 + gap)
    spread = np.abs(rng.normal(0, 0.006)) + 0.002
    for i in range(n):
        o, c = opens[i], closes[i]
        body = abs(c - o)
        lo = min(o, c) - rng.uniform(0, 1) * (body + spread * closes[i])
        hi = max(o, c) + rng.uniform(0, 1) * (body + spread * closes[i])
        lows[i] = max(lo, c * 0.9)
        highs[i] = max(hi, max(o, c) * 1.001)

    # volume: base + lognormal spikes (evidence bars need RVOL >= 1.05..1.5)
    base = 1_000_000
    vol = base * np.exp(rng.normal(0, 0.35, n))
    spikes = rng.random(n) < 0.12
    vol[spikes] *= rng.uniform(1.8, 5.0, spikes.sum())

    # occasional absorption bars: high volume, small body, close near the high
    k = int(n * 0.05)
    idx = rng.choice(n, size=k, replace=False)
    for i in idx:
        mid = (opens[i] + closes[i]) / 2
        opens[i] = mid
        closes[i] = mid * (1 + rng.uniform(0.0005, 0.004))
        lows[i] = min(opens[i], closes[i]) * (1 - rng.uniform(0.001, 0.006))
        highs[i] = max(opens[i], closes[i]) * (1 + rng.uniform(0.001, 0.003))
        vol[i] *= rng.uniform(2.0, 4.5)

    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="B")
    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": vol,
    }, index=idx)
    df.index.name = "date"
    # round to 2 decimals like a stock, then re-enforce OHLC consistency
    # (rounding can otherwise leave high < low or high < max(open, close))
    for col in ("open", "high", "low", "close"):
        df[col] = np.round(df[col], 2)
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    df["volume"] = df["volume"].astype(int)
    return df
