# FOOTPRINT ESSL — Python Scanner & Backtest Engine

A faithful Python port of the Pine v6 indicator
**"Footprint Source TAP + Fresh SSL + Developing Preview v8.2"**
(`FOOTPRINT ESSL.txt`), built for the **daily timeframe**, with:

* a **live market scanner** that runs continuously and sends **Telegram alerts**
  whenever, in *any* watched stock, price **taps an eSSL level with all the
  remaining rules matched** (a confirmed footprint OB's source-TAP condition fires on
  the same bar), plus defence / sweep / invalidation alerts;
* a **backtest engine** that trades exactly those signals on daily bars
  (next-open entry, OB stop, R-multiple target, time exit) with full statistics;
* a **report** command to inspect live zones, eSSL levels and FRESH lows at any time.

> Read **[ANALYSIS.md](ANALYSIS.md)** first — it contains the deep, section-by-section
> analysis of the indicator (state machines, exact arithmetic, ordering guarantees,
> and what the Python port adds).

---

## Quickstart

```bash
# 1. Python environment
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2. Telegram (see below for how to get the credentials)
export TELEGRAM_BOT_TOKEN="123456:ABC-..."      # or set in config.yaml
export TELEGRAM_CHAT_ID="123456789"
python -m fpfssl test-telegram                  # must print a message ✅

# 3. Live scanner (Yahoo Finance daily data, 15-min polling)
python -m fpfssl scan                           # runs forever (Ctrl-C to stop)
python -m fpfssl scan --once                    # single pass, then exit

# 4. Backtest the alert signals on daily history
python -m fpfssl backtest                       # default strategy: essl_ob_tap
python -m fpfssl backtest --strategy ob_tap     # TAP on any confirmed FP-OB
python -m fpfssl backtest --strategy essl_sweep # eSSL sweep + reclaim (liquidity grab)

# 5. Introspect current state (zones / eSSL levels / FRESH lows / recent events)
python -m fpfssl report --symbol RELIANCE.NS
```

### Getting Telegram credentials

1. Talk to **@BotFather** on Telegram → `/newbot` → copy the **bot token**.
2. Send your bot a message, then get your **chat id** from **@userinfobot**
   (group/subject id works too — negative numbers are fine).
3. Put them in `config.yaml` (`telegram.token` / `telegram.chat_id`) **or** export the
   environment variables (recommended — keeps secrets out of the repo).

Run `python -m fpfssl test-telegram` to verify. Until configured, `scan` prints
"Telegram not configured" and drops messages (or use `--dry-run` to print them).

---

## The primary alert (what you asked for)

`essl_ob_tap` — **price TAPS an eSSL level with ALL the remaining rules matched**:

On one daily bar, for one symbol, **both** must be true:

