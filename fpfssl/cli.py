"""Command line interface for the FPFSSL8.2 scanner / backtest.

Usage (from the repo root):
    python -m fpfssl scan [--once] [--symbols full_nse|RELIANCE.NS,TCS.NS] [--interval 1d] [--dry-run]
    python -m fpfssl backtest [--strategy essl_ob_tap|ob_tap|essl_sweep] [--interval 1d]
    python -m fpfssl report [--symbol RELIANCE.NS] [--interval 1d]
    python -m fpfssl diagnose [--symbols RELIANCE.NS] [--at "2026-09-10 12:00"]
    python -m fpfssl test-telegram
    python -m fpfssl sample-data [--bars 1500]

`full_nse` (also `all_nse` / `nse` / `*`) in the symbol list expands to the
complete NSE equity universe fetched via yfinance-compatible tickers (.NS);
see fpfssl/universe.py. The daily timeframe (1d) with raw exchange prices is
the default so scanner signals 1:1 match the TradingView indicator.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from datetime import datetime

from . import __version__
from .config import load_config
from .data import DataError
from .telegram import TelegramNotifier
from .fundamentals import SizeFilters
from .universe import expand_universe


def _set_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _expand(cfg, refresh: bool) -> None:
    """full_nse marker -> the complete NSE equity list (see fpfssl.universe)."""
    try:
        cfg.symbols = expand_universe(cfg.symbols, cfg.data, refresh=refresh)
    except DataError as e:
        print(f"Universe error: {e}", file=sys.stderr)
        sys.exit(2)


def _apply_filter_args(cfg, args) -> None:
    """CLI overrides for the size filters and the run/stop behaviour.

    Mirrors how the other overrides work (flags win over config.yaml), so the
    filters can be tried without editing the file:

        python -m fpfssl scan --once --min-mcap-cr 20000 --min-price 500
        python -m fpfssl scan --once --no-size-filters     # scan everything
    """
    sc = cfg.scanner
    v = getattr(args, "min_mcap_cr", None)
    if v is not None:
        sc.min_market_cap_cr = max(0.0, float(v))
    v = getattr(args, "min_price", None)
    if v is not None:
        sc.min_price = max(0.0, float(v))
    if getattr(args, "no_size_filters", False):
        sc.min_market_cap_cr = 0.0
        sc.min_price = 0.0
    v = getattr(args, "max_pass_minutes", None)
    if v is not None:
        sc.max_pass_minutes = max(0.0, float(v))
    if getattr(args, "exit_after_pass", False):
        sc.exit_after_pass = True
    if getattr(args, "keep_polling", False):
        sc.exit_after_pass = False


def _add_filter_args(p) -> None:
    g = p.add_argument_group("size filters / run mode")
    g.add_argument("--min-mcap-cr", dest="min_mcap_cr", type=float, metavar="CR",
                   help="scan only stocks whose market cap is above this many ₹ crore "
                        "(0 = off; overrides scanner.min_market_cap_cr)")
    g.add_argument("--min-price", dest="min_price", type=float, metavar="RS",
                   help="scan only stocks trading above this price in ₹ "
                        "(0 = off; overrides scanner.min_price)")
    g.add_argument("--no-size-filters", action="store_true",
                   help="ignore scanner.min_market_cap_cr / min_price (scan the whole universe)")
    g.add_argument("--refresh-fundamentals", action="store_true",
                   help="ignore the cached share counts (data/nse_fundamentals.csv) and "
                        "fetch them again — only needed for the market-cap filter")


def _add_run_mode_args(p) -> None:
    g = p.add_argument_group("run mode")
    g.add_argument("--exit-after-pass", dest="exit_after_pass", action="store_true",
                   help="stop as soon as one complete pass is done (default in config.yaml: "
                        "scanner.exit_after_pass) instead of polling until the close")
    g.add_argument("--keep-polling", dest="keep_polling", action="store_true",
                   help="keep the original behaviour: poll until the session ends")
    g.add_argument("--max-pass-minutes", dest="max_pass_minutes", type=float, metavar="MIN",
                   help="abort a single pass longer than this (0 = no ceiling; guards against "
                        "a stalled feed hanging the run)")


def cmd_scan(args):
    cfg = load_config(args.config)
    if args.symbols:
        cfg.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.source:
        cfg.data.source = args.source
    if args.interval:
        cfg.data.interval = args.interval
    _apply_filter_args(cfg, args)
    _expand(cfg, args.refresh_universe)
    if args.dry_run:
        cfg.telegram.dry_run = True
    if getattr(args, "no_dry_run", False):
        # explicit production intent: never let a stale config value swallow alerts
        cfg.telegram.dry_run = False
    notifier = TelegramNotifier(cfg.telegram)
    if args.max_runtime_minutes is not None:
        cfg.scanner.max_runtime_minutes = args.max_runtime_minutes
    if not notifier.cfg.dry_run and not notifier.ready:
        # fail fast: a live scanner that cannot deliver is worse than no scanner
        print("Telegram is NOT configured (token/chat_id missing), so a live scan "
              "would silently drop every alert.\nSet the TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID environment variables (or telegram.token/chat_id in "
              "config.yaml), or run with --dry-run / `diagnose` to inspect the "
              "scanner without alerts.", file=sys.stderr)
        sys.exit(2)
    if notifier.cfg.dry_run:
        print("dry-run: Telegram messages will be printed, not sent.", file=sys.stderr)
    from .scanner import LiveScanner
    sc = LiveScanner(cfg, notifier, refresh_fundamentals=args.refresh_fundamentals)
    if args.once:
        sc.scan_once()
        return
    try:
        reason = sc.run_forever()
    except KeyboardInterrupt:
        # signal handlers are installed inside run_forever, so this only
        # catches an interrupt that landed before they were in place
        sc.request_stop("interrupted")
        reason = "interrupted"
    print(f"scanner stopped: {reason}")


def cmd_backtest(args):
    cfg = load_config(args.config)
    if args.symbols:
        cfg.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.source:
        cfg.data.source = args.source
    if args.interval:
        cfg.data.interval = args.interval
    _expand(cfg, args.refresh_universe)
    if args.strategy:
        cfg.backtest.strategy = args.strategy
    if args.rr is not None:
        cfg.backtest.rr = args.rr
    if args.start:
        cfg.backtest.start = args.start
        cfg.data.start = args.start
    if args.bars:
        cfg.data.history_bars = args.bars
    from .backtest import format_summary, run_backtest
    out = os.path.join("output", f"backtest_{cfg.backtest.strategy}_{cfg.data.interval}_{_stamp()}")
    rep = run_backtest(cfg.symbols, cfg.data, cfg.engine, cfg.backtest, out_dir=out)
    print()
    print(format_summary(rep.summary))
    if len(rep.per_symbol):
        print("\nPer-symbol:")
        print(rep.per_symbol.to_string(index=False))
    print(f"\nReports written to {out}/ (trades.csv, equity.csv, events.csv, summary.txt)")


def cmd_report(args):
    cfg = load_config(args.config)
    if args.source:
        cfg.data.source = args.source
    if args.interval:
        cfg.data.interval = args.interval
    from .data import DataError, load_symbol
    from .engine import Engine, detect_tick, summarize_state
    from .scanner import is_live_last_bar, market_now, timeframe_label
    if args.symbol:
        cfg.symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
    _expand(cfg, args.refresh_universe)
    syms = cfg.symbols
    tf = timeframe_label(cfg.data.interval)
    for sym in syms:
        try:
            df = load_symbol(sym, cfg.data)
        except DataError as e:
            print(f"{sym}: {e}")
            continue
        tick = cfg.data.tick_overrides.get(sym, detect_tick(df, sym))
        live_last = is_live_last_bar(cfg, df.index[-1], market_now(cfg))
        res = Engine(sym, cfg.engine, tick, tf=cfg.data.interval).run(df, live_last_bar=live_last)
        st = summarize_state(res)
        last = res.dates[-1]
        px = float(df['close'].iloc[-1])
        print(f"\n{'='*62}\n{sym} [{tf}] — last bar {last} close {px:.2f} (tick {tick})"
              + ("  LIVE" if live_last else ""))
        print(f"counters: {res.counters}")
        print(f"pending setups: {st['pending_setups']}")
        if st['active_zones']:
            print("active FP-OBs:")
            for z in st['active_zones']:
                print(f"  #{z['id']} {z['bottom']:.2f}-{z['top']:.2f} ref {z['reference']:.2f} "
                      f"stop {z['invalidation']:.2f} taps {z['taps']} state {z['state']} "
                      f"departed {z['departed']} born {z['born']}")
        else:
            print("active FP-OBs: none")
        if st['e_ssl']:
            print("active eSSL levels:")
            for p in st['e_ssl']:
                print(f"  #{p['id']} {p['level']:.2f} ({p['members']} member(s)) {p['state']}")
        if st['i_ssl']:
            print("active iSSL levels:")
            for p in st['i_ssl']:
                print(f"  #{p['id']} {p['level']:.2f} ({p['members']} member(s)) {p['state']}")
        if st['fresh']:
            print("FRESH confirmed lows (unswept):")
            for r in st['fresh'][-5:]:
                print(f"  {r['price']:.2f} origin {r['origin']} major={r['major']}")
        recent = res.events[-15:]
        if recent:
            print("recent events:")
            for e in recent:
                print(f"  {e.date} {e.kind:14s} z{e.zone_id or '-':>3} p{e.pool_id or '-':>3} "
                      f"px={e.price if e.price is None else round(e.price,2)} conf={e.confirmed}")


def cmd_test_telegram(args):
    cfg = load_config(args.config)
    if args.dry_run:
        cfg.telegram.dry_run = True
    if getattr(args, "no_dry_run", False):
        cfg.telegram.dry_run = False
    n = TelegramNotifier(cfg.telegram)
    if not n.ready and not cfg.telegram.dry_run:
        print("Telegram not configured. Set telegram.token/chat_id in config.yaml or env vars\n"
              "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. (Create a bot with @BotFather, get your\n"
              "chat id from @userinfobot.)")
        sys.exit(1)
    from .scanner import timeframe_label
    tf = timeframe_label(cfg.data.interval)
    ok = n.send("<b>FPFSSL8.2</b> test message — Telegram connection works ✅\n"
                f"{tf}-TF eSSL tap + footprint alerts will arrive in this format.")
    print("sent" if ok else "send failed")


def cmd_diagnose(args):
    """Explain the live state of every watched symbol (armed references, filters)."""
    cfg = load_config(args.config)
    if args.symbols:
        cfg.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.source:
        cfg.data.source = args.source
    if args.interval:
        cfg.data.interval = args.interval
    if args.bars:
        cfg.data.max_bars = args.bars
    _apply_filter_args(cfg, args)
    _expand(cfg, args.refresh_universe)
    from .diag import format_diag_many, run_diag
    now = None
    if args.at:
        try:
            now = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                now = datetime.strptime(args.at, "%Y-%m-%d")
            except ValueError:
                print(f"--at must be 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD' (got {args.at!r})",
                      file=sys.stderr)
                sys.exit(2)
    live = True if args.live else None
    diags = run_diag(cfg, list(cfg.symbols), now=now, live=live,
                     refresh_fundamentals=getattr(args, "refresh_fundamentals", False),
                     max_symbols=int(getattr(args, "max_symbols", 0) or 0))
    n_fl = sum(1 for d in diags if d.status == "filtered")
    if n_fl and not args.json:
        print(f"size filters ({SizeFilters.from_config(cfg.scanner).describe()}): "
              f"{n_fl} of {len(diags)} symbol(s) filtered out (not scanned by the "
              f"scanner either)")
    print(format_diag_many(diags, as_json=args.json))
    if any(d.status != "ok" for d in diags):
        sys.exit(0)


def cmd_sample_data(args):
    cfg = load_config(args.config)
    if args.bars:
        cfg.data.history_bars = args.bars
    os.makedirs(cfg.data.csv_dir, exist_ok=True)
    # full_nse for the offline generator means the built-in demo list
    from .config import AppConfig as _AC
    from .universe import UNIVERSE_MARKERS
    if any(str(s).strip().lower() in UNIVERSE_MARKERS for s in cfg.symbols):
        cfg.symbols = ([s for s in cfg.symbols
                        if str(s).strip().lower() not in UNIVERSE_MARKERS]
                       + list(_AC().symbols))
    from .synthetic import generate
    for sym in cfg.symbols:
        df = generate(sym, cfg.data)
        path = os.path.join(cfg.data.csv_dir, f"{sym}.csv")
        df.to_csv(path)
        print(f"wrote {path} ({len(df)} bars, {df.index[0].date()} → {df.index[-1].date()})")
    print("\nNow run:\n  python -m fpfssl backtest --source csv\n  python -m fpfssl scan --once --source csv --dry-run")


def _stamp():
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def main(argv=None):
    p = argparse.ArgumentParser(prog="fpfssl", description="FOOTPRINT ESSL — Python scanner & backtest")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="live scanner with Telegram alerts")
    s.add_argument("--once", action="store_true", help="single pass, then exit")
    s.add_argument("--symbols", help="comma-separated override (also accepts the full_nse marker)")
    s.add_argument("--source", choices=["yahoo", "csv", "synthetic"])
    s.add_argument("--interval", help="yfinance TF: 1m,5m,15m,30m,1h,1d,1wk,1mo (overrides config)")
    s.add_argument("--refresh-universe", action="store_true",
                   help="ignore the cached full-NSE list and fetch it again")
    s.add_argument("--dry-run", action="store_true", help="print Telegram messages instead of sending")
    s.add_argument("--no-dry-run", dest="no_dry_run", action="store_true",
                   help="force real Telegram sends even if telegram.dry_run is true in config")
    s.add_argument("--max-runtime-minutes", type=float, metavar="MIN",
                   help="stop polling after this many minutes even if the session is "
                        "still open (0 = no limit; CI sets it below the job timeout)")
    _add_filter_args(s)
    _add_run_mode_args(s)
    s.set_defaults(fn=cmd_scan)

    b = sub.add_parser("backtest", help="backtest the alert signals")
    b.add_argument("--strategy", choices=["essl_ob_tap", "ob_tap", "essl_sweep"])
    b.add_argument("--rr", type=float, help="take-profit in R multiples (0 disables)")
    b.add_argument("--symbols", help="comma-separated override (also accepts the full_nse marker)")
    b.add_argument("--source", choices=["yahoo", "csv", "synthetic"])
    b.add_argument("--interval", help="yfinance TF (overrides config)")
    b.add_argument("--start", help="YYYY-MM-DD")
    b.add_argument("--bars", type=int, help="max bars per symbol")
    b.add_argument("--refresh-universe", action="store_true",
                   help="ignore the cached full-NSE list and fetch it again")
    b.set_defaults(fn=cmd_backtest)

    r = sub.add_parser("report", help="current zones / eSSL levels / recent events")
    r.add_argument("--symbol", help="one symbol or comma list (default: all; also accepts the full_nse marker)")
    r.add_argument("--source", choices=["yahoo", "csv", "synthetic"])
    r.add_argument("--interval", help="yfinance TF (overrides config)")
    r.add_argument("--refresh-universe", action="store_true",
                   help="ignore the cached full-NSE list and fetch it again")
    r.set_defaults(fn=cmd_report)

    t = sub.add_parser("test-telegram", help="send a test Telegram message")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--no-dry-run", dest="no_dry_run", action="store_true",
                   help="force a real send even if telegram.dry_run is true in config")
    t.set_defaults(fn=cmd_test_telegram)

    g = sub.add_parser("diagnose", help="why is the scanner (not) alerting? live state per symbol")
    g.add_argument("--symbols", help="comma-separated override (default: config.yaml; also accepts the full_nse marker)")
    g.add_argument("--source", choices=["yahoo", "csv", "synthetic"])
    g.add_argument("--interval", help="yfinance TF (overrides config)")
    g.add_argument("--at", help="pretend the market clock is this 'YYYY-MM-DD HH:MM' (IST)")
    g.add_argument("--live", action="store_true",
                   help="treat the last bar as a still-forming (LIVE) bar")
    g.add_argument("--json", action="store_true", help="machine-readable output")
    g.add_argument("--bars", type=int, help="cap bars per symbol (default: everything the feed gives)")
    g.add_argument("--refresh-universe", action="store_true",
                   help="ignore the cached full-NSE list and fetch it again")
    g.add_argument("--max-symbols", type=int, metavar="N",
                   help="run the engine for at most N symbols — the biggest ones that pass "
                        "the size filters first (bounds a full-NSE diagnose in CI; every "
                        "symbol still gets its filter verdict)")
    _add_filter_args(g)
    g.set_defaults(fn=cmd_diagnose)

    d = sub.add_parser("sample-data", help="generate offline synthetic CSVs into data/")
    d.add_argument("--bars", type=int)
    d.set_defaults(fn=cmd_sample_data)

    args = p.parse_args(argv)
    _set_logging(args.verbose)
    # machine-readable output must be pure JSON — no banner line
    if not (args.cmd == "diagnose" and getattr(args, "json", False)):
        print(f"fpfssl {__version__} — FOOTPRINT ESSL v8.2 port")
    args.fn(args)


if __name__ == "__main__":
    main()
