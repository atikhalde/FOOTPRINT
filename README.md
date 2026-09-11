# FOOTPRINT ESSL — Live NSE Scanner & Backtest Engine

A bar-for-bar Python port of the Pine v6 indicator
**"Footprint Source TAP + Fresh SSL + Developing Preview v8.2"**
(`FOOTPRINT ESSL.txt`), running on the **live Indian market (NSE/BSE) via yfinance**:

* a **live market scanner** over the **FULL NSE equity universe** (default: **daily TF**,
  narrowed by the **size filters** — market cap > ₹1,000 Cr and price > ₹100)
  that sends **Telegram alerts** whenever, in *any* watched stock, price **taps an eSSL
  level with all the remaining rules matched** (a confirmed footprint OB's source-TAP
  condition fires on the same bar), **and** — enabled by default — whenever price simply
  **touches an eSSL level** (💧 `essl_tap`: any active level, fresh or old, no footprint
  TAP required), plus defence / sweep / invalidation alerts;
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

## Quickstart — live NSE, daily TF, full universe

```bash
# 1. Python environment
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2. Telegram (see below for how to get the credentials)
export TELEGRAM_BOT_TOKEN="123456:ABC-..."      # or set in config.yaml
export TELEGRAM_CHAT_ID="123456789"
python -m fpfssl test-telegram                  # must print a message ✅

# 3. Live NSE scanner (default: DAILY bars, FULL NSE universe, size filters)
python -m fpfssl scan                           # one pass over the universe, then exits
python -m fpfssl scan --keep-polling            # poll every 15 min until the close instead
python -m fpfssl scan --once                    # single pass, then exit
python -m fpfssl scan --once --dry-run          # print what would be sent
python -m fpfssl scan --symbols RELIANCE.NS,TCS.NS   # subset instead of full_nse
python -m fpfssl scan --once --dry-run --min-mcap-cr 20000 --min-price 500
python -m fpfssl scan --once --dry-run --no-size-filters   # scan the whole NSE

# 4. Backtest the alert signals (default strategy: essl_ob_tap)
python -m fpfssl backtest                       # daily, full NSE universe
python -m fpfssl backtest --strategy ob_tap     # TAP on any confirmed FP-OB
python -m fpfssl backtest --interval 15m        # intraday timeframe instead

# 5. Introspect current state (zones / eSSL levels / FRESH lows / recent events)
python -m fpfssl report --symbol RELIANCE.NS
python -m fpfssl report --symbol RELIANCE.NS --interval 15m

# 6. "Why is it not alerting?" — the live state behind every rule, per symbol
python -m fpfssl diagnose                      # armed FP-OBs, eSSL levels, distance to price
python -m fpfssl diagnose --at "2026-09-10 12:00"   # replay an earlier clock
python -m fpfssl diagnose --json > diag.json   # machine-readable
```

### Full NSE universe (`full_nse`)

`config.yaml` ships with `symbols: [full_nse]`. The marker expands to the
**complete NSE equity list** (every `EQ`-series stock, ~2,000+ `.NS` tickers):

1. cached list from `data/nse_universe.csv` (reused for
   `data.universe_max_age_days`, default 7);
2. otherwise NSE's official equity list (`EQUITY_L.csv`), falling back to the
   Yahoo Finance screener (exchange `NSI`), then cached for next time.

Add `--refresh-universe` to force a refetch. Explicit tickers can be mixed
with the marker. For `--source csv` the marker means "every CSV in `data/`",
and for `synthetic` it means the built-in demo list — both offline. If the
universe cannot be fetched and no cache exists the scanner **fails loudly**
instead of silently scanning a smaller list.

### 100% match with the TradingView indicator

The scanner is designed so a signal here is a signal on the chart:

* **same timeframe** — default `interval: 1d` (daily), exactly the bars the
  indicator sees on the chart;
* **same prices** — `auto_adjust: false` fetches **raw exchange OHLC**
  (TradingView NSE data is unadjusted; adjusted series shift every OB/eSSL
  level after corporate actions);
* **same tick** — NSE/BSE tick = 0.05 (`syminfo.mintick`), resolved
  automatically per symbol;
