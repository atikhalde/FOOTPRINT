"""Deterministic synthetic OHLCV generators (daily + intraday).

Purpose: offline demos and engine/scanner tests in environments without
market-data access (e.g. CI sandboxes). Produces regime-switching random
walks with volume clustering, so footprint/evidence bars, pivot lows and deep
pullbacks all occur. NOT real market data.

`generate` produces daily bars; `generate_intraday` produces exchange-session
bars (09:15-15:30 IST, weekdays) so the live scanner path — forming last bar,
tick rounding, HH:MM bar stamps — can be exercised offline.
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


def _ticks(symbol: str) -> float:
    sym = (symbol or "").upper()
    return 0.05 if (sym.endswith(".NS") or sym.endswith(".BO")) else 0.01


def session_index(days: int, minutes: int = 15, end: pd.Timestamp | None = None) -> pd.DatetimeIndex:
    """Weekday exchange sessions of `minutes` bars starting 09:15 (IST wall time)."""
    per_day = int(375 / minutes)
    end = (end or pd.Timestamp.today()).normalize()
    stamps: list[pd.Timestamp] = []
    d = end
    while len(stamps) < days * per_day:
        if d.weekday() < 5:
            day: list[pd.Timestamp] = []
            t = d.replace(hour=9, minute=15)
            stop = d.replace(hour=15, minute=30)
            while t < stop and len(day) < per_day:
                day.append(t)
                t += pd.Timedelta(minutes=minutes)
            stamps = day + stamps
        d -= pd.Timedelta(days=1)
    idx = pd.DatetimeIndex(stamps[-days * per_day:])
    idx.name = "date"
    return idx


def generate_intraday(symbol: str, cfg: DataConfig, days: int | None = None,
                      minutes: int = 15, seed: int | None = None) -> pd.DataFrame:
    """Deterministic intraday OHLCV ending at the last completed session.

    Tick-rounded (0.05 for .NS/.BO), volume clustered and spiked so the
    evidence/RVOL and pivot machinery both fire, with enough range to create
    footprint OBs and eSSL pools — i.e. the same feature mix the live scanner
    sees, without touching the network.
    """
    tick = _ticks(symbol)
    if minutes not in (1, 2, 5, 15, 30, 60):
        minutes = 15
    days = int(days or max(10, cfg.history_bars // int(375 / minutes) + 1))
    idx = session_index(days, minutes)
    n = len(idx)
    s = (SEED_BASE + int(zlib.crc32(symbol.encode("utf-8")) % 100_000)
         if seed is None else int(seed))
    rng = np.random.default_rng(s)
    p0 = 250.0 + (s % 4000) / 2.0

    # per-bar vol scaled to the bar length; slow swings create pivot lows/highs
    scale = (minutes / 15.0) ** 0.5
    swing = np.sin(np.arange(n) / (95.0 / scale)) * 0.35 * scale
    ret = rng.normal(0, 0.0016 * scale, n) + np.diff(np.concatenate([[0.0], swing])) * 0.004
    close = p0 * np.exp(np.cumsum(ret))
    op = np.empty(n)
    op[0] = close[0] * (1 + rng.normal(0, 0.0005))
    op[1:] = close[:-1] * (1 + rng.normal(0, 0.0005, n - 1))
    body = np.abs(close - op)
    wick = np.abs(rng.normal(0, 1, n)) * 0.0011 * scale * close + body
    hi = np.maximum(op, close) + rng.uniform(0.05, 1.0, n) * wick
    lo = np.minimum(op, close) - rng.uniform(0.05, 1.0, n) * wick

    vol = 250_000.0 * np.exp(rng.normal(0, 0.35, n))
    spikes = rng.random(n) < 0.11
    vol[spikes] *= rng.uniform(1.8, 4.5, int(spikes.sum()))
    # absorption bars: high volume, small body, close near the high
    for i in rng.choice(n, size=max(1, n // 25), replace=False):
        mid = (op[i] + close[i]) * 0.5
        op[i], close[i] = mid, mid * (1 + rng.uniform(0.0004, 0.003))
        lo[i] = min(op[i], close[i]) * (1 - rng.uniform(0.0008, 0.005))
        hi[i] = max(op[i], close[i]) * (1 + rng.uniform(0.0008, 0.0025))
        vol[i] *= rng.uniform(2.0, 4.0)

    df = pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close,
                       "volume": vol}, index=idx)
    df.index.name = "date"
    for col in ("open", "high", "low", "close"):
        df[col] = np.round(df[col] / tick) * tick
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    df["volume"] = df["volume"].astype(int)
    return df