1. an **active eSSL level** (external sell-side liquidity = confirmed major 10-bar
   pivot low, still unbreached) is **tapped** — the bar's low reaches it
   (touch, partial, or full penetration with the script's own tick definitions); **and**
2. a **confirmed footprint OB** exists (evidence → departure → displacement → buffered
   break of the frozen structure, all verified) and its **source-TAP condition fires
   on that same bar** (`low ≤ TAP reference ∧ high ≥ zone bottom`, after departure,
   zone age ≥ 3, sweep rule if enabled).

```
🚨 eSSL TAP + Footprint TAP — ALL RULES MATCH
📈 RELIANCE.NS (Daily) 2026-09-10
💧 eSSL level 2,451.35 (EQL x2, age 55 bars, origin 2026-06-11)
   tap low 2,449.80 | penetrated 1.55 below level
   close 2,456.90 back above level → RECLAIMED ✅
🧱 FP-OB #7 2,448.00-2,462.50 (TAP 1/4, TAPPED / pending)
   ref 2,463.10 ← adj 2,460.25 | stop 2,445.90
   Repeated base-response proxy; pre-base volume reference | RVOL 1.42 | obs 3
   born 2026-08-20 | structure 2,488.10 | displaced 2026-08-21
Action: source-compatible long reference at TAP. Stop below OB invalidation.
```

The same bar may carry extra context (e.g. the tap is also a confirmed eSSL **sweep +
reclaim** — a liquidity grab — which the message marks).

### Other alerts (all in `scanner.alert_events`, toggle freely)

| event | meaning |
|---|---|
| `essl_ob_tap` | 🚨 the composite above (primary) |
| `essl_sweep` | eSSL penetration with close reclaim — liquidity grabbed at the external low |
| `footprint_tap` | source-compatible TAP on any confirmed FP-OB (no eSSL coincidence required) |
| `defence` | source defence confirmed after a TAP (bullish bar, CLV ≥ 0.65, RVOL ≥ 1.3, close > zone top, micro-BOS) |
| `zone_invalid` | OB invalidated (live close < fixed stop) or tap limit exceeded |
| `essl_created` | a new eSSL reference was published (fresh major low) — a future tap target |

---

## GitHub Actions: Scanner and Backtest

The **Actions** tab includes two workflows:

* **Scanner** — scheduled every 15 minutes on weekdays (UTC), running
  `scan --once` rather than an infinite loop. Add repository Actions secrets
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for live alerts. Manual runs default
  to **dry run**; uncheck it to send alerts. Optional `symbols` overrides the
  configured universe. Live runs restore/save deduplication state using Actions
  cache and do not overlap; dry runs do not change the live cache. Cache eviction
  can reset deduplication. Push runs only perform an offline synthetic smoke test.
* **Backtest** — choose **Run workflow** to select Yahoo or synthetic data,
  strategy, optional symbols, and history bars (200–20000). Results appear in the
  run summary and a downloadable artifact containing the CSV/text reports (30-day
  retention). Push/PR runs execute engine tests and a synthetic backtest without
  Telegram credentials or market-data access.

Merge the workflows into the repository's **default branch** to enable schedules
and the manual **Run workflow** buttons. GitHub schedules are best-effort and may
be delayed; use the supervised scanner below when precise continuous polling is
required. Synthetic results are demo data, not real market performance.

## Running it 24/7 on a live market

The scanner is a plain Python loop (`scan`), safe to run under any supervisor:

```bash
# terminal
nohup python -m fpfssl scan >> scanner.log 2>&1 &

# or systemd (recommended) — /etc/systemd/system/fpfssl.service
[Unit]
Description=FOOTPRINT ESSL daily scanner
After=network-online.target

[Service]
WorkingDirectory=/opt/FOOTPRINT
EnvironmentFile=/opt/FOOTPRINT/.env
ExecStart=/opt/FOOTPRINT/.venv/bin/python -m fpfssl scan
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Behaviour details:

* **Polling** — every `scanner.poll_minutes` (default 15), each symbol's daily history
  is re-run through the engine. Because the engine is *stateless per pass* (it rebuilds
  all state from history), intraday updates roll back naturally — exactly like Pine's
  live last bar.
* **Provisional vs confirmed** — events seen on the still-forming daily bar are sent
  with a `LIVE (intraday bar — provisional)` tag; after the daily close the scanner
  sends the confirmed version (separate dedupe key). Set `provisional_alerts: false`
  to only alert on closed bars.
* **Deduplication** — state is persisted in `state/scanner_state.json`; the same
  (symbol, event, bar-date, confirmed?, object) never alerts twice, and a per
  symbol+event cooldown (default 60 min) prevents spam.
* **Stale data guard** — Yahoo symbols whose last bar is older than
  `max_stale_days` are skipped (market closed, holiday, delisted).

---

## Data sources

| `data.source` | where it's used | notes |
|---|---|---|
| `yahoo` | **live use (default)** | daily bars via `yfinance`; works on any machine with internet. Tickers: US as-is, NSE `.NS`, BSE `.BO`, etc. |
| `csv` | offline / own data | `data/<SYMBOL>.csv` with header `date,open,high,low,close,volume` (TradingView exports work: rename columns). Great for NSE data you already have. |
| `synthetic` | demos/tests in sealed environments | deterministic regime-switching generator. **Not real data — never trade or draw conclusions from it.** |

Tick size is auto-detected per symbol from the prices; override per symbol in
`data.tick_overrides` if needed (e.g. NSE stocks trade in 0.05 ticks).

## Backtest engine

`python -m fpfssl backtest` runs the engine over each symbol's daily history and
trades the signals:

* **entry** — `next_open` (default; the signal is known at the bar close, so you enter
  the next day's open) or `bar_close`;
* **stop** — the OB's **fixed source stop** (`zone bottom − 0.15 × ATR`) for tap
  strategies; `eSSL level − 0.5 × ATR` for `essl_sweep`;
* **target** — `rr` × risk (default 2.0R; set `0` to disable);
* **time exit** — `max_bars` (default 30);
* **risk model** — compounded equity at `risk_per_trade` per trade (default 1%).

Outputs in `output/backtest_<strategy>_<timestamp>/`:

* `trades.csv` — every trade (entry/exit, R multiple, reason, zone/pool ids);
* `equity.csv` — equity curve;
* `events.csv` — the full event log the trades came from;
* `summary.txt` — win rate, profit factor, expectancy, max DD, Sharpe, exits, per-symbol table.

> ⚠️ Backtests measure *this* signal set, not a promise of future performance. The
> composite signal is deliberately rare by design; expect small sample sizes on
> any single stock over a few years of daily bars.

## Engine configuration

Every Pine input is exposed under `engine:` in `config.yaml` (same names as the Pine
input groups, snake_case). The defaults **are the Pine script's defaults** — change
them only after testing. Full list with descriptions: see the comment block in
`config.yaml` and §7 of ANALYSIS.md.

## Tests

```bash
.venv/bin/python tests/test_engine.py
```

Six hand-crafted scenario tests verify the port bar-for-bar: the full
evidence→OB→TAP→defence→invalidation chain, eSSL pool creation + touch + sweep,
eSSL break, EQL clustering of equal lows, event invariants on synthetic data, and the
composite ALL-RULES bar (eSSL tap + footprint TAP on the same bar).

## Project layout

```
FOOTPRINT ESSL.txt      the original Pine v6 script (source of truth)
ANALYSIS.md             deep analysis of the indicator
config.yaml             universe + scanner/backtest/telegram/engine config
fpfssl/
  engine.py             1:1 port of the Pine state machines (+ eSSL event layer)
  events.py             event model + Telegram message formatting
  scanner.py            live polling scanner, dedupe, alert dispatch
  backtest.py           signal trading + statistics + CSV reports
  data.py               yahoo / csv / synthetic loaders
  synthetic.py          deterministic offline data generator
  telegram.py           minimal Telegram HTTP notifier (dry-run capable)
  cli.py                `python -m fpfssl …` commands
tests/test_engine.py    scenario tests for the port
data/                   CSV data (git-ignored); `sample-data` fills it synthetically
state/                  scanner dedupe state (git-ignored)
output/                 backtest reports (git-ignored)
```

## Disclaimer

This is technical software for research. The indicator (and therefore this tool)
works on **proxies of institutional behaviour** — the source script itself says
institutional identity, order quantity and fills are *not observed*. Nothing here is
financial advice; past backtests do not guarantee future results.