* **same history** — daily data is fetched from listing (`period: max`), so
  the path-dependent state machines (footprint OBs, SSL pools) build the same
  state as the indicator's full chart history;
* **same engine** — the (A)→(G) per-bar ordering, arithmetic and state
  transitions of `FOOTPRINT ESSL.txt` are ported 1:1 (see `ANALYSIS.md`).

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

### The 💧 eSSL level-touch alert (`essl_tap`) — enabled by default

**Price touched an eSSL level → you get an alert**, on its own terms:

* **any** active eSSL level, **fresh or old** — the only age limit is
  `engine.essl_tap_max_age` (default 250 bars = the pool's own expiry, so in
  practice every live level counts);
* **with or without** a footprint TAP on that bar, and **first touch or repeat**;
* **never** gated by `tap_first_only` / `fresh_ob_only` — those filters narrow the
  footprint side only. A composite that they reject (TAP 2, or an OB older than
  the fresh window) still produces its 💧 touch alert, and the message says why
  the footprint side was filtered;
* one alert **per level** when a bar touches several eSSL levels (the 60-minute
  spam guard is per level, so they cannot mask each other);
* the level a 🚨 composite already reported is **not** repeated as a bare touch.

"Active" is exactly the indicator's own state. A bar may poke ≥ 1 tick below the
level and close back above it — that is a **sweep-and-reclaim**, the level held,
and it alerts as a touch (`RECLAIMED ✅`). A bar that **closes below the level**
is the indicator's *first full penetration is terminal* rule: the level is
retired at that very close, so it is no longer a touchable level when the
alert's bar ends. That outcome is a ⚠️ `essl_break` (enable it in
`alert_events`), never a 💧 touch — a bar still forming below the level waits
for the close instead of alerting mid-break. The verdict of that wait is the
**close-confirmed** alert, which is sent even while the 60-minute per-level
guard is still open for that level (see *Provisional vs confirmed* below) —
that follow-up is what delivers `RECLAIMED ✅` on a daily bar like the FMGOETZE
432.65 sweep.

```
💧 eSSL TAP — price touched the eSSL level
📈 RELIANCE.NS (15m) 2026-09-10 10:00 • LIVE (intraday bar — provisional)

💧 eSSL level 2,451.35 (EQL x2, age 191 bars, origin 2026-09-02 11:15)
   tap low 2,449.80 | penetrated 1.55 below level
   close 2,456.90 back above level → RECLAIMED ✅
   ℹ️ eSSL level touch — fires on every touch of an active eSSL level (fresh or old, no footprint TAP required)
```

### Other alerts (all in `scanner.alert_events`, toggle freely)

| event | meaning |
|---|---|
| `essl_ob_tap` | 🚨 the composite above (primary) |
| `essl_tap` | 💧 price touched an active eSSL level (fresh or old; footprint TAP not required) |
| `essl_sweep` | eSSL penetration with close reclaim — liquidity grabbed at the external low |
| `essl_break` | ⚠️ eSSL closed below (no reclaim) — the indicator retires the level at that close |
| `footprint_tap` | source-compatible TAP on any confirmed FP-OB (no eSSL coincidence required) |
| `defence` | source defence confirmed after a TAP (bullish bar, CLV ≥ 0.65, RVOL ≥ 1.3, close > zone top, micro-BOS) |
| `zone_invalid` | OB invalidated (live close < fixed stop) or tap limit exceeded |
| `essl_created` | a new eSSL reference was published (fresh major low) — a future tap target |

### TAP signal filters (TAP #1 + fresh OB only)

`config.yaml` ships with the TAP stream narrowed to first touches of young zones —
both the 🚨 composite and the standalone `footprint_tap` must pass these gates
(the 💧 `essl_tap` level-touch alert is **exempt**):

```yaml
scanner:
  tap_first_only: true       # only TAP 1/N alerts; TAP 2/3/4 stay silent
  fresh_ob_only: true        # only alert when the tapped OB is young
  fresh_ob_max_age_bars: 50  # OB age = tap_bar − ob_born_bar, in data.interval bars
```

