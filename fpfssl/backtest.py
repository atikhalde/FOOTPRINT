"""Backtest engine for FPFSSL8.2 daily signals.

Strategies (entry on the NEXT bar's open by default — the signal is known at
the bar close, exactly as a daily-TF user acts):

  essl_ob_tap  (default)  price TAPPED an eSSL level on a bar where a
                          footprint-source TAP also fired = the "ALL RULES"
                          live alert condition.
  ob_tap                 source-compatible TAP on any confirmed FP-OB
  essl_sweep             confirmed eSSL penetration WITH close reclaim
                          (liquidity grab at the external low)

Exits: fixed stop (OB invalidation, or level - k*ATR for sweeps), optional
take-profit in R multiples, time exit, end-of-data close-out.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import BacktestConfig, DataConfig, EngineConfig
from .data import load_symbol
from .engine import Engine, detect_tick
from .events import K_ESSL_SWEEP, K_ESSL_TAP, K_TAP


@dataclass
class Trade:
    symbol: str
    strategy: str
    entry_date: str
    entry_price: float
    stop: float
    target: float
    exit_date: str
    exit_price: float
    exit_reason: str
    bars_held: int
    r_multiple: float
    pnl_pct: float
    zone_id: int | None = None
    pool_id: int | None = None
    tap_number: int = 0
    signals: str = ""


@dataclass
class BacktestReport:
    strategy: str
    trades: list[Trade]
    equity: pd.DataFrame
    summary: dict
    per_symbol: pd.DataFrame
    out_dir: str = ""


def _signals_by_bar(res, strategy: str, entry_tap: int):
    """bar index -> dict of signal info"""
    sig: dict[int, dict] = {}
    for ev in res.events:
        if not ev.confirmed:
            continue
        if strategy == "essl_ob_tap" and ev.kind == K_ESSL_TAP:
            sig.setdefault(ev.bar, {})["essl"] = ev
        elif strategy in ("essl_ob_tap", "ob_tap") and ev.kind == K_TAP:
            # entry_tap filtering applies to the plain ob_tap strategy only;
            # the composite counts every qualifying bar (any tap number)
            if strategy == "ob_tap" and entry_tap and ev.extra.get("taps", 1) != entry_tap:
                continue
            sig.setdefault(ev.bar, {})["tap"] = ev
        elif strategy == "essl_sweep" and ev.kind == K_ESSL_SWEEP:
            sig.setdefault(ev.bar, {})["sweep"] = ev
    return sig


def run_backtest(
    symbols: list[str],
    data_cfg: DataConfig,
    engine_cfg: EngineConfig,
    bt: BacktestConfig,
    out_dir: str = "output",
) -> BacktestReport:
    import copy

    all_trades: list[Trade] = []
    event_rows: list[dict] = []
    n_ok = 0

    # the engine needs warmup history BEFORE the trading window; make sure the
    # data fetch starts early enough (ATR/MA seed, pivots, baselines)
    fetch_cfg = copy.deepcopy(data_cfg)
    if bt.start:
        need_start = pd.Timestamp(bt.start) - pd.Timedelta(days=200)
        cur = pd.Timestamp(fetch_cfg.start) if fetch_cfg.start else None
        if cur is None or cur > need_start:
            fetch_cfg.start = need_start.strftime("%Y-%m-%d")

    for sym in symbols:
        try:
            df = load_symbol(sym, fetch_cfg)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {sym}: data error: {e}")
            continue
        n_ok += 1
        # trading window: signals only from bt.start, but keep ~180 days of
        # warmup BEFORE start so ATR/MA/pivot state is fully warmed (event
        # bar indices stay aligned with this df — the engine runs on it)
        if bt.start:
            warm_start = pd.Timestamp(bt.start) - pd.Timedelta(days=180)
            df = df[df.index >= warm_start]
            trade_from = int(df.index.searchsorted(pd.Timestamp(bt.start)))
        else:
            trade_from = 0
        tick = data_cfg.tick_overrides.get(sym, detect_tick(df))
        eng = Engine(sym, engine_cfg, tick)
        res = eng.run(df, live_last_bar=False)
        for ev in res.events:
            event_rows.append({
                "symbol": sym, "date": ev.date, "kind": ev.kind, "confirmed": ev.confirmed,
                "zone_id": ev.zone_id, "pool_id": ev.pool_id, "price": ev.price,
                "price2": ev.price2, "taps": ev.extra.get("taps"),
                "reference": ev.extra.get("reference"), "stop": ev.extra.get("invalidation"),
                "reason": ev.extra.get("reason"),
            })
        print(f"  √ {sym}: {max(len(df) - trade_from, 0)} trade-window bars → footprints "
              f"{res.counters.get('footprints_observed', 0)}, OBs {res.counters.get('footprint_ob_created', 0)}, "
              f"taps {res.counters.get('taps', 0)}, eSSL taps {res.counters.get('essl_taps', 0)}, "
              f"sweeps {res.counters.get('essl_sweeps', 0)}")

        sigs = {t: v for t, v in _signals_by_bar(res, bt.strategy, bt.entry_tap).items() if t >= trade_from}
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        atr = res.atr
        dates = [d.strftime("%Y-%m-%d") for d in df.index]
        n = len(df)

        in_trade_until = -1
        for t in sorted(sigs):
            if t + 1 >= n:
                continue
            if bt.one_position_per_symbol and t <= in_trade_until:
                continue
            info = sigs[t]
            if bt.strategy == "essl_ob_tap":
                if "essl" not in info or "tap" not in info:
                    continue
                tap_ev, essl_ev = info["tap"], info["essl"]
                stop = tap_ev.extra.get("invalidation")
                sig_label = f"essl_tap+tap{tap_ev.extra.get('taps')}"
                zone_id, pool_id = tap_ev.zone_id, essl_ev.pool_id
                tap_no = tap_ev.extra.get("taps", 0)
            elif bt.strategy == "ob_tap":
                if "tap" not in info:
                    continue
                tap_ev = info["tap"]
                stop = tap_ev.extra.get("invalidation")
                sig_label = f"tap{tap_ev.extra.get('taps')}"
                zone_id, pool_id, tap_no = tap_ev.zone_id, None, tap_ev.extra.get("taps", 0)
            else:  # essl_sweep
                if "sweep" not in info:
                    continue
                sweep_ev = info["sweep"]
                if math.isnan(atr[t]) or atr[t] <= 0:
                    continue
                stop = sweep_ev.price - bt.sweep_stop_atr * atr[t]
                sig_label = "essl_sweep"
                zone_id, pool_id, tap_no = None, sweep_ev.pool_id, 0
            if stop is None or math.isnan(stop) or stop <= 0:
                continue
            e_price = o[t + 1] if bt.entry == "next_open" else c[t]
            if not (stop < e_price):
                continue
            target = e_price + bt.rr * (e_price - stop) if bt.rr and bt.rr > 0 else math.inf
            entry_i = t + 1 if bt.entry == "next_open" else t
            exit_i, exit_p, reason = None, None, ""
            last = min(entry_i + bt.max_bars, n - 1)
            for b in range(entry_i, last + 1):
                if l[b] <= stop:
                    exit_i, exit_p, reason = b, stop, "stop"
                    break
                if math.isfinite(target) and h[b] >= target:
                    exit_i, exit_p, reason = b, target, "target"
                    break
            if exit_i is None:
                exit_i, exit_p, reason = last, c[last], "time" if last < n - 1 else "end_of_data"
            risk = e_price - stop
            r_mult = (exit_p - e_price) / risk if risk > 0 else 0.0
            trade = Trade(
                symbol=sym, strategy=bt.strategy,
                entry_date=dates[entry_i], entry_price=round(e_price, 4),
                stop=round(stop, 4), target=round(target, 4) if math.isfinite(target) else None,
                exit_date=dates[exit_i], exit_price=round(exit_p, 4),
                exit_reason=reason, bars_held=exit_i - entry_i,
                r_multiple=round(r_mult, 3), pnl_pct=round((exit_p / e_price - 1) * 100, 3),
                zone_id=zone_id, pool_id=pool_id, tap_number=tap_no, signals=sig_label,
            )
            all_trades.append(trade)
            in_trade_until = exit_i
        del res

    trades_df = pd.DataFrame([t.__dict__ for t in all_trades])
    if len(trades_df):
        trades_df = trades_df.sort_values(["entry_date", "symbol"]).reset_index(drop=True)

    # equity curve (fixed fractional risk, per-trade, in completion order)
    equity = []
    if len(all_trades):
        cash = bt.initial_capital
        for t in sorted(all_trades, key=lambda t: (t.exit_date, t.entry_date, t.symbol)):
            r = t.r_multiple
            cash *= (1.0 + bt.risk_per_trade * r)
            equity.append({"date": t.exit_date, "symbol": t.symbol, "r": r, "equity": round(cash, 2)})
    equity_df = pd.DataFrame(equity)
    summary = _summarize(bt, all_trades, equity_df, n_ok)
    per_symbol = (
        trades_df.groupby("symbol").agg(
            trades=("symbol", "size"),
            win_rate_pct=("pnl_pct", lambda s: 100 * (s > 0).mean()),
            total_r=("r_multiple", "sum"),
            avg_bars=("bars_held", "mean"),
        ).round(2).reset_index()
        if len(trades_df) else pd.DataFrame()
    )

    os.makedirs(out_dir, exist_ok=True)
    if len(trades_df):
        trades_df.to_csv(os.path.join(out_dir, "trades.csv"), index=False)
    if len(equity_df):
        equity_df.to_csv(os.path.join(out_dir, "equity.csv"), index=False)
    with open(os.path.join(out_dir, "events.csv"), "w", encoding="utf-8") as fh:
        pd.DataFrame(event_rows).to_csv(fh, index=False)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write(format_summary(summary) + "\n\n" + per_symbol.to_string(index=False))
    return BacktestReport(strategy=bt.strategy, trades=all_trades, equity=equity_df,
                          summary=summary, per_symbol=per_symbol, out_dir=out_dir)


def _summarize(bt: BacktestConfig, trades: list[Trade], equity: pd.DataFrame, n_symbols: int) -> dict:
    s: dict = {
        "strategy": bt.strategy,
        "symbols": n_symbols,
        "trades": len(trades),
    }
    if not trades:
        s.update(win_rate=None, profit_factor=None, total_r=None, total_return_pct=None,
                 max_drawdown_pct=None, sharpe=None, avg_bars=None)
        return s
    r = np.array([t.r_multiple for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    pf = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
    eq = equity["equity"].to_numpy()
    peak = np.maximum.accumulate(eq)
    dd = (eq / peak - 1.0)
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([0.0])
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if np.std(rets) > 0 else 0.0
    s.update(
        win_rate=round(100 * len(wins) / len(r), 1),
        profit_factor=round(float(pf), 2) if math.isfinite(pf) else None,
        total_r=round(float(r.sum()), 2),
        avg_win_r=round(float(wins.mean()), 2) if len(wins) else None,
        avg_loss_r=round(float(losses.mean()), 2) if len(losses) else None,
        expectancy_r=round(float(r.mean()), 3),
        total_return_pct=round((eq[-1] / bt.initial_capital - 1) * 100, 2),
        max_drawdown_pct=round(float(dd.min()) * 100, 2),
        sharpe=round(sharpe, 2),
        avg_bars=round(float(np.mean([t.bars_held for t in trades])), 1),
        exits={reason: sum(1 for t in trades if t.exit_reason == reason) for reason in
               ("stop", "target", "time", "end_of_data")},
    )
    return s


def format_summary(s: dict) -> str:
    lines = [
        "=" * 62,
        "FPFSSL8.2 BACKTEST — " + s["strategy"].upper(),
        "=" * 62,
        f"symbols            : {s['symbols']}",
        f"trades             : {s['trades']}",
    ]
    if s["trades"]:
        lines += [
            f"win rate           : {s['win_rate']}%",
            f"profit factor      : {s['profit_factor']}",
            f"total R            : {s['total_r']} R",
            f"avg win / avg loss : {s.get('avg_win_r')} R / {s.get('avg_loss_r')} R",
            f"expectancy         : {s['expectancy_r']} R per trade",
            f"total return       : {s['total_return_pct']}% (compounded, per-trade risk model)",
            f"max drawdown       : {s['max_drawdown_pct']}%",
            f"sharpe (daily eq)  : {s['sharpe']}",
            f"avg bars held      : {s['avg_bars']}",
            f"exits              : {s['exits']}",
        ]
    else:
        lines.append("no trades for this strategy/universe in the tested window")
    lines.append("=" * 62)
    return "\n".join(lines)
