"""NSE fundamentals (share count / market capitalisation) — the size filters.

The scanner's *size* filters need one number per symbol that the OHLCV feed does
not carry: how many shares are outstanding. Market capitalisation is then

    market cap = shares outstanding × last bar close

so the value is recomputed on EVERY pass from the price the scanner already
fetched: a symbol that sinks below the threshold drops out of the scan and the
moment it recovers it is scanned again — no stale snapshot decides what you see.

Share counts move ~quarterly, so they are fetched lazily and cached on disk
(`data.fundamentals_cache_file`, same idea as the full-NSE symbol list in
``universe.py``): only symbols with no usable cache entry — or an entry older
than ``data.fundamentals_max_age_days`` — cost a request, and the first
full-NSE pass is the only expensive one.

Currency: Yahoo reports market cap in the LISTING currency, so `.NS`/`.BO`
values are INR and the threshold is in ₹ crore (1 crore = 1e7, i.e.
₹1,000 cr = ₹1e10). A symbol quoted in another currency is reported and left
alone rather than mis-filtered.

Fail-open policy (`scanner.keep_unknown_market_cap`): when no share count can
be resolved for a symbol the scanner KEEPS it and logs once. Dropping symbols
because a metadata endpoint hiccuped would silently swallow alerts, which this
project treats as worse than scanning one too many names.
"""
from __future__ import annotations

import csv
import logging
import math
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from .config import AppConfig
from .data import DataError

log = logging.getLogger("fpfssl.fundamentals")

#: 1 crore = 1e7 (₹). Market-cap thresholds in this project are ₹ crore.
CRORE = 1e7

_COLUMNS = ["symbol", "shares_outstanding", "market_cap", "price", "currency", "fetched"]


# ---------------------------------------------------------------------------
# one symbol's fundamentals
# ---------------------------------------------------------------------------
@dataclass
class Fundamentals:
    symbol: str
    shares: float = 0.0        # shares outstanding
    market_cap: float = 0.0    # as reported, listing currency (absolute ₹)
    price: float = 0.0         # the price the reported market cap belongs to
    currency: str = "INR"
    fetched: str = ""          # ISO timestamp of the fetch

    @property
    def is_inr(self) -> bool:
        """INR-listed (or currency unknown — NSE/BSE tickers are INR)."""
        return (self.currency or "").upper() in ("", "INR")

    @property
    def has_data(self) -> bool:
        return self.shares > 0 or self.market_cap > 0

    def market_cap_cr(self, price: float | None = None) -> float:
        """Market cap in ₹ crore at `price` (0.0 = unknown).

        The share count is preferred (it barely moves, so it stays accurate as
        the price ticks); a reported absolute market cap is the fallback and is
        scaled by price/price-at-fetch to reflect the current bar.
        """
        px = float(price) if price and price > 0 else self.price
        if self.shares > 0 and px > 0:
            return self.shares * px / CRORE
        if self.market_cap > 0:
            mc = float(self.market_cap)
            if px > 0 and self.price > 0:
                mc *= px / self.price
            return mc / CRORE
        return 0.0