Skipped taps are logged (`composite skipped — OB #7 age 132 bars > fresh window
50`) so a quiet pass still explains itself. Set either toggle to `false` to
restore the unfiltered stream. The default `alert_events` list is
`essl_ob_tap` + `essl_tap` + `footprint_tap` — add the muted events back to
re-enable them.

### Size filters — market cap > ₹1,000 Cr and price > ₹100

The universe is the whole NSE, but most of it is not worth an alert: ₹40
penny stocks on a few-crore float produce exactly the same signals and mostly
produce untradeable ones. `config.yaml` therefore ships with two **size
filters**, applied per symbol *after* its bars are loaded and *before* the
engine runs (so a filtered stock costs no engine time at all):

```yaml
scanner:
  min_market_cap_cr: 1000   # ₹ crore — keep only stocks worth MORE than 1,000 Cr
  min_price: 100            # ₹     — keep only stocks trading ABOVE ₹100
  keep_unknown_market_cap: true   # no share count available -> scan anyway
  market_cap_cr_overrides: {}     # pin a symbol's ₹ crore, e.g. {TATASTEEL.NS: 95000}
```

Both thresholds are **strictly greater than** (₹1,000.00 exactly is filtered
out), and `0` switches that filter off. The 💧 `essl_tap` alert is *not*
exempt here — a filtered symbol is not scanned at all, so nothing alerts on it.

* **Price** is the last close of the frame the pass already fetched — raw
  exchange ₹, free, no extra request.
* **Market cap** = `shares outstanding × last close`, recomputed **every pass**,
  so a stock that sinks below the floor drops out of the scan and comes back the
  moment it recovers. Share counts move ~quarterly, so they are fetched once and
  cached in `data/nse_fundamentals.csv` (like the symbol list;
  `data.fundamentals_max_age_days: 30`). The first full-NSE pass pays the
  metadata fetch, every later pass is free.
* Symbols that clear the filters carry a size footer in their alerts, so a
  message states what it is worth:
  `📏 size price ₹1,432.00 · mcap ₹945,000 Cr · filter: mcap > ₹1,000 Cr + price > ₹100.00`
* **Fail-open:** if no share count can be resolved (metadata endpoint down), the
  symbol is *kept* and the pass logs one warning. A metadata outage must never
  silently swallow alerts; set `keep_unknown_market_cap: false` to make the
  filter strict instead.
* Try it without editing config: `scan --once --dry-run --min-mcap-cr 20000
  --min-price 500`, or scan everything again with `--no-size-filters`.
  `diagnose` reports the same verdict per symbol (`⛔ FILTERED OUT (size
  filters) — market cap ₹54 Cr below the ₹1,000 Cr minimum`).

---

## GitHub Actions: Scanner and Backtest

The **Actions** tab includes two workflows:

