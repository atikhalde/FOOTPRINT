"""Data layer: daily OHLCV from Yahoo (yfinance), local CSV, or synthetic.

Every source returns a pandas DataFrame indexed by date (tz-naive) with
lower-case columns: open, high, low, close, volume.
"""
from __future__ import annotations

import io
import os

import numpy as np
import pandas as pd

from .config import DataConfig


class DataError(RuntimeError):
    pass


def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df is None or len(df) == 0:
        raise DataError(f"no data returned for {symbol}")
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    need = {"open", "high", "low", "close"}
    if not need.issubset(df.columns):
        raise DataError(f"{symbol}: missing columns {need - set(df.columns)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    # index -> tz-naive dates
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[df["close"] > 0]
    # keep one row per day (last), sorted
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df["high"] >= df["low"]) & (df["high"] >= df["open"]) & (df["high"] >= df["close"])]
    return df


def load_yahoo(symbol: str, cfg: DataConfig) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as e:
        raise DataError("yfinance is not installed (pip install yfinance)") from e
    start = cfg.start or "2020-01-01"
    end = cfg.end
    df = yf.Ticker(symbol).history(start=start, end=end, interval="1d", auto_adjust=False)
    df = _normalize(df, symbol)
    if len(df) > cfg.history_bars * 2:
        df = df.tail(cfg.history_bars * 2)
    return df


def load_csv(symbol: str, cfg: DataConfig) -> pd.DataFrame:
    d = cfg.csv_dir
    candidates = [
        os.path.join(d, f"{symbol}.csv"),
        os.path.join(d, symbol.replace(".", "_") + ".csv"),
        os.path.join(d, symbol.lower() + ".csv"),
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        raise DataError(
            f"no CSV for {symbol}: expected one of {candidates}\n"
            "Format: header 'date,open,high,low,close,volume' (date = YYYY-MM-DD or YYYYMMDD)"
        )
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    head = text.lstrip().splitlines()[0].lower() if text.strip() else ""
    if "date" in head:
        df = pd.read_csv(io.StringIO(text), parse_dates=["date"])
        df = df.set_index("date")
    else:
        df = pd.read_csv(io.StringIO(text), header=None,
                         names=["date", "open", "high", "low", "close", "volume"])
        df = df.set_index("date")
        df.index = pd.to_datetime(df.index)
    if cfg.start:
        df = df[df.index >= pd.Timestamp(cfg.start)]
    if cfg.end:
        df = df[df.index <= pd.Timestamp(cfg.end)]
    df = _normalize(df, symbol)
    if len(df) > cfg.history_bars * 2:
        df = df.tail(cfg.history_bars * 2)
    return df


def load_symbol(symbol: str, cfg: DataConfig) -> pd.DataFrame:
    if cfg.source == "yahoo":
        return load_yahoo(symbol, cfg)
    if cfg.source == "csv":
        return load_csv(symbol, cfg)
    if cfg.source == "synthetic":
        from .synthetic import generate
        return generate(symbol, cfg)
    raise DataError(f"unknown data source: {cfg.source}")
