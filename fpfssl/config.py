"""Configuration for the FPFSSL8.2 Python engine / scanner / backtest.

EngineConfig mirrors the Pine v6 script "Footprint Source TAP + Fresh SSL +
Developing Preview v8.2" input-by-input (default values identical to the
script).  See ANALYSIS.md for what each group means.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ============================================================================
# Engine parameters (1:1 with the Pine inputs, defaults = script defaults)
# ============================================================================
@dataclass
class EngineConfig:
    # -- 1 - Primary footprint evidence ------------------------------------
    atr_length: int = 14
    volume_length: int = 20
    support_length: int = 10
    strong_rvol: float = 1.50
    evidence_window: int = 5
    minimum_evidence_bars: int = 2
    repeated_bar_rvol: float = 1.05
    repeated_mean_rvol: float = 1.20
    max_evidence_body: float = 0.50
    max_evidence_range: float = 1.50
    max_downside_progress: float = 0.50
    evidence_close_location: float = 0.60
    minimum_lower_wick: float = 0.20
    support_distance_atr: float = 1.0
    max_base_width_atr: float = 2.50

    # -- 2 - Required later price confirmation -----------------------------
    confirmation_structure: str = "Internal"          # "Internal" | "External"
    confirmation_deadline: int = 20
    departure_clearance: float = 0.10
    displacement_grace: int = 2
    minimum_displacement_range: float = 0.80
    minimum_displacement_body: float = 0.50
    displacement_close_location: float = 0.65
    break_clearance: float = 0.05
    break_grace: int = 2
    max_pending_setups: int = 24

    # -- 3 - Footprint / opposing-origin formation --------------------------
    origin_search: int = 12
    origin_padding: int = 3
    minimum_link_ticks: int = 1
    minimum_width_ticks: int = 2
    max_ob_width_atr: float = 2.50
    invalidation_mode: str = "Close"                  # "Close" | "Wick"
    invalidation_atr: float = 0.10
    invalidation_ticks: int = 1
    contact_departure_atr: float = 0.50
    max_contacts: int = 3
    max_zones: int = 12
    duplicate_overlap: float = 0.75

    # -- 5 - Original-script TAP compatibility (LIVE) -----------------------
    precision_zone_method: str = "Lower half of candle"  # Full|Open to low|Body to low|Body only|Lower half
    source_atr_length: int = 14
    source_volume_length: int = 20
    source_entry_mode: str = "Proximal"          # Proximal|50%|62%|70.5%|79%|Distal + 1 tick
    source_front_run_mode: str = "Auto"          # Auto|ATR|Ticks|Off
    source_front_run_atr: float = 0.18
    source_front_run_ticks: int = 2
    source_max_zone_buffer: float = 0.40
    source_entry_offset_ticks: int = 0
    source_stop_atr: float = 0.15
    source_approach_atr: float = 0.25
    source_min_age: int = 3
    source_max_touches: int = 4
    source_raise_after_first_tap: bool = True
    source_repeat_tap_atr: float = 0.05
    source_require_departure: float = 1.0
    source_confirm_bars: int = 3
    source_confirm_rvol: float = 1.3
    source_confirm_clv: float = 0.65
    source_confirm_bos_length: int = 3
    source_require_sweep: bool = False
    source_sweep_length: int = 5

    # -- 6 - Secondary internal / external SSL context ----------------------
    ssl_enabled: bool = True
    ssl_internal_depth: int = 3
    ssl_external_depth: int = 10
    ssl_show_equal: bool = True
    ssl_equal_atr: float = 0.05
    ssl_penetration_ticks: int = 1
    ssl_reclaim_ticks: int = 1
    ssl_max_age: int = 250
    ssl_record_cap: int = 30
    ssl_context_bars: int = 10
    ssl_context_distance: float = 1.0

    # -- 7 - FRESH SSL and developing-low preview ---------------------------
    fresh_show_confirmed: bool = True
    fresh_show_developing: bool = True
    fresh_low_source: str = "Both"               # Internal|External|Both
    fresh_record_cap: int = 40

    # -- 8 - Scanner alert extension (NOT in the Pine script) ---------------
    # eSSL tap semantics for the live scanner / backtest signals:
    essl_tap_buffer_ticks: int = 0      # extra ticks of "in front of" the level that still count as a tap
    # Only age limit on an eSSL tap, applied identically on the forming bar and
    # on confirmed bars. 250 == ssl_max_age, so with the defaults EVERY active
    # eSSL level alerts when touched — fresh or old. Lower it only if you want
    # to ignore levels that have been sitting there a long time.
    essl_tap_max_age: int = 250         # ignore eSSL pools older than this (bars)

    def validate(self) -> None:
        if self.ssl_external_depth <= self.ssl_internal_depth:
            raise ValueError("ssl_external_depth must exceed ssl_internal_depth (Pine runtime rule)")
        if self.minimum_evidence_bars > self.evidence_window:
            raise ValueError("minimum_evidence_bars cannot exceed evidence_window (Pine runtime rule)")
        if self.minimum_width_ticks < 1 or self.minimum_link_ticks < 1:
            raise ValueError("minimum_width_ticks / minimum_link_ticks must be >= 1")

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "EngineConfig":
        known = {f for f in EngineConfig.__dataclass_fields__}
        return EngineConfig(**{k: v for k, v in (d or {}).items() if k in known})


# ============================================================================
# Data / scanner / backtest / telegram configuration
# ============================================================================
@dataclass
class DataConfig:
    source: str = "yahoo"                # yahoo | csv | synthetic
    csv_dir: str = "data"
    # Minimum history the scanner wants. The LIVE scanner must NOT cut the
    # frame down to this: the engine's footprint-TAP state machine is
    # path-dependent (a zone born 600 bars ago can be tapped today), so every
    # bar the feed gives is state the live pass needs. `max_bars` is the only
    # hard cap (0 = keep everything the source returned).
    history_bars: int = 500
    max_bars: int = 0                    # hard cap on loaded bars (0 = unlimited)
    interval: str = "1d"                 # yfinance TF: 1m,2m,5m,15m,30m,60m,90m,1h,1d,1wk,1mo
    period: str | None = None            # yfinance period override (auto from interval when None)
    prepost: bool = False                # include pre/post market (NSE: keep False)
    start: str | None = None             # optional window start (YYYY-MM-DD); None = full history
    end: str | None = None
    # 100%-parity with the TradingView indicator: the Pine script runs on the
    # EXCHANGE's raw (unadjusted) NSE prices, so the feed must too. Adjusted
    # OHLC (splits/dividends) shifts every level the indicator would draw.
    auto_adjust: bool = False            # False = raw exchange OHLC (matches the chart)
    # Full-universe yahoo fetching: yfinance makes one HTTP request per
    # ticker, so a full-NSE pass is thousands of requests. `batch` fetches all
    # symbols of a scan pass in one go (threaded); batch_size paces the
    # requests in groups with batch_delay_sec between them.
    batch: bool = True                   # fetch every symbol of a pass together
    batch_size: int = 100                # symbols per download group (pacing only)
    batch_threads: int = 8               # concurrent ticker requests
    batch_delay_sec: float = 0.5         # pause between groups (Yahoo courtesy)
    # full_nse universe resolution (see fpfssl/universe.py)
    universe_cache_file: str = "data/nse_universe.csv"
    universe_max_age_days: float = 7.0   # reuse the cached symbol list this long
    tick_overrides: dict[str, float] = field(default_factory=dict)  # symbol -> tick size
    # share counts for the scanner's market-cap size filter (see fpfssl/fundamentals.py).
    # A share count changes ~quarterly, so it is cached like the symbol list and
    # market cap is recomputed as shares x last close on every pass.
    fundamentals_cache_file: str = "data/nse_fundamentals.csv"
    fundamentals_max_age_days: float = 30.0

    def is_intraday(self) -> bool:
        # yfinance: 1m = 1 minute (intraday), 1mo = 1 month (not intraday)
        return self.interval.lower() not in ("1d", "d", "1wk", "1w", "1mo", "daily")


@dataclass
class ScannerConfig:
    poll_minutes: float = 15.0           # daily cadence (intraday TFs can go lower)
    state_file: str = "state/scanner_state.json"
    alert_events: list[str] = field(default_factory=lambda: [
        "essl_ob_tap",    # composite: eSSL tap on a bar where a footprint TAP also fires (ALL RULES)
        "essl_tap",       # price TOUCHED an active eSSL level (fresh or old; no footprint TAP needed)
        "essl_sweep",     # confirmed eSSL sweep / gap reclaim (liquidity grab + reclaim)
        "footprint_tap",  # source-compatible TAP on any confirmed FP-OB
        "defence",        # source defence confirmation after a TAP
        "zone_invalid",   # zone invalidated (stop) or touch limit exceeded
        "essl_created",   # new eSSL reference published (fresh major low)
    ])
    provisional_alerts: bool = True      # also alert on the still-forming bar (marked LIVE)
    alert_cooldown_minutes: float = 60.0
    # TAP signal filters (apply to `essl_ob_tap` composite + `footprint_tap`).
    # tap_first_only: only TAP 1/N on a zone alerts; TAP 2/3/4 stay silent.
    # fresh_ob_only: only alert when the tapped OB itself is young
    # (tap_bar - ob_born_bar <= fresh_ob_max_age_bars). An old zone tapped
    # for the first time years later stays silent.
    # NB: neither filter touches `essl_tap` — an eSSL LEVEL touch alerts on
    # every touch of every active level, fresh or old. A composite rejected by
    # these filters still produces its eSSL touch alert.
    tap_first_only: bool = False
    fresh_ob_only: bool = False
    fresh_ob_max_age_bars: int = 50      # OB freshness window, in bars of data.interval
    min_bars: int = 300                  # skip symbols with too little history (engine warmup)
    max_stale_days: int = 4              # skip symbols whose last bar is older than this (trading-day aware)
    max_lag_minutes: int = 90            # intraday: skip when market is open but feed lags more than this
    # -- size filters (universe hygiene: skip names too small to be tradeable) --
    # Applied per symbol AFTER the bars are loaded and BEFORE the engine runs, so
    # a filtered name costs no engine time. Both are strictly-greater-than:
    #   min_market_cap_cr: keep a symbol only if shares x last close > this (₹ crore)
    #   min_price:         keep a symbol only if its last close > this (₹)
    # 0 disables that filter (so a config written before these existed behaves
    # exactly as before). Market cap comes from the cached share count
    # (fpfssl/fundamentals.py) — see `data.fundamentals_cache_file`.
    min_market_cap_cr: float = 0.0
    min_price: float = 0.0
    # When no share count can be resolved for a symbol: true = scan it anyway
    # (a metadata outage must not silently swallow alerts), false = skip it.
    keep_unknown_market_cap: bool = True
    # Pin/override a symbol's market cap in ₹ crore (offline runs, or to keep a
    # name in the scan even though the feed reports no share count).
    market_cap_cr_overrides: dict[str, float] = field(default_factory=dict)
    market_timezone: str = "Asia/Kolkata"  # NSE/BSE live clock (IST)
    market_open: str = "09:15"           # NSE equity session open (IST, HH:MM)
    market_close: str = "15:30"          # NSE equity session close (IST, HH:MM)
    recent_bars: int = 3                 # only the last N bars can raise new alerts
    # `scan` without --once polls until this many minutes past the close, so the
    # final bar (15:15-15:30) is still alerted once the feed settles.
    stop_after_close_minutes: float = 15.0
    # `scan` without --once waits for the open only when it is this close;
    # started further out it runs a single pass and exits (scheduled ticks are
    # cheap, holding a 6h runner for nothing is not).
    preopen_wait_minutes: float = 60.0
    # Hard wall-clock budget for `scan` without --once: after this many minutes
    # the poller saves its dedup state and exits cleanly, even if the session
    # has not ended. 0 = no limit (the market clock alone decides). CI sets it
    # below the job timeout so a long job always ends on its own instead of
    # being killed mid-pass.
    max_runtime_minutes: float = 0.0
    # exit_after_pass: `scan` without --once stops after ONE complete pass
    # instead of napping until the close.
    #   false (the shipped default) = the run IS the session worker: it polls
    #     until `stop_after_close_minutes` past the close, or until
    #     `max_runtime_minutes` cuts it (then `scanner.reschedule_in_ci` hands
    #     the rest of the session to a re-armed run). GitHub's `schedule`
    #     trigger delivers only a fraction of its ticks — this repo sees ~1 of
    #     40 — so "one tick, one pass" leaves most of the session unscanned and
    #     reads as "the scanner sends nothing". Polling inside one bounded job is
    #     what actually covers the session.
    #   true = cheapest, most resumable mode (a pass per trigger, no runner
    #     held). Use it when an external scheduler pings `repository_dispatch`
    #     every few minutes, or with `reschedule_in_ci` as the pinger.
    exit_after_pass: bool = False
    # Ceiling for a single pass (batch fetch + engine loop). The batched yahoo
    # fetch waits on this and abandons a stalled download instead of hanging
    # forever; the per-symbol loop stops at the next symbol boundary. 0 = off.
    max_pass_minutes: float = 0.0
    # A failed pass (the feed stalled, the batch download raised) must NOT end
    # the session worker: the next poll retries it. Only after this many
    # *consecutive* failed passes does the run give up (state saved) — so a
    # 10-minute yahoo hiccup cannot turn into "the scanner never ran again
    # today", while a real outage still ends the job cleanly.
    max_pass_failures: int = 6
    # When one pass cannot finish the universe inside `max_pass_minutes`, keep
    # going where it stopped instead of re-scanning the same first N symbols on
    # every run: the scan order is rotated by a cursor persisted in the state
    # file. Without it, the tail of an alphabetically sorted universe can never
    # alert — the ceiling cuts the pass at the same place every time.
    rotate_universe: bool = True
    # Machine-readable per-pass report (what was scanned, what was filtered, how
    # many alerts were delivered). The Scanner workflow appends it to the job
    # summary so "no alerts" is explainable from the run page. Default "" so an
    # in-process / test run never writes into a real state directory; the shipped
    # config.yaml turns it on.
    report_file: str = ""
    # Self re-arm: when a session-long run stops while the market is still open
    # (runtime budget, a stalled pass, the pass ceiling), dispatch one more
    # Actions run instead of waiting for a cron tick — GitHub delivers scheduled
    # ticks late or not at all, and session coverage depends on it. Needs
    # GITHUB_TOKEN + GITHUB_REPOSITORY in the environment; a no-op anywhere else
    # (see fpfssl/ci.py).
    reschedule_in_ci: bool = True
    reschedule_max_runs_per_day: int = 40    # runaway-loop guard, per market day


@dataclass
class TelegramConfig:
    enabled: bool = True
    dry_run: bool = False                # True -> print messages instead of sending
    token: str | None = None             # or env TELEGRAM_BOT_TOKEN
    chat_id: str | None = None           # or env TELEGRAM_CHAT_ID
    api_base: str = "https://api.telegram.org"
    timeout: float = 15.0
    # ---- delivery limits (Telegram allows ~1 message/second PER CHAT) -------
    # A full-universe pass can raise hundreds of taps at once. Sending them
    # back-to-back used to end in `429 Too Many Requests`, and every rejected
    # message was simply lost — a green run that delivered nothing. The notifier
    # now paces itself, honours `retry_after` and retries, so an alert is
    # deferred instead of dropped (see fpfssl/telegram.py).
    min_interval_sec: float = 1.0        # spacing between two sends to this chat
    max_retries: int = 4                 # attempts per message (429/5xx/network)
    retry_backoff_sec: float = 2.0       # base for the exponential backoff
    max_wait_sec: float = 90.0           # total waiting budget per message
    # After a message exhausts its retries, stop talking to Telegram for this
    # long: a chat that is refusing sends must not turn a scan pass into hours of
    # waiting. Deferred alerts are retried on the next pass (they were never
    # recorded as sent), so this costs latency, not signals.
    mute_after_failure_sec: float = 60.0


@dataclass
class BacktestConfig:
    start: str | None = None             # optional trading-window start (YYYY-MM-DD)
    strategy: str = "essl_ob_tap"        # essl_ob_tap | ob_tap | essl_sweep
    entry: str = "next_open"             # next_open | bar_close
    rr: float = 2.0                      # take-profit in R multiples (<=0 disables target)
    max_bars: int = 30                   # time exit
    risk_per_trade: float = 0.01         # fraction of equity risked (for the equity curve)
    initial_capital: float = 100_000.0
    sweep_stop_atr: float = 0.5          # essl_sweep strategy: stop below level, in ATR multiples
    entry_tap: int = 1                   # ob_tap strategy: which tap number to enter on (0 = any)
    one_position_per_symbol: bool = True


@dataclass
class AppConfig:
    data: DataConfig = field(default_factory=DataConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    symbols: list[str] = field(default_factory=lambda: [
        # NSE large caps (Yahoo suffix .NS) — live Indian market universe
        "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
        "SBIN.NS", "ITC.NS", "BHARTIARTL.NS", "LT.NS", "ASIANPAINT.NS",
    ])


def _mk(dc, d):
    """Build a config section, warning about — not swallowing — unknown keys.

    A typo like `min_market_cap: 500` next to the real `min_market_cap_cr` used
    to be dropped in silence: the filter the user thinks is configured is not,
    and a setting that never loads is indistinguishable from a working default.
    """
    import logging

    d = d or {}
    known = {f for f in dc.__dataclass_fields__}
    unknown = [k for k in d if k not in known]
    if unknown:
        logging.getLogger("fpfssl.config").warning(
            "%s: ignoring unknown config key(s) %s (valid: %s)", dc.__name__,
            ", ".join(sorted(unknown)), ", ".join(sorted(known)))
    return dc(**{k: v for k, v in d.items() if k in known})


def load_config(path: str | None = "config.yaml") -> AppConfig:
    import os

    cfg = AppConfig()
    if path and os.path.exists(path):
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        cfg.symbols = list(raw.get("symbols", cfg.symbols))
        cfg.data = _mk(DataConfig, raw.get("data"))
        cfg.scanner = _mk(ScannerConfig, raw.get("scanner"))
        cfg.telegram = _mk(TelegramConfig, raw.get("telegram"))
        cfg.backtest = _mk(BacktestConfig, raw.get("backtest"))
        cfg.engine = EngineConfig.from_dict(raw.get("engine"))
    # environment overrides for credentials
    if not cfg.telegram.token:
        cfg.telegram.token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not cfg.telegram.chat_id:
        cfg.telegram.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    cfg.engine.validate()
    return cfg
