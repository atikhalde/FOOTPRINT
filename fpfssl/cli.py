"""Command line interface for the FPFSSL8.2 scanner / backtest.

Usage (from the repo root):
    python -m fpfssl scan [--once] [--symbols RELIANCE.NS,TCS.NS] [--interval 15m] [--dry-run]
    python -m fpfssl backtest [--strategy essl_ob_tap|ob_tap|essl_sweep] [--interval 15m]
    python -m fpfssl report [--symbol RELIANCE.NS] [--interval 15m]
    python -m fpfssl test-telegram
    python -m fpfssl sample-data [--bars 1500]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .config import load_config
from .telegram import TelegramNotifier


def _set_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_scan(args):
    cfg = load_config(args.config)
    if args.symbols:
        cfg.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.source:
        cfg.data.source = args.source
    if args.interval:
        cfg.data.interval = args.interval
    if args.dry_run:
        cfg.telegram.dry_run = True
    notifier = TelegramNotifier(cfg.telegram)
    from .scanner import LiveScanner
    sc = LiveScanner(cfg, notifier)
    if args.once:
        sc.scan_once()
    else:
        sc.run_forever()


def cmd_backtest(args):
    cfg = load_config(args.config)
    if args.symbols:
        cfg.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.source:
        cfg.data.source = args.source
    if args.interval:
        cfg.data.interval = args.interval
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
    syms = [s.strip().upper() for s in args.symbol.split(",")] if args.symbol else cfg.symbols
    tf = timeframe_label(cfg.data.interval)
    for sym in syms:
        try:
            df = load_symbol(sym, cfg.data).tail(cfg.data.history_bars)
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


def cmd_sample_data(args):
    cfg = load_config(args.config)
    if args.bars:
        cfg.data.history_bars = args.bars
    os.makedirs(cfg.data.csv_dir, exist_ok=True)
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
    s.add_argument("--symbols", help="comma-separated override")
    s.add_argument("--source", choices=["yahoo", "csv", "synthetic"])
    s.add_argument("--interval", help="yfinance TF: 1m,5m,15m,30m,1h,1d,1wk,1mo (overrides config)")
    s.add_argument("--dry-run", action="store_true", help="print Telegram messages instead of sending")
    s.set_defaults(fn=cmd_scan)

    b = sub.add_parser("backtest", help="backtest the alert signals")
    b.add_argument("--strategy", choices=["essl_ob_tap", "ob_tap", "essl_sweep"])
    b.add_argument("--rr", type=float, help="take-profit in R multiples (0 disables)")
    b.add_argument("--symbols", help="comma-separated override")
    b.add_argument("--source", choices=["yahoo", "csv", "synthetic"])
    b.add_argument("--interval", help="yfinance TF (overrides config)")
    b.add_argument("--start", help="YYYY-MM-DD")
    b.add_argument("--bars", type=int, help="max bars per symbol")
    b.set_defaults(fn=cmd_backtest)

    r = sub.add_parser("report", help="current zones / eSSL levels / recent events")
    r.add_argument("--symbol", help="one symbol or comma list (default: all)")
    r.add_argument("--source", choices=["yahoo", "csv", "synthetic"])
    r.add_argument("--interval", help="yfinance TF (overrides config)")
    r.set_defaults(fn=cmd_report)

    t = sub.add_parser("test-telegram", help="send a test Telegram message")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(fn=cmd_test_telegram)

    d = sub.add_parser("sample-data", help="generate offline synthetic CSVs into data/")
    d.add_argument("--bars", type=int)
    d.set_defaults(fn=cmd_sample_data)

    args = p.parse_args(argv)
    _set_logging(args.verbose)
    print(f"fpfssl {__version__} — FOOTPRINT ESSL v8.2 port")
    args.fn(args)


if __name__ == "__main__":
    main()
