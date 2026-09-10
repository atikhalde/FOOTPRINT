# FOOTPRINT ESSL — Live NSE Scanner & Backtest Engine

A bar-for-bar Python port of the Pine v6 indicator
**"Footprint Source TAP + Fresh SSL + Developing Preview v8.2"**
(`FOOTPRINT ESSL.txt`), running on the **live Indian market (NSE/BSE) via yfinance**:

* a **live market scanner** on any timeframe (`15m` default, `1d` supported) that sends
  **Telegram alerts** whenever, in *any* watched stock, price **taps an eSSL level with
  all the remaining rules matched** (a confirmed footprint OB's source-TAP condition fires
  on the same bar), plus defence / sweep / invalidation alerts;
* a **backtest engine** that trades exactly those signals (next-open entry, OB stop,
  R-multiple target, time exit) with full statistics;
* a **report** command to inspect live zones, eSSL levels and FRESH lows at any time.

> Read **[ANALYSIS.md](ANALYSIS.md)** first — it contains the deep, section-by-section
> analysis of the indicator (state machines, exact arithmetic, ordering guarantees,
> and what the Python port adds).

The scanner **exactly matches the indicator script**: same input defaults, same
(A)→(G) bar ordering (setup lifecycle → SSL → FRESH → confirmation → structure →
evidence → *live* TAP pass), same ATR/RMA/pivot math, same frozen-origin and
first-penetration-terminal rules. The only additions are the group-8 **event
emitters** (eSSL tap/sweep/break) that reuse the script's own penetration/reclaim
definitions — the Pine script itself has no alerts.

---

## Quickstart — live NSE on 15m

```bash
# 1. Python environment
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2. Telegram (see below for how to get the credentials)
export TELEGRAM_BOT_TOKEN="123456:ABC-..."      # or set in config.yaml
export TELEGRAM_CHAT_ID="123456789"
python -m fpfssl test-telegram                  # must print a message ✅

# 3. Live NSE scanner (default: 15m bars, 5-min polling, IST market clock)
python -m fpfssl scan                           # runs forever (Ctrl-C to stop)
python -m fpfssl scan --once                    # single pass, then exit
python -m fpfssl scan --once --dry-run          # print what would be sent

# 4. Backtest the alert signals (default strategy: essl_ob_tap)
python -m fpfssl backtest                       # 15m NSE universe
python -m fpfssl backtest --strategy ob_tap     # TAP on any confirmed FP-OB
python -m fpfssl backtest --interval 1d         # daily timeframe instead

# 5. Introspect current state (zones / eSSL levels / FRESH lows / recent events)
python -m fpfssl report --symbol RELIANCE.NS
python -m fpfssl report --symbol RELIANCE.NS --interval 1d

# 6. "Why is it not alerting?" — the live state behind every rule, per symbol
python -m fpfssl diagnose                      # armed FP-OBs, eSSL levels, distance to price
python -m fpfssl diagnose --at "2026-09-10 12:00"   # replay an earlier clock
python -m fpfssl diagnose --json > diag.json   # machine-readable
```

### Getting Telegram credentials

1. Talk to **@BotFather** on Telegram → `/newbot` → copy the **bot token**.
2. Send your bot a message, then get your **chat id** from **@userinfobot**
   (group/subject id works too — negative numbers are fine).
3. Put them in `config.yaml` (`telegram.token` / `telegram.chat_id`) **or** export the
   environment variables (recommended — keeps secrets out of the repo).

Run `python -m fpfssl test-telegram` to verify (add `--no-dry-run` to force a
real send if `telegram.dry_run` is still `true`). Until credentials are
configured, `scan` prints a loud warning and drops messages — use `--dry-run` to
preview them instead. Keep `telegram.dry_run: false` for live alerts: the CI
workflow passes `--no-dry-run` on real runs so a leftover `true` cannot silence
the scanner silently.

---

## The primary alert (what you asked for)

`essl_ob_tap` — **price TAPS an eSSL level with ALL the remaining rules matched**:

On one bar (15m by default), for one symbol, **both** must be true:

