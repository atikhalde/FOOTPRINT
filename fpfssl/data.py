"""Data layer: OHLCV from Yahoo (yfinance), local CSV, or synthetic.

Every source returns a pandas DataFrame indexed by bar time (tz-naive) with
lower-case columns: open, high, low, close, volume.

* Daily (`interval="1d"`): index is midnight per trading day.
* Intraday (`interval="15m"` etc): index keeps the session time (exchange wall
  time; for NSE/BSE `.NS`/`.BO` symbols that is IST). The engine and scanner
  treat every bar identically to Pine — the indicator is timeframe-agnostic.
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timedelta

import pandas as pd

from .config import DataConfig


class DataError(RuntimeError):
    pass


# Yahoo intraday lookback limits (history older than this is rejected).
_INTRADAY_MAX_DAYS = {
    "1m": 7,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "90m": 60,
    "1h": 730,
}

_VALID_INTERVALS = set(_INTRADAY_MAX_DAYS) | {"1d", "d", "1wk", "1w", "1mo"}


def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df is None or len(df) == 0:
        raise DataError(f"no data returned for {symbol}")
    df = df.copy()
    # yfinance may return MultiIndex columns (Price, Ticker) on some versions.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).strip().lower() for c in df.columns]
    else:
        df.columns = [str(c).strip().lower() for c in df.columns]
    # yfinance auto_adjust renames 'close' only; keep 'adj close' fallback.
    if "close" not in df.columns and "adj close" in df.columns:
        df["close"] = df["adj close"]
    need = {"open", "high", "low", "close"}
    if not need.issubset(df.columns):
        raise DataError(f"{symbol}: missing columns {need - set(df.columns)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    # index -> tz-naive bar timestamps (exchange wall time; IST for .NS/.BO)
    idx = pd.to_datetime(df.index)
    try:
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
    except Exception:  # noqa: BLE001
        idx = pd.to_datetime(df.index).tz_localize(None)
    df.index = idx
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[df["close"] > 0]
    # keep one row per bar time (last), sorted
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df["high"] >= df["low"]) & (df["high"] >= df["open"]) & (df["high"] >= df["close"])]
    if len(df) == 0:
        raise DataError(f"no usable bars for {symbol} after cleaning")
    return df


def _yahoo_kwargs(cfg: DataConfig) -> dict:
    """Build yfinance history() kwargs for the configured interval."""
    interval = (cfg.interval or "1d").lower()
    if interval == "d":
        interval = "1d"
    if interval == "w":
        interval = "1wk"
    if interval not in _VALID_INTERVALS:
        raise DataError(
            f"invalid interval {cfg.interval!r}: use one of "
            "1m,2m,5m,15m,30m,60m,90m,1h,1d,1wk,1mo"
        )
    kw: dict = {"interval": interval, "auto_adjust": True, "prepost": bool(cfg.prepost)}
    if interval in _INTRADAY_MAX_DAYS:
        # Intraday: Yahoo only serves a limited lookback. Prefer an explicit
        # period, else a recent start/end window, else the max allowed period.
        max_days = _INTRADAY_MAX_DAYS[interval]
        if cfg.period:
            kw["period"] = cfg.period
        elif cfg.start or cfg.end:
            try:
                ok = True
                if cfg.start:
                    age = (datetime.now() - pd.Timestamp(cfg.start)).days
                    ok = age <= max_days
                if ok:
                    if cfg.start:
                        kw["start"] = cfg.start
                    if cfg.end:
                        kw["end"] = cfg.end
                else:
                    kw["period"] = f"{max_days}d"
            except Exception:  # noqa: BLE001
                kw["period"] = f"{max_days}d"
        else:
            kw["period"] = f"{max_days}d"
    else:
        kw["start"] = cfg.start or "2020-01-01"
        if cfg.end:
            kw["end"] = cfg.end
        if cfg.period:
            kw["period"] = cfg.period
    return kw


def load_yahoo(symbol: str, cfg: DataConfig) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as e:
        raise DataError("yfinance is not installed (pip install yfinance)") from e
    kw = _yahoo_kwargs(cfg)
    try:
        # auto_adjust=True: split/dividend-adjusted OHLC, so ATR/pivots see a
        # continuous price series (unadjusted data injects fake gaps on ex-div dates)
        df = yf.Ticker(symbol).history(**kw)
    except Exception as e:  # noqa: BLE001
        raise DataError(f"{symbol}: yahoo download failed ({e})") from e
    # Fallback: an explicit start/end outside Yahoo's intraday window returns
    # empty — retry once with the max allowed period.
    if (df is None or len(df) == 0) and "start" in kw and (cfg.interval or "").lower() in _INTRADAY_MAX_DAYS:
        max_days = _INTRADAY_MAX_DAYS[(cfg.interval or "").lower()]
        try:
            df = yf.Ticker(symbol).history(
                period=f"{max_days}d", interval=kw["interval"],
                auto_adjust=True, prepost=bool(cfg.prepost),
            )
        except Exception:  # noqa: BLE001
            pass
    df = _normalize(df, symbol)
    if cfg.end and cfg.is_intraday():
        try:
            df = df[df.index <= pd.Timestamp(cfg.end) + timedelta(days=1)]
        except Exception:  # noqa: BLE001
            pass
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
            "Format: header 'date,open,high,low,close,volume' "
            "(date = YYYY-MM-DD or YYYY-MM-DD HH:MM for intraday)"
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
        df = df[df.index <= pd.Timestamp(cfg.end) + timedelta(days=1)]
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
