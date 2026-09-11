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
import logging
import os
import time
from datetime import datetime, timedelta

import pandas as pd

from .config import DataConfig

log = logging.getLogger("fpfssl.data")


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



def _cap(df: pd.DataFrame, cfg: DataConfig) -> pd.DataFrame:
    """Apply the optional hard bar cap (``max_bars``).

    Deliberately *not* capped at ``history_bars``: the live scanner needs all
    the history the feed can serve (a footprint OB born several hundred bars
    ago is still a live TAP candidate today, and cutting the frame drops that
    state). The backtest slices its own trading window.
    """
    cap = int(getattr(cfg, "max_bars", 0) or 0)
    if cap > 0 and len(df) > cap:
        df = df.tail(cap)
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
    kw: dict = {"interval": interval, "auto_adjust": bool(cfg.auto_adjust),
                "prepost": bool(cfg.prepost)}
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
        # Daily/weekly/monthly: full history by default. The Pine indicator's
        # state machines run from the first bar of the chart, so parity with
        # the indicator means the engine must see every bar the feed has —
        # `max_bars` is the only cap. An explicit period/start/end wins.
        if cfg.period:
            kw["period"] = cfg.period
        elif cfg.start or cfg.end:
            if cfg.start:
                kw["start"] = cfg.start
            if cfg.end:
                kw["end"] = cfg.end
        else:
            kw["period"] = "max"
    return kw


def load_yahoo(symbol: str, cfg: DataConfig) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as e:
        raise DataError("yfinance is not installed (pip install yfinance)") from e
    kw = _yahoo_kwargs(cfg)
    try:
        # auto_adjust follows config: False (raw exchange OHLC) is the default
        # because the Pine indicator runs on the exchange's unadjusted NSE
        # prices — adjusted series would move every level the chart shows.
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
                auto_adjust=kw["auto_adjust"], prepost=bool(cfg.prepost),
            )
        except Exception:  # noqa: BLE001
            pass
    df = _normalize(df, symbol)
    if cfg.end and cfg.is_intraday():
        try:
            df = df[df.index <= pd.Timestamp(cfg.end) + timedelta(days=1)]
        except Exception:  # noqa: BLE001
            pass
    return _cap(df, cfg)


def _split_download(raw: pd.DataFrame, group: list[str]) -> dict[str, pd.DataFrame]:
    """Split a yf.download result into per-symbol frames.

    yfinance 1.x returns a MultiIndex (Ticker, Price) by default; this helper
    also copes with (Price, Ticker) orderings and single-symbol results.
    """
    if raw is None or len(raw) == 0:
        return {}
    frames: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        wanted = {s.upper() for s in group}
        ticker_level = raw.columns.nlevels - 1  # assume the LAST level
        for lvl in range(raw.columns.nlevels):
            vals = {str(v).upper() for v in raw.columns.get_level_values(lvl)}
            if vals & wanted:
                ticker_level = lvl
                break
        for tkr in group:
            try:
                sub = raw.xs(tkr, axis=1, level=ticker_level, drop_level=True)
            except KeyError:
                continue
            if isinstance(sub, pd.Series):
                sub = sub.to_frame()
            frames[tkr] = sub.copy()
    elif len(group) == 1:
        frames[group[0]] = raw.copy()
    else:
        # un-levelled columns but several tickers requested: the first
        # level-less column name is the ticker (older yfinance versions)
        for tkr in group:
            if tkr in raw.columns:
                frames[tkr] = raw[[tkr]].copy()
    return frames


def load_yahoo_batch(symbols: list[str], cfg: DataConfig) -> dict[str, pd.DataFrame]:
    """Fetch many symbols in one pass (full-universe scanner path).

    yfinance makes one HTTP request per ticker even for bulk downloads, so a
    full-NSE daily pass is ~2,000+ requests. This fetches them threaded, in
    paced groups (`batch_size` / `batch_delay_sec`), and returns every symbol
    that produced usable bars. Missing/dead tickers are simply absent (the
    scanner logs them); DataError is only raised when NOTHING came back.
    """
    try:
        import yfinance as yf
    except ImportError as e:
        raise DataError("yfinance is not installed (pip install yfinance)") from e
    kw = _yahoo_kwargs(cfg)
    size = max(1, int(getattr(cfg, "batch_size", 100) or 100))
    threads = max(1, int(getattr(cfg, "batch_threads", 8) or 8))
    delay = float(getattr(cfg, "batch_delay_sec", 0.5) or 0)
    out: dict[str, pd.DataFrame] = {}
    missing = 0
    for i in range(0, len(symbols), size):
        group = symbols[i:i + size]
        try:
            raw = yf.download(
                group, group_by="ticker", threads=threads,
                progress=False, **kw,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("batch download failed for %d symbols (%s)", len(group), e)
            missing += len(group)
            continue
        got = 0
        for tkr, sub in _split_download(raw, group).items():
            try:
                out[tkr] = _cap(_normalize(sub, tkr), cfg)
                got += 1
            except DataError as e:  # noqa: BLE001
                log.info("%s: %s", tkr, e)
        missing += len(group) - got
        if delay and i + size < len(symbols):
            time.sleep(delay)
    if not out:
        raise DataError(f"yahoo returned no usable bars for any of {len(symbols)} symbols")
    if missing:
        log.info("yahoo batch: %d/%d symbols returned data (%d missing/dead)",
                 len(out), len(symbols), missing)
    return out


def load_all(symbols: list[str], cfg: DataConfig) -> dict[str, pd.DataFrame]:
    """Load every symbol of a pass at once (best-effort).

    * yahoo: one threaded batch call (fastest path for the full NSE universe).
    * csv / synthetic: per-symbol load; failures are logged and skipped.
    The result dict contains only symbols with usable bars.
    """
    if cfg.source == "yahoo":
        return load_yahoo_batch(symbols, cfg)
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            out[sym] = load_symbol(sym, cfg)
        except DataError as e:  # noqa: BLE001
            log.warning("%s: %s", sym, e)
    return out


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
    return _cap(df, cfg)


def load_symbol(symbol: str, cfg: DataConfig) -> pd.DataFrame:
    if cfg.source == "yahoo":
        return load_yahoo(symbol, cfg)
    if cfg.source == "csv":
        return load_csv(symbol, cfg)
    if cfg.source == "synthetic":
        from .synthetic import generate, generate_intraday
        if cfg.is_intraday():
            # intraday synthetic bars (session stamps) so the LIVE path —
            # forming last bar, HH:MM stamps, tick rounding — works offline
            return _cap(generate_intraday(symbol, cfg), cfg)
        return generate(symbol, cfg)
    raise DataError(f"unknown data source: {cfg.source}")
