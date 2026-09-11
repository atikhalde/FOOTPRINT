"""Full-NSE universe resolution for the live scanner / backtest.

Put the marker in ``config.yaml`` (or pass it to ``--symbols``):

    symbols:
      - full_nse            # also: all_nse, nse, *

and every command expands it to the complete NSE equity list (Yahoo ``.NS``
suffix, ~2,000+ symbols). Explicit symbols can be mixed in:

    symbols:
      - full_nse
      - HDFCBANK.NS        # listed twice is harmless — deduped

Resolution order:
  1. cached list from ``data.universe_cache_file`` when younger than
     ``data.universe_max_age_days`` (default 7 days — listings rarely change)
  2. NSE's official equity list (EQUITY_L.csv archive, EQ series only)
  3. Yahoo Finance equity screener (exchange "NSI"), paginated
  4. offline / all sources failed -> DataError with instructions.
     The scanner NEVER silently falls back to a smaller universe: a wrong
     universe means silent missed alerts, which is worse than a loud error.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
from datetime import datetime, timedelta

from .config import AppConfig, DataConfig
from .data import DataError

log = logging.getLogger("fpfssl.universe")

# markers understood in `symbols:` (case-insensitive)
UNIVERSE_MARKERS = {"full_nse", "all_nse", "nse", "nse_all", "*"}

_NSE_EQUITY_CSV = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def normalize_nse_symbol(sym: str) -> str:
    s = (sym or "").strip().upper().replace('"', "").replace("'", "")
    if not s:
        return s
    if not s.endswith(".NS"):
        s += ".NS"
    return s


def expand_universe(symbols: list[str], cfg: DataConfig,
                    refresh: bool = False) -> list[str]:
    """Replace universe markers with the full symbol list; dedupe, keep order.

    The marker resolves differently per data source:
      * yahoo     -> full NSE equity list (cached; see fetch_nse_universe)
      * csv       -> the local CSV files in `csv_dir` (offline)
      * synthetic -> the built-in demo symbol list (offline)
    """
    out: list[str] = []
    seen: set[str] = set()
    needs_universe = False
    for raw in symbols or []:
        if str(raw).strip().lower() in UNIVERSE_MARKERS:
            needs_universe = True
            continue
        s = normalize_nse_symbol(str(raw))
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    if not needs_universe:
        return out
    if (cfg.source or "").lower() == "csv":
        universe = list_csv_symbols(getattr(cfg, "csv_dir", "data") or "data")
        log.info("universe: %d local CSV symbols (source=csv)", len(universe))
        norm = lambda s: (s or "").strip().upper()  # noqa: E731 - keep file names as-is
    elif (cfg.source or "").lower() == "synthetic":
        universe = list(AppConfig().symbols)
        log.warning("universe: synthetic source — full_nse expands to the %d built-in "
                    "demo symbols (offline)", len(universe))
        norm = normalize_nse_symbol
    else:
        universe = fetch_nse_universe(cfg, refresh=refresh)
        norm = normalize_nse_symbol
    for s in universe:
        s = norm(s)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    log.info("universe: %d symbols after expanding full_nse", len(out))
    return out


def list_csv_symbols(csv_dir: str) -> list[str]:
    """Symbol names for every *.csv in the directory (offline universe)."""
    syms: list[str] = []
    if not csv_dir or not os.path.isdir(csv_dir):
        return syms
    for name in sorted(os.listdir(csv_dir)):
        if not name.lower().endswith(".csv"):
            continue
        stem = name[:-4]
        syms.append(stem.replace("_", ".") if "." not in stem else stem)
    return syms


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
def _read_cache(path: str, max_age_days: float) -> list[str] | None:
    if not path or not os.path.exists(path):
        return None
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        if datetime.now() - mtime > timedelta(days=max_age_days):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            syms = [normalize_nse_symbol(ln) for ln in fh
                    if ln.strip() and not ln.lstrip().startswith("#")]
        return [s for s in syms if s] or None
    except Exception:  # noqa: BLE001
        return None


def _write_cache(path: str, syms: list[str]) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"# full-NSE equity list, fetched {datetime.utcnow().isoformat()}Z\n")
            fh.write(f"# {len(syms)} symbols\n")
            for s in syms:
                fh.write(s + "\n")
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("could not write universe cache %s: %s", path, e)


def _fetch_nse_official(cfg: DataConfig) -> list[str]:
    """NSE's official full equity list (archive CSV). Series 'EQ' = equity."""
    import requests
    r = requests.get(_NSE_EQUITY_CSV, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    syms, header = [], None
    for row in reader:
        if header is None:
            # tolerate a BOM on the header row
            header = {str(k).lstrip("\ufeff").strip().upper(): k for k in row.keys()}
        sym = (row.get(header.get("SYMBOL", "SYMBOL")) or "").strip()
        series = (row.get(header.get("SERIES", "SERIES")) or "").strip().upper()
        if sym and series == "EQ":
            syms.append(normalize_nse_symbol(sym))
    if not syms:
        raise DataError("NSE equity list parsed empty")
    return sorted(set(syms))


def _fetch_yahoo_screener(cfg: DataConfig) -> list[str]:
    """Yahoo equity screener, exchange 'NSI' (NSE), paginated 250/page."""
    try:
        from yfinance import EquityQuery
        from yfinance import screener as yf_screener
    except Exception as e:  # noqa: BLE001
        raise DataError(f"yfinance screener unavailable ({e})") from e
    # EquityQuery already implies quoteType=EQUITY; the only filter needed is
    # the NSE exchange ("NSI" in Yahoo's screener vocabulary).
    query = EquityQuery("and", [
        EquityQuery("is-in", ["exchange", "NSI"]),
        EquityQuery("eq", ["region", "in"]),
    ])
    syms: set[str] = set()
    offset, size = 0, 250
    for _ in range(80):  # hard page cap (80 * 250 = 20k, NSE is ~2.5k)
        try:
            page = yf_screener.screen(query, offset=offset, size=size)
        except Exception as e:  # noqa: BLE001
            log.warning("yahoo screener page %d failed: %s", offset, e)
            break
        quotes = page.get("quotes") if isinstance(page, dict) else None
        if not quotes:
            break
        for item in quotes:
            sym = item.get("symbol") if isinstance(item, dict) else item
            if sym:
                syms.add(normalize_nse_symbol(str(sym)))
        if len(quotes) < size:
            break
        offset += size
    if not syms:
        raise DataError("yahoo screener returned no NSE symbols")
    return sorted(syms)


def fetch_nse_universe(cfg: DataConfig, refresh: bool = False) -> list[str]:
    """Full NSE equity list ('.NS' tickers), cached on disk between runs."""
    cache_path = getattr(cfg, "universe_cache_file", None) or None
    max_age = float(getattr(cfg, "universe_max_age_days", 7.0) or 7.0)
    if not refresh:
        cached = _read_cache(cache_path, max_age)
        if cached:
            log.info("universe: using cached full-NSE list (%d symbols)", len(cached))
            return cached
    errors: list[str] = []
    for name, fn in (("NSE official list", _fetch_nse_official),
                     ("yahoo screener", _fetch_yahoo_screener)):
        try:
            syms = fn(cfg)
            _write_cache(cache_path, syms)
            log.info("universe: fetched %d NSE symbols via %s", len(syms), name)
            return syms
        except Exception as e:  # noqa: BLE001
            log.warning("universe source '%s' failed: %s", name, e)
            errors.append(f"{name}: {e}")
    raise DataError(
        "could not fetch the full-NSE symbol list (network blocked or both "
        "sources failed). Fix connectivity, set an explicit symbol list in "
        f"config.yaml instead of full_nse, or re-run with --refresh-universe "
        f"once a cached list exists. Errors: {'; '.join(errors)}"
    )