# ---------------------------------------------------------------------------
# the scanner-side filter
# ---------------------------------------------------------------------------
@dataclass
class SizeFilters:
    """`scanner.min_market_cap_cr` / `scanner.min_price` as a reusable check.

    Both are *strictly greater than* thresholds (a ₹100.00 stock with
    `min_price: 100` is filtered out). 0 disables that one filter; a symbol
    whose market cap cannot be resolved is kept when `fail_open` is set.
    """
    min_market_cap_cr: float = 0.0
    min_price: float = 0.0
    fail_open: bool = True
    overrides: dict[str, float] = field(default_factory=dict)  # symbol -> ₹ crore
    warned: set = field(default_factory=set)   # symbols already logged (spam guard)

    @property
    def enabled(self) -> bool:
        return self.min_market_cap_cr > 0 or self.min_price > 0

    @property
    def mcap_on(self) -> bool:
        return self.min_market_cap_cr > 0

    def describe(self) -> str:
        parts = []
        if self.min_market_cap_cr > 0:
            parts.append(f"mcap > ₹{self.min_market_cap_cr:,.0f} Cr")
        if self.min_price > 0:
            parts.append(f"price > ₹{self.min_price:,.2f}")
        return " + ".join(parts) if parts else "none"

    @staticmethod
    def from_config(sc) -> "SizeFilters":
        return SizeFilters(
            min_market_cap_cr=float(getattr(sc, "min_market_cap_cr", 0) or 0),
            min_price=float(getattr(sc, "min_price", 0) or 0),
            fail_open=bool(getattr(sc, "keep_unknown_market_cap", True)),
            overrides={str(k).upper(): float(v)
                       for k, v in (getattr(sc, "market_cap_cr_overrides", None) or {}).items()},
        )

    def check(self, symbol: str, price: float,
              table: "FundamentalTable | None" = None) -> tuple[bool, str]:
        """`(keep, reason)` — `reason` explains a rejection (empty when kept)."""
        if not self.enabled:
            return True, ""
        try:
            px = float(price or 0.0)
        except (TypeError, ValueError):
            px = float("nan")
        if not math.isfinite(px) or px <= 0:
            # A feed hiccup (NaN / zero close) is not "a cheap stock": keep the
            # symbol and let the data-quality guards decide, per the fail-open
            # policy below.
            return True, ""
        if self.min_price > 0 and not px > self.min_price:
            return False, (f"price ₹{px:,.2f} below the ₹{self.min_price:,.2f} "
                           f"minimum (scanner.min_price)")
        if self.mcap_on:
            cr = self.market_cap_cr(symbol, px, table)
            if cr is None:
                if not self.fail_open:
                    return False, ("market cap unknown (needs a share count) and "
                                   "scanner.keep_unknown_market_cap is false")
                return True, ""
            if not cr > self.min_market_cap_cr:
                return False, (f"market cap ₹{cr:,.0f} Cr below the "
                               f"₹{self.min_market_cap_cr:,.0f} Cr minimum "
                               f"(scanner.min_market_cap_cr)")
        return True, ""

    def market_cap_cr(self, symbol: str, price: float,
                      table: "FundamentalTable | None" = None) -> float | None:
        """₹ crore for `symbol` at `price`, or None when it cannot be judged.

        None means "no verdict", which `check()` turns into fail-open (keep) or
        into a skip when `fail_open` is false. A non-INR listing (the data layer
        also serves US tickers) is deliberately no verdict: a ₹ crore threshold
        cannot be compared against a USD market cap.
        """
        sym = (symbol or "").upper()
        if sym in self.overrides:
            return float(self.overrides[sym])
        f = table.get(sym) if table is not None else None
        if f is None:
            return None
        if not f.is_inr:
            self._warn_once(sym, f"listed in {f.currency}, not INR — a ₹ crore "
                            f"threshold cannot judge it (price filter still applies)")
            return None
        cr = f.market_cap_cr(price)
        if cr <= 0:
            self._warn_once(sym, "no share count cached — market cap unknown")
        return cr if cr > 0 else None

    def _warn_once(self, symbol: str, why: str) -> None:
        """One log line per symbol per run: a whole universe without share counts
        must not produce 2,000 identical warnings, but the first one has to be
        loud enough to explain why the filter did nothing."""
        if symbol in self.warned:
            return
        self.warned.add(symbol)
        if len(self.warned) == 1 or len(self.warned) % 500 == 0:
            log.warning("market-cap filter: %s %s (%d symbol(s) like this so far)",
                        symbol, why, len(self.warned))