1. an **active eSSL level** (external sell-side liquidity = confirmed major 10-bar
   pivot low, still unbreached) is **tapped** — the bar's low reaches it
   (touch, partial, or full penetration with the script's own tick definitions); **and**
2. a **confirmed footprint OB** exists (evidence → departure → displacement → buffered
   break of the frozen structure, all verified) and its **source-TAP condition fires
   on that same bar** (`low ≤ TAP reference ∧ high ≥ zone bottom`, after departure,
   zone age ≥ 3, sweep rule if enabled).

```
🚨 eSSL TAP + Footprint TAP — ALL RULES MATCH
📈 RELIANCE.NS (15m) 2026-09-10 10:00 • LIVE (intraday bar — provisional)
💧 eSSL level 2,451.35 (EQL x2, age 55 bars, origin 2026-09-08 11:15)
   tap low 2,449.80 | penetrated 1.55 below level
   close 2,456.90 back above level → RECLAIMED ✅
🧱 FP-OB #7 2,448.00-2,462.50 (TAP 1/4, TAPPED / pending)
   ref 2,463.10 ← adj 2,460.25 | stop 2,445.90
   Repeated base-response proxy; pre-base volume reference | RVOL 1.42 | obs 3
   born 2026-09-09 14:15 | structure 2,488.10 | displaced 2026-09-09 14:15
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

* **Scanner** — scheduled every 10 minutes across the NSE session
  (09:15–15:30 IST = 03:45–10:00 UTC, Mon–Fri). **Each trigger starts one job
  that polls internally until the close** (`python -m fpfssl scan`), and the
  `concurrency` group keeps a single scanner alive: a tick that fires while a
  session job is running becomes the next queued run and takes over when the
  job ends. That matters because GitHub's `schedule` is best-effort — ticks are
  delayed 5–30 min under load and high-frequency ticks get dropped (a 5-minute
  cron in this repo's history fired **once** where sixteen ticks were
  expected), so a design that needs 96 ticks a day cannot work; a design where
  **any single tick covers the whole session** does. Add repository Actions
  secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for live alerts; scheduled
  runs pass `--no-dry-run`, so a `telegram.dry_run: true` left in `config.yaml`
  can never silently swallow alerts again. Manual runs send for real by default
  (check *dry_run* to preview), and `symbols` / `interval` / `once` override the
  config. Live runs restore/save deduplication state using Actions cache; dry
  runs never touch the live cache. Push runs only do an offline synthetic smoke
  test. Every run appends a **diagnostics block** (per-symbol armed references,
  feed lag, skip reasons) to its run summary.
* **Backtest** — choose **Run workflow** to select Yahoo or synthetic data,
  strategy, timeframe, optional symbols, and history bars (200–20000). Results
  appear in the run summary and a downloadable artifact containing the CSV/text
  reports (30-day retention). Push/PR runs execute engine tests and a synthetic
  backtest without Telegram credentials or market-data access.

Merge the workflows into the repository's **default branch** to enable schedules
and the manual **Run workflow** buttons. GitHub only evaluates `schedule` on the
default branch, and it can still delay (5–30 min) or drop a tick — that is why
the scanner job is long-lived instead of relying on a tick every 5 minutes. For
guaranteed timing, run it under systemd below, or trigger the workflow from an
external scheduler via `repository_dispatch`/`workflow_dispatch`. Synthetic
results are demo data, not real market performance.

## Running it live on NSE (09:15–15:30 IST)

The scanner is a plain Python loop (`scan`), safe to run under any supervisor.
It uses the **IST market clock**: the last bar is `LIVE` (provisional) only while
it is actually forming; after the bar closes the same event is sent once more as
confirmed. Weekends, holidays and feed lag are handled (stale symbols are skipped,
not alerted).

```bash
# terminal (15m live)
nohup python -m fpfssl scan >> scanner.log 2>&1 &

# or systemd (recommended) — /etc/systemd/system/fpfssl.service
[Unit]
Description=FOOTPRINT ESSL live NSE scanner
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

* **Timeframes** — `data.interval` selects the TradingView-equivalent TF
  (`15m` default; `5m`/`30m`/`1h`/`1d` all run the identical state machines).
  Yahoo intraday windows apply (15m → last 60 days, 1m → last 7 days, 1h → last
  730 days); the scanner requests the maximum window automatically.
* **History is never trimmed** — the engine runs over *every* bar the feed
  serves (`data.max_bars` is the only cap, `0` = keep everything). The
  footprint/TAP state machine is path-dependent: a zone born several hundred
  bars ago is still the live TAP reference today, and cutting the frame to
  `history_bars` deletes that state — measurably ~1 in 10 composites
  disappears, and long-lived zones vanish entirely (see
  `tests/test_live_scanner.py`, whose regression case is a composite whose zone
  is 685 bars old). `history_bars` is a *minimum window* hint, `min_bars` is
  the skip threshold for feeds that are too short to warm the engine up.
* **Polling** — every `scanner.poll_minutes` (default 5 for 15m), each symbol's
  history is re-run through the engine. Because the engine is *stateless per pass*
  (it rebuilds all state from history), forming-bar updates roll back naturally —
  exactly like Pine's live last bar.
* **Provisional vs confirmed** — events on the still-forming bar are sent with a
  `LIVE (intraday bar — provisional)` tag, immediately when the bar is still
  forming. The closed bar has a separate dedupe key, so it *may* alert again —
  in practice the `alert_cooldown_minutes` spam guard (60 min) usually suppresses
  that follow-up, which is why the LIVE message is the one to act on. Set
  `provisional_alerts: false` to only alert on closed bars.
* **Deduplication** — state is persisted in `state/scanner_state.json`; the same
  (symbol, timeframe, event, bar-time, confirmed?, object) never alerts twice, and
  a per symbol+event cooldown (default 60 min) prevents spam.
* **Stale/lag guards** — symbols whose last bar is older than `max_stale_days`
  *trading* days are skipped (weekends don't count), as are symbols lagging more
  than `max_lag_minutes` behind the live NSE clock.
* **Session loop** — `scan` (without `--once`) waits for the 09:15 bell if it is
  started within `preopen_wait_minutes`, polls every `poll_minutes`, and keeps
  going until `stop_after_close_minutes` after 15:30 so the closing bar is
  alerted once the feed settles. Started far outside the session it does one
  pass and exits.

* **NSE ticks** — `.NS`/`.BO` symbols resolve to the 0.05 equity tick
  automatically (adjusted-price dust never corrupts the tick math); override per
  symbol in `data.tick_overrides` if needed.

### Why did I get no alert? (troubleshooting)

Run `python -m fpfssl diagnose` (or read the *Diagnostics* block in the Scanner
workflow's run summary). It prints exactly which rule is not satisfied:

| what you see | meaning |
|---|---|
| `⛔ SKIPPED — stale feed …` | the last bar is `max_stale_days` trading days old (wrong symbol/timezone, feed stuck) |
| `⛔ SKIPPED — feed lag …` | the market is open but the feed is `max_lag_minutes` behind |
| `⛔ SKIPPED — only N bars < min_bars` | not enough history to warm the engine up |
| `armed FP-OBs: none` | no confirmed footprint OB — the composite needs a TAP, so nothing can fire |
| `armed eSSL: none` | no active eSSL level to tap (all breached/expired) |
| both armed, far away | the setup is live but price has not reached the references yet |
| `→ essl_ob_tap @ …` | the composite **is** firing on a recent bar — check the Telegram credentials |

The composite alert is deliberately strict (footprint TAP **and** eSSL tap on the
*same* bar — the "ALL RULES" condition), so expect a *low* rate rather than a
daily stream. Measured on the configured NSE universe (15m, 60 days of Yahoo
bars, ~1470 bars per symbol): 0–5 composite bars per symbol, i.e. roughly **one
alert per symbol per month**, clustered when price sweeps an external low into
an armed OB — plus `footprint_tap`/`essl_sweep`/`defence` events if you enable
them (5–22 TAPs and 10–15 sweeps per symbol per 60 days). `diagnose` prints the
exact rate for your universe:
`alert rate: 2 composite bar(s) in 1471 bars (0.14%); last 2026-08-26 15:00`.

---

## Data sources

| `data.source` | where it's used | notes |
|---|---|---|
| `yahoo` | **live use (default)** | 15m/daily bars via `yfinance`; works on any machine with internet. NSE `.NS`, BSE `.BO`, US as-is. |
| `csv` | offline / own data | `data/<SYMBOL>.csv` with header `date,open,high,low,close,volume` (intraday: `date` may include `HH:MM`). Great for data you already have. |
| `synthetic` | demos/tests in sealed environments | deterministic regime-switching generator. **Not real data — never trade or draw conclusions from it.** |

## Backtest engine

`python -m fpfssl backtest` runs the engine over each symbol's history and
trades the signals:

* **entry** — `next_open` (default; the signal is known at the bar close, so you enter
  the next bar's open) or `bar_close`;
* **stop** — the OB's **fixed source stop** (`zone bottom − 0.15 × ATR`) for tap
  strategies; `eSSL level − 0.5 × ATR` for `essl_sweep`;
* **target** — `rr` × risk (default 2.0R; set `0` to disable);
* **time exit** — `max_bars` (default 30);
* **risk model** — compounded equity at `risk_per_trade` per trade (default 1%).

Outputs in `output/backtest_<strategy>_<interval>_<timestamp>/`:

* `trades.csv` — every trade (entry/exit, R multiple, reason, zone/pool ids);
* `equity.csv` — equity curve;
* `events.csv` — the full event log the trades came from;
* `summary.txt` — win rate, profit factor, expectancy, max DD, Sharpe, exits, per-symbol table.

> ⚠️ Backtests measure *this* signal set, not a promise of future performance. The
> composite signal is deliberately rare by design; expect small sample sizes on
> any single stock. Intraday backtests are bounded by Yahoo's lookback window.

## Engine configuration

Every Pine input is exposed under `engine:` in `config.yaml` (same names as the Pine
input groups, snake_case). The defaults **are the Pine script's defaults** — change
them only after testing. Full list with descriptions: see the comment block in
`config.yaml` and §7 of ANALYSIS.md.

## Tests

```bash
python tests/test_engine.py          # 9 indicator-parity scenarios (daily bars)
python tests/test_fidelity_live.py   # 10 exact-match + live-NSE/intraday tests
python tests/test_live_scanner.py    # 9 end-to-end live-scanner tests (offline feed)
```

The first suite verifies the port bar-for-bar: the full
evidence→OB→TAP→defence→invalidation chain, eSSL pool creation + touch + sweep,
eSSL break, EQL clustering of equal lows, event invariants on synthetic data, and the
composite ALL-RULES bar (eSSL tap + footprint TAP on the same bar).
The second suite locks the exact-match fixes (volume-baseline counts, NaN-safe
averages, repeat-tap emission, silent cap retirement, NSE 0.05 ticks) and the live
path (15m `HH:MM` bars, IST session clock, live-vs-closed last bar, Yahoo
intraday windows, MultiIndex intraday frames).
The third drives the **whole live pipeline offline** (fake feed + fake IST clock →
`LiveScanner.scan_symbol` → alert text): the long-lived-zone regression above,
LIVE vs confirmed tagging, dedup across passes, skip filters, session-clock
helpers, the untrimmed history guarantee and the day-long `scan` loop.

## Project layout

```
FOOTPRINT ESSL.txt      the original Pine v6 script (source of truth)
ANALYSIS.md             deep analysis of the indicator
config.yaml             universe + scanner/backtest/telegram/engine config (NSE 15m live)
fpfssl/
  engine.py             1:1 port of the Pine state machines (+ eSSL event layer)
  events.py             event model + Telegram message formatting
  scanner.py            live polling scanner (IST clock, any TF), dedupe, alert dispatch
  diag.py               `diagnose`: armed references / why-no-alert, per symbol
  backtest.py           signal trading + statistics + CSV reports
  data.py               yahoo (daily/intraday) / csv / synthetic loaders
  synthetic.py          deterministic offline data generator
  telegram.py           minimal Telegram HTTP notifier (dry-run capable)
  cli.py                `python -m fpfssl …` commands
tests/test_engine.py        indicator-parity scenario tests
tests/test_fidelity_live.py exact-match + live-NSE/intraday tests
tests/test_live_scanner.py  offline end-to-end live-scanner (fake feed/clock) tests
data/                   CSV data (git-ignored); `sample-data` fills it synthetically
state/                  scanner dedupe state (git-ignored)
output/                 backtest reports (git-ignored)
```

## Disclaimer

This is technical software for research. The indicator (and therefore this tool)
works on **proxies of institutional behaviour** — the source script itself says
institutional identity, order quantity and fills are *not observed*. Nothing here is
financial advice; past backtests do not guarantee future results.