* **Scanner** — scheduled every 10 minutes across the NSE session
  (09:15–15:30 IST = 03:45–10:00 UTC, Mon–Fri). **Each trigger starts one job
  that scans the universe once and exits** (`python -m fpfssl scan` with the
  shipped `scanner.exit_after_pass: true`; dispatch with `poll_session: true`
  to get the previous session-long poller back), and the
  `concurrency` group keeps a single scanner alive: a tick that fires while a
  session job is running becomes the next queued run and takes over when the
  job ends. That matters because GitHub's `schedule` is best-effort — ticks are
  delayed 5–30 min under load and high-frequency ticks get dropped (a 5-minute
  cron in this repo's history fired **once** where sixteen ticks were
  expected), so a design that needs 96 ticks a day cannot work; a design where
  **any single tick covers the whole session** does. Every such job always ends
  on its own: at the close, at its `--max-runtime-minutes 325` budget (under the
  350-minute job timeout), or a second after you press **Cancel workflow** — see
  [Stopping the scanner](#stopping-the-scanner-ctrl-c-cancel-runtime-budget).
  Add repository Actions
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

## Stopping the scanner (one pass, Ctrl-C, cancel, runtime budget)

`scan` (without `--once`) can be a long-lived poller, so it has to be
**stoppable** — and by default it no longer needs to be: `scanner.exit_after_pass:
true` makes a run do ONE complete pass over the universe, save its dedup state and
exit (`scanner stopped: scan complete (scanner.exit_after_pass)`). Set it to
`false` (or pass `--keep-polling`) for the session-long poller described below:

| How | What happens |
| --- | --- |
| **Ctrl-C** / **SIGINT** | The poller stops within a second — at the next symbol, or immediately out of its poll nap — saves the dedup state and exits (`scanner stopped: received SIGINT`). |
| **`kill <pid>`** / **SIGTERM** (systemd stop, `timeout`, CI cancel) | Same graceful stop, so a cancelled run never re-announces its alerts later. |
| **one pass then exit** (`scanner.exit_after_pass: true`, the shipped default) | The pass completes, dedup state is saved, the process exits. A scheduled CI run therefore ends with its scan instead of sitting in the runner until the close (with every later cron tick queued behind it in the `concurrency` group). |
| **`--max-pass-minutes N`** (or `scanner.max_pass_minutes`) | Ceiling for a *single* pass (default `90`): a yahoo batch fetch that stalls past the ceiling is abandoned in a daemon thread — logged, state saved, run ends. This is the fix for "the scanner is stuck": a hung `yfinance` request used to be able to hold the pass (and the CI job) open indefinitely, because the polling loop only checked the clock *between* passes. |
| **`--max-runtime-minutes N`** (or `scanner.max_runtime_minutes`) | Hard wall-clock budget: after N minutes it saves state and exits cleanly, even mid-session. `0` (default) = the market clock alone decides. The Actions workflow passes `325`, below its own job timeout. |
| **Actions → Cancel workflow** | Cancels within a second. It used to be swallowed: a backgrounded process inherits SIGINT as `SIG_IGN` from the runner's non-interactive shell, so Python never installed its `KeyboardInterrupt` handler and the job ran for hours. The scanner now **installs its own SIGINT/SIGTERM handlers**, which overrides the inherited disposition. |
| **Pause the schedule** | Set the repository variable `SCANNER_ENABLED=false` (Settings → Secrets and variables → Actions → Variables). Scheduled ticks then exit immediately; manual *Run workflow* still works. |
| **session end** | Unchanged: the poller exits `stop_after_close_minutes` (15) past the 15:30 IST close, after the closing bar settles. |

A stopped run always leaves `state/scanner_state.json` written, so the pass that
takes over (a queued cron tick, or your next `scan`) does not repeat alerts.

## Running it live on NSE (09:15–15:30 IST)

The scanner is a plain Python loop (`scan`), safe to run under any supervisor.
It uses the **IST market clock**: the last bar is `LIVE` (provisional) only while
it is actually forming; after the bar closes the same event is sent once more as
confirmed. Weekends, holidays and feed lag are handled (stale symbols are skipped,
not alerted).

```bash
# terminal (daily, full NSE)
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
  (`1d` default; `5m`/`15m`/`30m`/`1h` all run the identical state machines).
  Yahoo intraday windows apply (15m → last 60 days, 1m → last 7 days, 1h → last
  730 days); the scanner requests the maximum window automatically. Daily data
  is fetched from listing (`period: max`).
* **Full universe fetching** — a full-NSE pass is thousands of yfinance
  requests (one per ticker), so each scan pass downloads the whole universe in
  one threaded, paced batch (`data.batch*` settings) and then runs the engine
  per symbol over the same frames — identical signals, one transport.
* **History is never trimmed** — the engine runs over *every* bar the feed
  serves (`data.max_bars` is the only cap, `0` = keep everything). The
  footprint/TAP state machine is path-dependent: a zone born several hundred
  bars ago is still the live TAP reference today, and cutting the frame to
  `history_bars` deletes that state — measurably ~1 in 10 composites
  disappears, and long-lived zones vanish entirely (see
  `tests/test_live_scanner.py`, whose regression case is a composite whose zone
  is 685 bars old). `history_bars` is a *minimum window* hint, `min_bars` is
  the skip threshold for feeds that are too short to warm the engine up.
* **Raw prices** — `data.auto_adjust: false` (default) feeds the engine the
  same unadjusted OHLC the TradingView indicator sees, so OB/eSSL levels match
  the chart exactly (including after dividends/splits/bonuses).
* **Polling** — every `scanner.poll_minutes` (default 15 for daily), each
  symbol's history is re-run through the engine. Because the engine is
  *stateless per pass* (it rebuilds all state from history), forming-bar
  updates roll back naturally — exactly like Pine's live last bar.
* **Provisional vs confirmed** — events on the still-forming bar are sent with a
  `LIVE (intraday bar — provisional)` tag, immediately when the bar is still
  forming. The closed bar has a separate dedupe key, so it alerts **again**: that
  second message is the state the indicator itself shows at the close (e.g. the
  `RECLAIMED ✅` of a swept eSSL level), and it is deliberately *not* suppressed
  by `alert_cooldown_minutes` — on the daily timeframe the confirming pass always
  runs within the cooldown window of the last intraday poll, so letting the guard
  eat it would mean the close-confirmed alert never arrives at all. Dedup still
  applies per bar+state+object, so a touch is at most two messages (the guess and
  the verdict), never a stream. Set `provisional_alerts: false` to only alert on
  closed bars.
* **Deduplication** — state is persisted in `state/scanner_state.json`; the same
  (symbol, timeframe, event, bar-time, confirmed?, object) never alerts twice, and
  a per symbol+event cooldown (default 60 min) prevents spam across *different*
  bars (the one exception is the close-confirmed counterpart of a bar already
  alerted LIVE, see above).
* **Stale/lag guards** — symbols whose last bar is older than `max_stale_days`
  *trading* days are skipped (weekends don't count), as are symbols lagging more
  than `max_lag_minutes` behind the live NSE clock. Pre-open (or on a holiday)
  the newest bar is legitimately yesterday's, so that is treated as a warm-up
  pass, not as lag: intraday alerts only ever announce bars from the **current
  session**, which also stops a restarted scanner (fresh runner, evicted cache)
  from re-announcing yesterday's signals.
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
| `⛔ FILTERED OUT (size filters) — …` | the symbol is below `min_market_cap_cr` / `min_price` and is **not scanned at all** (raise the thresholds or set them to `0` to see it again) |
| `⏳ newest bar … is from a previous session` | pre-open/holiday: nothing new to alert yet (warm-up pass) |
| `armed FP-OBs: none` | no confirmed footprint OB — the composite needs a TAP, so nothing can fire (the 💧 eSSL touch alert does **not** need one) |
| `armed eSSL: none` | no active eSSL level to tap (all breached/expired) — neither the composite nor the 💧 touch alert can fire |
| both armed, far away | the setup is live but price has not reached the references yet |
| `→ essl_ob_tap @ …` | the composite **is** firing on a recent bar — check the Telegram credentials |
| `→ essl_tap @ … (price touched an eSSL level)` | the 💧 touch alert **is** firing on a recent bar |
| `scan` exits: `Telegram is NOT configured` | a live run would drop everything, so it refuses to start (use `--dry-run` to preview) |

The composite alert is deliberately strict (footprint TAP **and** eSSL tap on the
*same* bar — the "ALL RULES" condition), so expect a *low* rate rather than a
daily stream. Measured on the configured NSE universe (15m, 60 days of Yahoo
bars, ~1470 bars per symbol): 0–5 composite bars per symbol, i.e. roughly **one
alert per symbol per month**, clustered when price sweeps an external low into
an armed OB — plus `footprint_tap`/`essl_sweep`/`defence` events if you enable
them (5–22 TAPs and 10–15 sweeps per symbol per 60 days). The 💧 `essl_tap`
level-touch alert is deliberately *not* rare — it fires on every touch of every
active eSSL level, so expect it far more often than the composite (bounded by
the per-level `alert_cooldown_minutes` guard). `diagnose` prints both counts for
your universe:
`alert rate: 2 composite bar(s) in 1471 bars (0.14%); last 2026-08-26 15:00 | 💧 eSSL level touched on 45 bar(s)`.

---

## Data sources

| `data.source` | where it's used | notes |
|---|---|---|
| `yahoo` | **live use (default)** | daily (default) or intraday bars via `yfinance`, **raw unadjusted OHLC** (`auto_adjust: false`) so levels match the chart; full daily history from listing. `full_nse` universe fetched in one paced batch. NSE `.NS`, BSE `.BO`, US as-is. |
| `csv` | offline / own data | `data/<SYMBOL>.csv` with header `date,open,high,low,close,volume` (intraday: `date` may include `HH:MM`). Great for data you already have. With `full_nse` the marker scans every CSV present in `data/`. |
| `synthetic` | demos/tests in sealed environments | deterministic regime-switching generator (daily, or session-stamped 15m when the interval is intraday). **Not real data — never trade or draw conclusions from it.** |

> **Feed latency**: Yahoo's NSE intraday quotes are delayed (~15 min) and bars
> can print late, so a live alert can trail the tape by that much. Polling uses
> `scanner.max_lag_minutes` to skip a symbol whose feed has genuinely stalled,
> and `poll_minutes` controls how often a forming bar is re-read. Daily bars
> settle once per session, so the daily scanner polls through the session and
> re-checks the closing bar until `stop_after_close_minutes` after 15:30 IST.

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
python tests/test_engine.py             # 10 indicator-parity scenarios (daily bars)
python tests/test_fidelity_live.py      # 10 exact-match + live-NSE/intraday tests
python tests/test_live_scanner.py       # 10 end-to-end live-scanner tests (offline feed)
python tests/test_tap_filters.py        # 5 TAP #1 / fresh-OB filter tests
python tests/test_essl_touch_alerts.py  # 10 eSSL level-touch alert tests
python tests/test_universe.py           # full-NSE universe + batched-download tests
python tests/test_size_filters.py       # 15 size-filter / stop-after-pass tests
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
The fourth pins the TAP filters (TAP 2+ silent, old-OB TAP 1 silent, young-OB
TAP 1 alerts).
The fifth pins the 💧 **eSSL level-touch** alert: a touch with no footprint TAP
alerts, an old (non-fresh) level still alerts, a composite rejected by the TAP
filters still alerts its touch, a level the composite already reported is not
duplicated, two levels on one bar both alert, muting `essl_tap` still silences
it, and the engine taps an old level on the forming bar and the confirmed bar
alike under one `essl_tap_max_age` rule.
The sixth pins the **size filters** (price floor skips before the engine, market-cap
floor both ways with ₹1,000 Cr exactly filtered, fail-open vs fail-closed on an
unknown share count, `market_cap_cr_overrides`, the 📏 alert footer), the
share-count cache (round-trip, only-missing `prime()`, age-based refresh,
market cap = shares × price in ₹ crore) and the run mode: `exit_after_pass` ends
after one pass while the polling mode still naps between passes, and a stalled
feed is abandoned at `max_pass_minutes` instead of hanging the run.

## Project layout

```
FOOTPRINT ESSL.txt      the original Pine v6 script (source of truth)
ANALYSIS.md             deep analysis of the indicator
config.yaml             universe + scanner/backtest/telegram/engine config (NSE 15m live)
fpfssl/
  engine.py             1:1 port of the Pine state machines (+ eSSL event layer)
  events.py             event model + Telegram message formatting
  scanner.py            live polling scanner (IST clock, any TF), size filters, dedupe, alerts
  fundamentals.py        share counts / market cap for the size filters (cached)
  diag.py               `diagnose`: armed references / why-no-alert, per symbol
  backtest.py           signal trading + statistics + CSV reports
  data.py               yahoo (daily/intraday) / csv / synthetic loaders
  synthetic.py          deterministic offline data generator
  telegram.py           minimal Telegram HTTP notifier (dry-run capable)
  cli.py                `python -m fpfssl …` commands
tests/test_engine.py        indicator-parity scenario tests
tests/test_fidelity_live.py exact-match + live-NSE/intraday tests
tests/test_live_scanner.py  offline end-to-end live-scanner (fake feed/clock) tests
tests/test_size_filters.py  size filters (₹ crore / ₹ floors) + run-mode/ceiling tests
data/                   CSV data (git-ignored); `sample-data` fills it synthetically
state/                  scanner dedupe state (git-ignored)
output/                 backtest reports (git-ignored)
```

## Disclaimer

This is technical software for research. The indicator (and therefore this tool)
works on **proxies of institutional behaviour** — the source script itself says
institutional identity, order quantity and fills are *not observed*. Nothing here is
financial advice; past backtests do not guarantee future results.