# ---------------------------------------------------------------------------
# cache + fetch
# ---------------------------------------------------------------------------
class FundamentalTable:
    """Fundamentals for every symbol the scanner touches (disk-cached)."""

    def __init__(self, entries: dict[str, Fundamentals] | None = None, *,
                 cache_file: str = "", max_age_days: float = 30.0):
        self.entries: dict[str, Fundamentals] = entries or {}
        self.cache_file = cache_file or ""
        self.max_age_days = float(max_age_days or 0)

    # -- construction --------------------------------------------------------
    @staticmethod
    def from_config(cfg: AppConfig) -> "FundamentalTable":
        d = cfg.data
        return FundamentalTable(
            cache_file=getattr(d, "fundamentals_cache_file", "") or "",
            max_age_days=float(getattr(d, "fundamentals_max_age_days", 30.0) or 0),
        ).load()

    @staticmethod
    def for_test(entries: dict[str, float], **kw) -> "FundamentalTable":
        """₹ crore -> table, for offline tests (no share count, no network).

        `price` stays 0 on purpose: the absolute-market-cap branch would
        otherwise rescale the pinned value by price/price-at-fetch.
        """
        out = {s.upper(): Fundamentals(symbol=s.upper(), market_cap=float(cr) * CRORE,
                                       currency="INR")
               for s, cr in (entries or {}).items()}
        return FundamentalTable(out, **kw)

    def load(self) -> "FundamentalTable":
        path = self.cache_file
        if not path or not os.path.exists(path):
            return self
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                rows = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
            if not rows:
                return self
            for row in csv.DictReader(rows):
                sym = (row.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                def num(key: str) -> float:
                    try:
                        return float(row.get(key) or 0.0)
                    except (TypeError, ValueError):
                        return 0.0
                self.entries[sym] = Fundamentals(
                    symbol=sym, shares=num("shares_outstanding"),
                    market_cap=num("market_cap"), price=num("price"),
                    currency=(row.get("currency") or "INR").strip(),
                    fetched=(row.get("fetched") or "").strip())
            log.info("fundamentals: loaded %d cached share counts from %s",
                     len(self.entries), path)
        except Exception as e:  # noqa: BLE001 - a broken cache must never break a scan
            log.warning("could not read fundamentals cache %s: %s", path, e)
        return self

    def save(self) -> None:
        path = self.cache_file
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(f"# NSE fundamentals cache (share counts / market cap), "
                         f"written {datetime.now().isoformat(timespec='seconds')}\n")
                fh.write(f"# {len(self.entries)} symbols — market cap = shares x last close, "
                         "see fpfssl/fundamentals.py\n")
                w = csv.writer(fh)
                w.writerow(_COLUMNS)
                for sym in sorted(self.entries):
                    f = self.entries[sym]
                    w.writerow([sym, f"{f.shares:.0f}", f"{f.market_cap:.0f}",
                                f"{f.price:.4f}", f.currency, f.fetched])
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            log.warning("could not write fundamentals cache %s: %s", path, e)

    # -- lookups -------------------------------------------------------------
    def get(self, symbol: str) -> Fundamentals | None:
        return self.entries.get((symbol or "").upper())

    def known(self, symbol: str) -> bool:
        f = self.get(symbol)
        return bool(f and f.has_data)

    def _needs(self, symbol: str) -> bool:
        f = self.get(symbol)
        if f is None or not f.has_data:
            return True
        if self.max_age_days <= 0:
            return False
        try:
            age_days = (datetime.now() - datetime.fromisoformat(f.fetched)).total_seconds() / 86400.0
        except (TypeError, ValueError):
            return True          # unparseable stamp -> refresh it
        return age_days > self.max_age_days

    # -- priming (the only network step) -------------------------------------
    def prime(self, symbols: list[str], *, refresh: bool = False,
              fail_open: bool = True, cfg: AppConfig | None = None) -> int:
        """Fetch share counts for the symbols the cache cannot answer. Returns the count.

        Never raises: if the feed is unreachable the table simply stays
        incomplete and the filters fail open for those symbols (a loud warning,
        no silently missed alerts).
        """
        todo = sorted({(s or "").upper() for s in symbols if s} - {""})
        if not todo:
            return 0
        if not refresh:
            todo = [s for s in todo if self._needs(s)]
        if not todo:
            return 0
        if cfg is not None and (cfg.data.source or "").lower() != "yahoo":
            log.info("fundamentals: data.source=%s (offline) — no share counts fetched; "
                     "the market-cap filter runs on the cache + "
                     "scanner.market_cap_cr_overrides only", cfg.data.source)
            return 0
        threads = 8
        delay = 0.0
        if cfg is not None:
            threads = max(1, int(getattr(cfg.data, "batch_threads", 8) or 8))
            delay = float(getattr(cfg.data, "batch_delay_sec", 0.0) or 0.0)
        log.info("fundamentals: fetching share counts for %d symbol(s) "
                 "(cached %.0f day(s), one fetch per symbol, then free)",
                 len(todo), self.max_age_days)
        got = fetch_many(todo, threads=threads, delay_sec=delay)
        for sym, f in got.items():
            self.entries[sym] = f
        if got:
            self.save()
        missing = [s for s in todo if s not in got]
        if missing:
            log.warning("fundamentals: no share count for %d of %d symbol(s) — %s",
                        len(missing), len(todo),
                        "kept in the scan (fail-open; no alert can be missed)"
                        if fail_open else
                        "filtered out (scanner.keep_unknown_market_cap is false)")
        return len(got)


# ---------------------------------------------------------------------------
# yfinance fetch
# ---------------------------------------------------------------------------
def _num(v) -> float:
    try:
        if v is None:
            return 0.0
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f > 0 else 0.0      # NaN-guard, non-positive -> 0


def _fi_get(fi, *keys) -> float:
    for k in keys:
        try:
            v = fi.get(k) if hasattr(fi, "get") else getattr(fi, k, None)
        except Exception:  # noqa: BLE001 - a missing fast_info key must not abort
            v = None
        n = _num(v)
        if n:
            return n
    return 0.0


def fetch_one(symbol: str) -> Fundamentals:
    """Share count (+ reported market cap) for one .NS/.BO ticker."""
    try:
        import yfinance as yf
    except ImportError as e:  # pragma: no cover - offline installs
        raise DataError("yfinance is not installed (pip install yfinance)") from e
    sym = (symbol or "").upper()
    f = Fundamentals(symbol=sym, fetched=datetime.now().isoformat(timespec="seconds"))
    t = yf.Ticker(sym)
    try:
        fi = t.fast_info          # cheap-ish path, has shares / market_cap / currency
    except Exception as e:  # noqa: BLE001
        log.debug("%s: fast_info unavailable (%s)", sym, e)
        fi = None
    if fi is not None:
        f.shares = _fi_get(fi, "shares", "share_count", "sharesOutstanding")
        f.market_cap = _fi_get(fi, "market_cap", "marketCap")
        f.price = _fi_get(fi, "last_price", "lastPrice")
        try:
            cur = fi.get("currency") if hasattr(fi, "get") else getattr(fi, "currency", None)
            f.currency = str(cur).upper() if cur else "INR"
        except Exception:  # noqa: BLE001
            pass
    if not f.shares:
        # `Ticker.shares` = fundamentals time series (Date x Shares); newest row wins.
        try:
            sh = t.shares
            if sh is not None and len(getattr(sh, "columns", []) or []):
                col = sh.columns[0]
                series = sh[col].dropna() if hasattr(sh, "dropna") else sh.dropna()
                if len(series):
                    f.shares = _num(series.iloc[-1])
        except Exception as e:  # noqa: BLE001
            log.debug("%s: Ticker.shares unavailable (%s)", sym, e)
    if not f.shares and not f.market_cap:
        # last resort: the heavier quoteSummary payload (older yfinance keeps
        # marketCap / sharesOutstanding there; newer versions retire them).
        try:
            info = t.info or {}
        except Exception as e:  # noqa: BLE001
            log.debug("%s: Ticker.info unavailable (%s)", sym, e)
            info = {}
        if isinstance(info, dict):
            f.shares = f.shares or _num(info.get("sharesOutstanding"))
            f.market_cap = f.market_cap or _num(info.get("marketCap"))
            f.price = f.price or _num(info.get("currentPrice")
                                      or info.get("regularMarketPrice"))
            f.currency = str(info.get("currency") or f.currency or "INR").upper()
    if not f.has_data:
        raise DataError(f"{sym}: no share count or market cap available")
    return f


def fetch_many(symbols: list[str], *, threads: int = 8, delay_sec: float = 0.0,
               group_size: int = 200) -> dict[str, Fundamentals]:
    """Threaded per-symbol fundamentals fetch, paced in groups (like the bars).

    Best-effort: symbols that cannot be resolved are absent from the result —
    the caller decides (fail open) what that means for the scan.
    """
    syms = [s for s in (symbols or []) if s]
    out: dict[str, Fundamentals] = {}
    if not syms:
        return out
    size = max(1, int(group_size or 1))
    for i in range(0, len(syms), size):
        group = syms[i:i + size]
        def one(s):
            try:
                return s, fetch_one(s)
            except Exception as e:  # noqa: BLE001
                log.debug("%s: fundamentals unavailable (%s)", s, e)
                return s, None
        try:
            with ThreadPoolExecutor(max_workers=max(1, int(threads or 1))) as ex:
                for _s, f in ex.map(one, group):
                    if f is not None:
                        out[f.symbol] = f
        except Exception as e:  # noqa: BLE001
            log.warning("fundamentals batch fetch failed (%s); trying sequentially", e)
            for s in group:
                _s, f = one(s)
                if f is not None:
                    out[f.symbol] = f
        if delay_sec and i + size < len(syms):
            time.sleep(delay_sec)
    log.info("fundamentals: resolved %d/%d symbol(s)", len(out), len(syms))
    return out


__all__ = ["CRORE", "Fundamentals", "FundamentalTable", "SizeFilters",
           "fetch_one", "fetch_many"]
