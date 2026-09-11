"""Scanner size filters (market cap / price) + stop-after-pass tests (offline).

Covers `scanner.min_market_cap_cr` / `scanner.min_price` /
`keep_unknown_market_cap` / `market_cap_cr_overrides` and
`scanner.exit_after_pass` + `scanner.max_pass_minutes`:

  1. a stock below the price floor is skipped BEFORE the engine runs
  2. a stock below the market-cap floor is skipped; above it, it alerts
  3. unknown market cap fails OPEN (kept) — or filtered when told not to
  4. `market_cap_cr_overrides` pins a symbol offline
  5. alerts carry the 📏 size footer, and no footer when the filters are off
  6. the share-count cache round-trips and drives `prime()` (only missing
     symbols are fetched, results are saved)
  7. market cap = shares x last close in ₹ crore
  8. `diagnose` states the same verdict (status "filtered") without running the
     engine, and `--max-symbols` bounds the engine work to the biggest names
  9. `exit_after_pass: true` -> one pass, then the run ends (was: poll to close)
 10. `max_pass_minutes` -> a stalled feed cannot hang the pass (or the process)

Run:  .venv/bin/python tests/test_size_filters.py      (or via pytest)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from fpfssl.config import AppConfig, DataConfig
from fpfssl import fundamentals as FU
from fpfssl.fundamentals import CRORE, Fundamentals, FundamentalTable

import fpfssl.scanner as scanner

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_engine import composite_rows, make_df  # noqa: E402

SYM = "RELIANCE.NS"
LAST_CLOSE = float(make_df(composite_rows())["close"].iloc[-1])   # ~103 ₹
_ORIG_LOAD = scanner.load_symbol
_ORIG_NOW = scanner.market_now

# bars of the shared composite fixture: TAP 1 + eSSL tap on bar 60 alerts
DF = make_df(composite_rows())


def _restore():
    scanner.load_symbol = _ORIG_LOAD
    scanner.market_now = _ORIG_NOW


class Recorder:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def mk_cfg(fundamentals: FundamentalTable | None = None, **sc_kw) -> AppConfig:
    cfg = AppConfig()
    cfg.symbols = [SYM]
    cfg.data = DataConfig(source="yahoo", interval="1d", history_bars=100)
    cfg.data.fundamentals_cache_file = os.path.join(
        tempfile.mkdtemp(prefix="fpfssl-size-"), "nse_fundamentals.csv")
    cfg.scanner.min_bars = 10
    cfg.scanner.provisional_alerts = True
    cfg.scanner.alert_cooldown_minutes = 0
    cfg.scanner.recent_bars = 1
    cfg.scanner.alert_events = ["essl_ob_tap", "footprint_tap"]
    cfg.scanner.state_file = os.path.join(
        tempfile.mkdtemp(prefix="fpfssl-size-"), "scanner_state.json")
    for k, v in sc_kw.items():
        setattr(cfg.scanner, k, v)
    return cfg


def scan_last_bar(cfg: AppConfig, fundamentals: FundamentalTable | None = None,
                  end_bar: int = 60) -> tuple[Recorder, scanner.LiveScanner]:
    """One `scan_symbol` pass over df[:end_bar+1] with the last bar confirmed."""
    _restore()
    frame = DF.iloc[: end_bar + 1]
    last = pd.Timestamp(frame.index[-1]).to_pydatetime()
    now = last.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.weekday() >= 5:
        now = last + timedelta(hours=2)
    rec = Recorder()
    sc = scanner.LiveScanner(cfg, rec, symbols=[SYM], fundamentals=fundamentals)
    scanner.load_symbol = lambda sym, d: frame
    scanner.market_now = lambda c: now
    try:
        sc.scan_symbol(SYM)
    finally:
        _restore()
    return rec, sc


def big_table(cr: float = 250_000.0) -> FundamentalTable:
    return FundamentalTable.for_test({SYM: cr})


# ---------------------------------------------------------------------------
# 1-2. the filters themselves
# ---------------------------------------------------------------------------
def test_price_floor_skips_before_the_engine():
    cfg = mk_cfg(min_price=LAST_CLOSE + 50.0, min_market_cap_cr=0.0)
    rec, sc = scan_last_bar(cfg, big_table())
    assert rec.messages == [], rec.messages
    assert sc.filtered == 1 and sc.scanned == 0, (sc.filtered, sc.scanned)
    print(f"ok test_price_floor_skips_before_the_engine (close {LAST_CLOSE:.2f} "
          f"< floor {LAST_CLOSE + 50:.2f})")


def test_price_floor_keeps_a_bigger_stock():
    cfg = mk_cfg(min_price=50.0, min_market_cap_cr=0.0)
    rec, sc = scan_last_bar(cfg, big_table())
    assert sc.filtered == 0 and sc.scanned == 1, (sc.filtered, sc.scanned)
    assert any("ALL RULES MATCH" in m for m in rec.messages), rec.messages
    print("ok test_price_floor_keeps_a_bigger_stock")


def test_market_cap_floor_filters_small_cap():
    # price clears the floor, market cap does not: the eSSL/footprint alert
    # must NOT be sent, and the reason must name the market cap
    cfg = mk_cfg(min_price=0.0, min_market_cap_cr=1000.0)
    tbl = FundamentalTable.for_test({SYM: 500.0})
    rec, sc = scan_last_bar(cfg, tbl)
    assert rec.messages == [], rec.messages
    assert sc.filtered == 1 and sc.scanned == 0, (sc.filtered, sc.scanned)
    keep, why = sc.size_ok(SYM, LAST_CLOSE)
    assert not keep and "market cap ₹500 Cr" in why, why
    print(f"ok test_market_cap_floor_filters_small_cap ({why})")


def test_market_cap_floor_keeps_large_cap():
    # ₹1,000 Cr exactly is filtered (strictly greater), ₹1,001 is kept
    cfg = mk_cfg(min_price=0.0, min_market_cap_cr=1000.0)
    rec_eq, sc_eq = scan_last_bar(cfg, FundamentalTable.for_test({SYM: 1000.0}))
    assert rec_eq.messages == [] and sc_eq.filtered == 1, rec_eq.messages
    rec, sc = scan_last_bar(cfg, FundamentalTable.for_test({SYM: 100_000.0}))
    assert any("ALL RULES MATCH" in m for m in rec.messages), rec.messages
    assert sc.scanned == 1 and sc.filtered == 0
    print("ok test_market_cap_floor_keeps_large_cap")


# ---------------------------------------------------------------------------
# 3-4. unknown market cap + overrides
# ---------------------------------------------------------------------------
def test_unknown_market_cap_fails_open():
    cfg = mk_cfg(min_price=0.0, min_market_cap_cr=1000.0,
                 keep_unknown_market_cap=True)
    rec, sc = scan_last_bar(cfg, FundamentalTable())   # empty cache
    assert sc.scanned == 1, "a symbol without a share count must still be scanned"
    assert any("ALL RULES MATCH" in m for m in rec.messages), rec.messages
    print("ok test_unknown_market_cap_fails_open")


def test_unknown_market_cap_can_be_filtered_when_asked():
    cfg = mk_cfg(min_price=0.0, min_market_cap_cr=1000.0,
                 keep_unknown_market_cap=False)
    rec, sc = scan_last_bar(cfg, FundamentalTable())
    assert rec.messages == [] and sc.filtered == 1, rec.messages
    print("ok test_unknown_market_cap_can_be_filtered_when_asked")


def test_market_cap_override_pins_a_symbol():
    cfg = mk_cfg(min_price=0.0, min_market_cap_cr=1000.0,
                 market_cap_cr_overrides={SYM: 42.0})
    rec, sc = scan_last_bar(cfg, FundamentalTable.for_test({SYM: 90_000.0}))
    assert rec.messages == [] and sc.filtered == 1, rec.messages
    # and the same pin from above the floor keeps it in
    cfg2 = mk_cfg(min_price=0.0, min_market_cap_cr=1000.0,
                  market_cap_cr_overrides={SYM: 5_000.0})
    rec2, sc2 = scan_last_bar(cfg2, FundamentalTable())
    assert rec2.messages, "an override above the floor must be scanned"
    print("ok test_market_cap_override_pins_a_symbol")


# ---------------------------------------------------------------------------
# 5. alert footer
# ---------------------------------------------------------------------------
def test_alert_carries_size_footer_only_when_filters_are_on():
    cfg = mk_cfg(min_price=50.0, min_market_cap_cr=1000.0)
    rec, _ = scan_last_bar(cfg, FundamentalTable.for_test({SYM: 250_000.0}))
    assert rec.messages, rec.messages
    assert all("📏 <b>size</b>" in m and "price ₹" in m and "mcap ₹250,000 Cr" in m
               for m in rec.messages), rec.messages[0]
    cfg_off = mk_cfg(min_price=0.0, min_market_cap_cr=0.0)
    rec_off, _ = scan_last_bar(cfg_off, FundamentalTable())
    assert rec_off.messages and all("📏" not in m for m in rec_off.messages)
    print("ok test_alert_carries_size_footer_only_when_filters_are_on")


# ---------------------------------------------------------------------------
# 6-7. the fundamentals table
# ---------------------------------------------------------------------------
def test_market_cap_is_shares_times_price_in_crore():
    f = Fundamentals(symbol="X.NS", shares=67.7e8, price=1000.0, currency="INR")
    assert abs(f.market_cap_cr() - 67.7e8 * 1000.0 / CRORE) < 1e-6
    # recomputed at today's price (that is the whole point of the design)
    assert abs(f.market_cap_cr(2000.0) - 2 * f.market_cap_cr()) < 1e-6
    assert Fundamentals(symbol="Y.NS").market_cap_cr(500.0) == 0.0   # unknown
    print("ok test_market_cap_is_shares_times_price_in_crore")


def test_cache_roundtrip_and_prime_only_fetches_missing():
    tmp = tempfile.mkdtemp(prefix="fpfssl-size-")
    path = os.path.join(tmp, "nse_fundamentals.csv")
    tbl = FundamentalTable(cache_file=path, max_age_days=30.0)
    tbl.entries["AAA.NS"] = Fundamentals(symbol="AAA.NS", shares=50e7, price=100.0,
                                        currency="INR",
                                        fetched=datetime.now().isoformat(timespec="seconds"))
    tbl.save()
    again = FundamentalTable(cache_file=path, max_age_days=30.0).load()
    assert again.known("AAA.NS"), "cached share count must survive a reload"
    assert abs(again.get("AAA.NS").market_cap_cr(100.0) - 50e7 * 100 / CRORE) < 1e-6
    assert not again.known("BBB.NS") and again._needs("BBB.NS")
    assert not again._needs("AAA.NS"), "a fresh entry must not be refetched"
    # an entry older than max_age_days is due for a refresh
    again.entries["OLD.NS"] = Fundamentals(
        symbol="OLD.NS", shares=1e6, price=10.0,
        fetched=(datetime.now() - timedelta(days=31)).isoformat(timespec="seconds"))
    assert again._needs("OLD.NS")

    asked: list[list[str]] = []
    orig_fetch_many = FU.fetch_many

    def fake_fetch_many(symbols, **kw):
        asked.append(list(symbols))
        return {s: Fundamentals(symbol=s, shares=20e7, price=200.0, currency="INR",
                                fetched=datetime.now().isoformat(timespec="seconds"))
                for s in symbols}

    FU.fetch_many = fake_fetch_many
    try:
        n = again.prime(["AAA.NS", "BBB.NS", "OLD.NS"], cfg=None)
    finally:
        FU.fetch_many = orig_fetch_many
    assert n == 2, n
    assert asked == [["BBB.NS", "OLD.NS"]], asked  # AAA.NS was fresh -> not fetched
    reloaded = FundamentalTable(cache_file=path, max_age_days=30.0).load()
    assert reloaded.known("BBB.NS"), "primed entries must be persisted"
    print("ok test_cache_roundtrip_and_prime_only_fetches_missing")


def test_non_inr_and_broken_prices_are_no_verdict():
    # a USD-listed symbol cannot be judged by a ₹ crore threshold -> kept (the
    # price filter still applies to it)
    flt = FU.SizeFilters(min_market_cap_cr=1000.0, min_price=100.0)
    tbl = FundamentalTable({"AAPL": Fundamentals(symbol="AAPL", shares=1.5e10,
                                                 price=200.0, currency="USD")})
    keep, why = flt.check("AAPL", 500.0, tbl)
    assert keep and why == "", (keep, why)
    assert flt.check("AAPL", 50.0, tbl)[0] is False, "price filter still applies"
    # a NaN / zero close is a data hiccup, not a cheap stock -> never filtered
    assert flt.check(SYM, float("nan"), big_table())[0] is True
    assert flt.check(SYM, 0.0, big_table())[0] is True
    # one warning per symbol, not one per pass
    assert len(flt.warned) == 1, flt.warned
    print("ok test_non_inr_and_broken_prices_are_no_verdict")


def test_filters_describe_and_disabled():
    off = FU.SizeFilters.from_config(AppConfig().scanner)
    assert not off.enabled and off.describe() == "none"
    cfg = mk_cfg()
    cfg.scanner.min_market_cap_cr = 1000.0
    cfg.scanner.min_price = 100.0
    on = FU.SizeFilters.from_config(cfg.scanner)
    assert on.enabled and "₹1,000 Cr" in on.describe() and "₹100.00" in on.describe()
    print("ok test_filters_describe_and_disabled")


def test_diagnose_reports_the_verdict_instead_of_the_engine():
    """`diagnose` states the filter verdict and skips the engine for it."""
    from fpfssl.diag import run_diag

    tmp = tempfile.mkdtemp(prefix="fpfssl-size-")
    path = os.path.join(tmp, "nse_fundamentals.csv")
    pinned = FundamentalTable.for_test({SYM: 40.0})   # a tiny float: 40 Cr
    pinned.cache_file = path
    pinned.save()
    assert os.path.exists(path), "the pinned cache must be on disk for the report to read"
    cfg = mk_cfg(min_price=0.0, min_market_cap_cr=1000.0)
    cfg.data.source = "synthetic"
    cfg.data.fundamentals_cache_file = path
    cfg.data.history_bars = 60
    cfg.scanner.min_bars = 10
    diags = run_diag(cfg, [SYM])
    assert len(diags) == 1, diags
    d = diags[0]
    assert d.status == "filtered", (d.status, d.skip_reason)
    assert "market cap ₹40 Cr" in d.skip_reason, d.skip_reason
    assert d.counters == {}, "a filtered symbol must not have run the engine"
    print(f"ok test_diagnose_reports_the_verdict_instead_of_the_engine ({d.skip_reason})")


def test_diagnose_cap_runs_the_engine_on_the_biggest_names_only():
    """`--max-symbols` bounds the expensive part and keeps the verdicts."""
    from fpfssl import diag as DIAG
    prices = {"BIG.NS": 900.0, "MID.NS": 400.0, "SMALL.NS": 120.0}
    frames = {s: make_df([(p, p * 1.01, p * 0.99, p, 1000.0)] * 60)
              for s, p in prices.items()}
    orig = DIAG.load_all
    DIAG.load_all = lambda syms, d: frames
    try:
        cfg = mk_cfg(min_price=0.0, min_market_cap_cr=0.0)
        now = pd.Timestamp(frames["MID.NS"].index[-1]).to_pydatetime().replace(hour=17)
        diags = DIAG.run_diag(cfg, list(prices), now=now, max_symbols=1)
    finally:
        DIAG.load_all = orig
    assert diags[0].symbol == "BIG.NS", [d.symbol for d in diags]
    assert diags[0].counters, "the ranked symbol must get a real engine pass"
    assert {d.symbol: d.status for d in diags[1:]} == {"MID.NS": "capped",
                                                        "SMALL.NS": "capped"}, diags
    print("ok test_diagnose_cap_runs_the_engine_on_the_biggest_names_only")


# ---------------------------------------------------------------------------
# 8. stop after a pass
# ---------------------------------------------------------------------------
def _patch_clock(open_now: bool = True):
    scanner.market_is_open = lambda cfg, now=None: open_now
    scanner.market_now = lambda c: datetime(2026, 9, 11, 12, 0)
    scanner._session_finished = lambda cfg, now: False
    scanner.minutes_until_close = lambda cfg, now=None: 120.0
    scanner.minutes_until_open = lambda cfg, now=None: 999.0


def test_exit_after_pass_ends_the_run():
    orig = (scanner.market_is_open, scanner.market_now, scanner._session_finished,
            scanner.minutes_until_close, scanner.minutes_until_open,
            scanner.LiveScanner.install_stop_handlers,
            scanner.LiveScanner.restore_stop_handlers)
    _patch_clock()
    scanner.LiveScanner.install_stop_handlers = lambda self: None
    scanner.LiveScanner.restore_stop_handlers = lambda self: None
    try:
        calls = []
        sc = scanner.LiveScanner(mk_cfg(exit_after_pass=True, max_pass_minutes=0.0),
                                 Recorder(), symbols=[SYM])
        sc.scan_once = lambda: calls.append(1) or 0
        reason = sc.run_forever()
        assert len(calls) == 1, f"exit_after_pass must stop after ONE pass ({calls})"
        assert "exit_after_pass" in reason, reason
        print(f"ok test_exit_after_pass_ends_the_run ({reason})")

        # ...while the old behaviour keeps polling (3 passes, then cancelled)
        calls2 = []
        naps = []

        def fake_sleep(self, seconds):
            naps.append(seconds)
            if len(calls2) >= 3:
                self.request_stop("cancel")
                return False
            return True

        sc2 = scanner.LiveScanner(mk_cfg(exit_after_pass=False, max_pass_minutes=0.0),
                                  Recorder(), symbols=[SYM])
        sc2.scan_once = lambda: calls2.append(1) or 0
        scanner.LiveScanner._sleep = fake_sleep
        try:
            reason2 = sc2.run_forever()
        finally:
            del scanner.LiveScanner._sleep
        assert len(calls2) == 3, calls2
        assert naps, "polling mode must have napped between passes"
        assert "cancel" in reason2, reason2
        print(f"ok test_polling_mode_still_polls ({len(calls2)} passes, {reason2})")
    finally:
        (scanner.market_is_open, scanner.market_now, scanner._session_finished,
         scanner.minutes_until_close, scanner.minutes_until_open,
         scanner.LiveScanner.install_stop_handlers,
         scanner.LiveScanner.restore_stop_handlers) = orig


# ---------------------------------------------------------------------------
# 9. a stalled feed cannot hang the pass
# ---------------------------------------------------------------------------
def test_max_pass_minutes_abandons_a_stalled_fetch():
    orig_load_all = scanner.load_all
    cfg = mk_cfg(min_price=0.0, min_market_cap_cr=0.0, max_pass_minutes=0.05,
                 exit_after_pass=False)

    def stalling_load_all(symbols, dcfg):
        time.sleep(30)          # longer than the 3-second pass ceiling
        return {}

    cfg.symbols = [SYM, "TCS.NS"]      # >1 symbol -> the batch path is used
    cfg.data.batch = True
    scanner.load_all = stalling_load_all
    sc = scanner.LiveScanner(cfg, Recorder(), symbols=cfg.symbols)
    t0 = time.time()
    try:
        total = sc.scan_once()
    finally:
        scanner.load_all = orig_load_all
    dur = time.time() - t0
    assert total == 0
    assert dur < 6.0, f"pass should be cut at the ceiling, took {dur:.1f}s"
    assert sc.stop_requested and "stalled" in sc.stop_reason, sc.stop_reason
    print(f"ok test_max_pass_minutes_abandons_a_stalled_fetch (returned in {dur:.2f}s)")


ALL = [
    test_price_floor_skips_before_the_engine,
    test_price_floor_keeps_a_bigger_stock,
    test_market_cap_floor_filters_small_cap,
    test_market_cap_floor_keeps_large_cap,
    test_unknown_market_cap_fails_open,
    test_unknown_market_cap_can_be_filtered_when_asked,
    test_market_cap_override_pins_a_symbol,
    test_alert_carries_size_footer_only_when_filters_are_on,
    test_market_cap_is_shares_times_price_in_crore,
    test_cache_roundtrip_and_prime_only_fetches_missing,
    test_non_inr_and_broken_prices_are_no_verdict,
    test_diagnose_reports_the_verdict_instead_of_the_engine,
    test_diagnose_cap_runs_the_engine_on_the_biggest_names_only,
    test_filters_describe_and_disabled,
    test_exit_after_pass_ends_the_run,
    test_max_pass_minutes_abandons_a_stalled_fetch,
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
        finally:
            _restore()
    if failed:
        sys.exit(1)
    print(f"\nAll {len(ALL)} size-filter / run-mode tests passed.")


if __name__ == "__main__":
    main()
