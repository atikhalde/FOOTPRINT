"""Live scanner: poll Yahoo bars, detect FPFSSL8.2 events, alert via Telegram.

Timeframe-agnostic (the Pine indicator runs on any TF): `data.interval`
selects daily (`1d`) or intraday (`15m`, `5m`, `1h`, ...) bars. The live clock
is the configured market timezone (default Asia/Kolkata for NSE/BSE).

Flow per poll, per symbol:
  1. fetch OHLCV (last bar = still-forming bar when the market is live)
  2. size filters: `scanner.min_price` (last close in ₹) and
     `scanner.min_market_cap_cr` (cached share count x that close, ₹ crore —
     fpfssl/fundamentals.py) skip the symbol BEFORE any engine work, so a
     ₹40 penny stock on a small float costs nothing and cannot alert
  3. run the faithful engine over the whole history
  4. group events by bar; detect the composite ALL-RULES condition:
       eSSL tap  AND  footprint-source TAP  on the SAME bar
     plus the standalone eSSL LEVEL TOUCH (`essl_tap`): price reached an active
     eSSL level — fresh or old, with or without a footprint TAP that bar, and
     never gated by tap_first_only / fresh_ob_only (those filter the footprint
     side only). A composite that the TAP filters reject therefore still
     produces its eSSL touch alert. "Active" is the indicator's own state: a
     bar that penetrates the level ≥ 1 tick and closes back above it is a
     sweep-and-reclaim (a valid tap), but a bar that closes BELOW the level
     retires it at that close (first full penetration is terminal) — that
     outcome goes to `essl_break`, never to the touch channel, and a forming
     bar that is already below the level waits for the close instead of
     alerting mid-break.
  5. dedupe against persisted state, apply cooldowns, format, send Telegram

Dedup key: symbol | kind | bar-time | confirmed? | zone-or-pool-id
Provisional (forming-bar) alerts carry a distinct key, so after the bar closes
the scanner sends one follow-up "confirmed" version.

Run mode: `scanner.exit_after_pass: true` (the shipped default) means ONE
complete pass per run — save the dedup state and exit — instead of holding the
process (and a CI job's `concurrency` group) until the close; `--keep-polling`
restores the session-long poller. `scanner.max_pass_minutes` bounds a single
pass, so a stalled feed cannot hang either mode.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

from .config import AppConfig
from .data import DataError, load_all, load_symbol
from .engine import Engine, detect_tick
from .events import (
    K_DEFENCE,
    K_ESSL_BREAK,
    K_ESSL_SWEEP,
    K_ESSL_TAP,
    K_FOOTPRINT,
    K_SSL_CREATED,
    K_TAP,
    K_ZONE_INVALID,
    format_composite,
    format_event,
)
from .fundamentals import FundamentalTable, SizeFilters
from .telegram import TelegramNotifier

log = logging.getLogger("fpfssl.scanner")


def timeframe_label(interval: str) -> str:
    m = {
        "1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
        "60m": "1H", "90m": "90m", "1h": "1H",
        "1d": "Daily", "d": "Daily",
        "1wk": "Weekly", "1w": "Weekly", "1mo": "Monthly",
    }
    return m.get((interval or "").lower(), interval or "Daily")


def interval_minutes(interval: str) -> float | None:
    m = {
        "1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
        "60m": 60, "90m": 90, "1h": 60,
        "1d": 24 * 60, "d": 24 * 60,
        "1wk": 7 * 24 * 60, "1w": 7 * 24 * 60, "1mo": 30 * 24 * 60,
    }
    return m.get((interval or "").lower())


def market_now(cfg: AppConfig) -> datetime:
    """Current time in the configured market timezone (naive wall time)."""
    tzname = cfg.scanner.market_timezone or "Asia/Kolkata"
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tzname)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - no tzdata: IST = UTC+5:30
        return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _parse_hhmm(s: str, default: str) -> tuple[int, int]:
    try:
        h, m = (s or default).split(":")
        return int(h), int(m)
    except Exception:  # noqa: BLE001
        h, m = default.split(":")
        return int(h), int(m)


def market_is_open(cfg: AppConfig, now: datetime | None = None) -> bool:
    """NSE equity session check: Mon-Fri 09:15-15:30 market time."""
    now = now if now is not None else market_now(cfg)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    oh, om = _parse_hhmm(cfg.scanner.market_open, "09:15")
    ch, cm = _parse_hhmm(cfg.scanner.market_close, "15:30")
    t = (now.hour, now.minute)
    return (oh, om) <= t < (ch, cm)


def _session_bounds(cfg: AppConfig, now: datetime) -> tuple[datetime, datetime]:
    """Today's session open/close as naive datetimes in market time."""
    oh, om = _parse_hhmm(cfg.scanner.market_open, "09:15")
    ch, cm = _parse_hhmm(cfg.scanner.market_close, "15:30")
    return (now.replace(hour=oh, minute=om, second=0, microsecond=0),
            now.replace(hour=ch, minute=cm, second=0, microsecond=0))


def minutes_until_close(cfg: AppConfig, now: datetime | None = None) -> float:
    now = now if now is not None else market_now(cfg)
    _, close_dt = _session_bounds(cfg, now)
    return (close_dt - now).total_seconds() / 60.0


def minutes_until_open(cfg: AppConfig, now: datetime | None = None) -> float | None:
    """Minutes until the next session open (None on weekends)."""
    now = now if now is not None else market_now(cfg)
    open_dt, close_dt = _session_bounds(cfg, now)
    if now < open_dt:
        return (open_dt - now).total_seconds() / 60.0
    # after the close (or a weekend): next weekday's open
    days = 0
    probe = now
    while days < 7:
        probe = probe + timedelta(days=1)
        days += 1
        if probe.weekday() < 5:
            nxt_open, _ = _session_bounds(cfg, probe)
            return (nxt_open - now).total_seconds() / 60.0
    return None


def _session_finished(cfg: AppConfig, now: datetime) -> bool:
    """True once the close (plus a settling grace) has passed.

    The grace matters: the final 15m bar closes at 15:30, and Yahoo needs a
    few minutes before the closing bar is final — the scanner keeps polling
    through `stop_after_close_minutes` so the last bar still gets alerted.
    """
    if now.weekday() >= 5:
        return True
    return minutes_until_close(cfg, now) < -abs(cfg.scanner.stop_after_close_minutes)


def is_live_last_bar(cfg: AppConfig, last_bar_time, now: datetime | None = None) -> bool:
    """Is the feed's last bar still forming? (Pine barstate.isconfirmed=False).

    * Daily: today's bar is forming while now < close + buffer.
    * Intraday: the bar is forming while now < bar_open + interval + buffer.
    On weekends/holidays — or when the feed lags past the bar end — the last
    bar is already closed, so events on it are confirmed, not LIVE.
    """
    if cfg.data.source != "yahoo":
        return False
    now = now if now is not None else market_now(cfg)
    last = pd.Timestamp(last_bar_time).to_pydatetime()
    interval = (cfg.data.interval or "1d").lower()
    buf = timedelta(minutes=2)
    if interval in ("1d", "d"):
        if last.date() != now.date():
            return False
        ch, cm = _parse_hhmm(cfg.scanner.market_close, "15:30")
        close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
        return now < close_dt + timedelta(minutes=15)
    if interval in ("1wk", "1w", "1mo"):
        # weekly/monthly bars are always "forming" intra-period; treat the
        # last bar as provisional only while the market is open today.
        return bool(market_is_open(cfg, now))
    mins = interval_minutes(interval)
    if not mins:
        return False
    bar_end = last + timedelta(minutes=mins)
    return now < bar_end + buf


def trading_days_between(a, b) -> int:
    """Weekday (Mon-Fri) count in (a, b] for stale-data purposes."""
    a, b = pd.Timestamp(a).date(), pd.Timestamp(b).date()
    if b <= a:
        return 0
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def watch_lines(res, cfg: AppConfig, tick: float, limit: int = 3) -> list[str]:
    """One log line describing what is armed right now.

    A silent scan pass is otherwise indistinguishable from a broken one: this
    prints the nearest live TAP reference, the nearest active eSSL level and
    the distance price still has to travel, so `scan` output explains itself.
    """
    price = float(res.close[-1])
    zones = []
    for z in res.zones:
        if not z.active:
            continue
        ref = float(z.source_reference)
        zones.append((abs(price - ref),
                      "FP-OB #%d ref %.2f (band %.2f-%.2f, %s, taps %d) price %+.2f%% away"
                      % (z.id, ref, z.bottom, z.top,
                         "departed" if z.source_departed else "awaiting departure",
                         z.source_taps, (price - ref) / price * 100.0)))
    pools = []
    for p in res.pools:
        if p.active and p.scope == 1:
            lvl = float(p.lower)
            pools.append((abs(price - lvl),
                          "eSSL #%d level %.2f (%d member(s)) price %+.2f%% away"
                          % (p.id, lvl, p.members, (price - lvl) / price * 100.0)))
    zones.sort(key=lambda x: x[0])
    pools.sort(key=lambda x: x[0])
    if not zones and not pools:
        return ["armed: nothing — no active FP-OB and no active eSSL level, "
                "so no TAP/sweep/eSSL event can fire yet"]
    out = []
    if zones:
        out.append("armed FP-OBs: " + " | ".join(t for _, t in zones[:limit]))
    else:
        out.append("armed FP-OBs: none (composite cannot fire without a TAP)")
    if pools:
        out.append("armed eSSL:   " + " | ".join(t for _, t in pools[:limit]))
    else:
        out.append("armed eSSL:   none")
    return out


class LiveScanner:
    def __init__(self, cfg: AppConfig, notifier: TelegramNotifier, symbols: list[str] | None = None,
                 fundamentals: "FundamentalTable | None" = None,
                 refresh_fundamentals: bool = False):
        self.cfg = cfg
        self.notifier = notifier
        self.symbols = symbols or list(cfg.symbols)
        self.state: dict = {"alerted": {}, "cooldown": {}}
        self.filtered = 0          # symbols the size filters dropped this pass
        self.scanned = 0           # symbols that reached the engine this pass
        self._refresh_fundamentals = bool(refresh_fundamentals)
        self._init_runtime()
        self._load_state()
        self.fundamentals = fundamentals

    @property
    def exit_after_pass(self) -> bool:
        """`scanner.exit_after_pass`: stop the run when a pass completes."""
        return bool(getattr(self.cfg.scanner, "exit_after_pass", False))

    # -- size filters (market cap / price) ------------------------------------
    @property
    def filters(self) -> SizeFilters:
        """`scanner.min_market_cap_cr` / `scanner.min_price` as a check object."""
        if getattr(self, "_filters", None) is None:
            self._filters = SizeFilters.from_config(self.cfg.scanner)
        return self._filters

    def _fund_table(self) -> FundamentalTable:
        """Share-count table (disk-cached). Tests may inject one via the ctor."""
        if getattr(self, "fundamentals", None) is None:
            self.fundamentals = FundamentalTable(
                cache_file=getattr(self.cfg.data, "fundamentals_cache_file", "") or "",
                max_age_days=float(getattr(self.cfg.data, "fundamentals_max_age_days", 30.0) or 0),
            ).load()
        return self.fundamentals

    def size_ok(self, sym: str, price: float) -> tuple[bool, str]:
        """`(keep, reason)` for the scanner's size filters (no network here)."""
        return self.filters.check(sym, price, self._fund_table())

    def size_note(self, sym: str, price: float) -> str:
        """One-line size summary appended to alerts, so a message explains that
        the symbol cleared the filters (and what it is worth)."""
        cr = self.filters.market_cap_cr(sym, price, self._fund_table())
        bits = []
        if price and price > 0:
            bits.append(f"price ₹{price:,.2f}")
        if cr:
            bits.append(f"mcap ₹{cr:,.0f} Cr")
        if not bits:
            return ""
        return ("\n\n📏 <b>size</b> " + " · ".join(bits)
                + f" · filter: {self.filters.describe()}")

    def _size_tail(self, sym: str, price: float) -> str:
        """Size footer for an alert (empty when the filters are off).

        Every alert states the stock it cleared: ₹1,432 price / ₹9,50,000 Cr
        market cap, plus the thresholds in force — so a message is verifiable
        against config.yaml without re-running `diagnose`.
        """
        if not self.filters.enabled:
            return ""
        return self.size_note(sym, price)

    def prime_fundamentals(self, symbols: list[str], frames: dict[str, pd.DataFrame],
                           refresh: bool = False) -> int:
        """Fetch share counts for the symbols worth asking about (once per pass).

        Only symbols that clear the *price* filter can reach the market-cap
        filter, so those are the only ones fetched — over the full NSE list the
        ₹100 floor typically halves the metadata work. Nothing here blocks a
        scan: a failed fetch just leaves those symbols without a share count
        (and `keep_unknown_market_cap` decides whether they are scanned).
        """
        flt = self.filters
        if not flt.mcap_on or self.cfg.data.source != "yahoo":
            return 0
        table = self._fund_table()
        cands = [s for s in symbols
                 if s in frames and (flt.min_price <= 0
                                     or float(frames[s]["close"].iloc[-1]) > flt.min_price)]
        return table.prime(cands, refresh=refresh, fail_open=flt.fail_open, cfg=self.cfg)

    # -- cancellation / runtime budget ----------------------------------------
    def _init_runtime(self) -> None:
        """Create the stop flag, handler bookkeeping and runtime deadline.

        Idempotent: tests build a scanner with ``__new__`` (no ``__init__``),
        and `run_forever` calls this again so a stop request always has an
        Event to set even on such an instance.
        """
        if getattr(self, "_stop", None) is None:
            self._stop = threading.Event()
        if getattr(self, "_stop_reason", None) is None:
            self._stop_reason = ""
        if getattr(self, "_saved_handlers", None) is None:
            self._saved_handlers: dict[int, object] = {}
        if not hasattr(self, "_runtime_deadline"):
            self._runtime_deadline = self._compute_deadline()

    def _compute_deadline(self) -> float | None:
        """monotonic() deadline from `scanner.max_runtime_minutes` (None = none)."""
        minutes = float(getattr(self.cfg.scanner, "max_runtime_minutes", 0) or 0)
        return time.monotonic() + minutes * 60.0 if minutes > 0 else None

    @property
    def stop_requested(self) -> bool:
        """True once Ctrl-C / SIGTERM (or `request_stop`) asked us to stop."""
        self._init_runtime()
        return self._stop.is_set()

    @property
    def stop_reason(self) -> str:
        self._init_runtime()
        return self._stop_reason

    def request_stop(self, reason: str = "stop requested") -> None:
        """Ask the scanner to stop at the next checkpoint (thread-safe)."""
        self._init_runtime()
        if not self._stop.is_set():
            self._stop_reason = reason
            self._stop.set()

    def _handle_stop_signal(self, signum, _frame) -> None:
        try:
            name = signal.Signals(signum).name
        except Exception:  # noqa: BLE001
            name = str(signum)
        self.request_stop(f"received {name}")

    def install_stop_handlers(self) -> None:
        """Make SIGINT/SIGTERM stop the loop — even when they were ignored.

        A process started in the background by a non-interactive shell (CI
        runners, `... &`, nohup, systemd) inherits SIGINT set to SIG_IGN, and
        Python then never installs its KeyboardInterrupt handler: Ctrl-C /
        "Cancel workflow" are silently swallowed and the scanner only stops
        when the job is killed. Installing the handler explicitly overrides the
        inherited disposition, so a stop request is always honoured.
        """
        self._init_runtime()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._saved_handlers[sig] = signal.signal(sig, self._handle_stop_signal)
            except (ValueError, OSError, AttributeError):  # not the main thread
                log.debug("could not install a handler for signal %s", sig)

    def restore_stop_handlers(self) -> None:
        for sig, handler in list(getattr(self, "_saved_handlers", {}).items()):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):  # noqa: BLE001
                pass
        self._saved_handlers = {}

    def out_of_time(self) -> bool:
        """True once the `max_runtime_minutes` budget is spent."""
        self._init_runtime()
        deadline = self._runtime_deadline
        return deadline is not None and time.monotonic() >= deadline

    def _stop_now(self) -> str:
        """Reason to stop right now — "" while the poller may keep going."""
        if self.stop_requested:
            return self.stop_reason or "stop requested"
        if self.out_of_time():
            return "runtime limit reached"
        return ""

    def _sleep(self, seconds: float) -> bool:
        """Sleep in short chunks so a stop request wakes us within a second.

        Returns True when the full nap elapsed, False when it was cut short by
        a stop request. Chunking (instead of one long `time.sleep`) is what
        makes "cancel" feel instant during the 15-minute poll nap; the elapsed
        total is accumulated from the chunks (not from the wall clock) so a
        patched/faked `time.sleep` in tests stays in control of the clock.
        """
        self._init_runtime()
        seconds = max(0.0, float(seconds))
        slept = 0.0
        while slept < seconds:
            # wake for a stop request *and* for an expired runtime budget:
            # otherwise a cancel would have to wait out the whole nap
            if self._stop.is_set() or self.out_of_time():
                return False
            chunk = min(1.0, seconds - slept)
            time.sleep(chunk)
            slept += chunk
        return not (self._stop.is_set() or self.out_of_time())

    # -- state ---------------------------------------------------------------
    def _load_state(self):
        p = self.cfg.scanner.state_file
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    self.state = json.load(fh)
            except Exception:  # noqa: BLE001
                log.warning("could not read state file %s; starting fresh", p)
        self.state.setdefault("alerted", {})
        self.state.setdefault("cooldown", {})

    def _save_state(self):
        p = self.cfg.scanner.state_file
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        # keep the file bounded
        alerted = self.state["alerted"]
        if len(alerted) > 20000:
            cutoff = (datetime.now() - timedelta(days=14)).isoformat()
            alerted = {k: v for k, v in alerted.items() if v >= cutoff}
            self.state["alerted"] = alerted
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)

    # -- alerting ------------------------------------------------------------
    def _try_alert(self, sym: str, kind: str, message: str, dedup_key: str,
                   cooldown_key: str | None = None,
                   follow_up_of: str | None = None) -> bool:
        """Send one alert unless it was already sent or is inside its cooldown.

        `cooldown_key` defaults to `symbol|kind` (one stream per event type).
        Callers pass a finer key when several *distinct objects* of the same
        kind are alertable at once — e.g. two different eSSL levels touched on
        the same bar — so one level's alert cannot swallow the other's.

        `follow_up_of` is the dedup key of the *provisional* (LIVE) alert this
        message is the confirmed counterpart of. The LIVE alert is a guess made
        at an intrabar price; the confirmed one carries the bar's actual close,
        which is the state the indicator itself shows — `provisional_alerts`
        documents "alert on the still-forming bar too, then again when
        confirmed". The per-symbol+kind cooldown must not eat that follow-up
        (on the shipped daily timeframe the confirming pass always lands within
        `alert_cooldown_minutes` of the last intraday poll, so every
        LIVE-then-confirmed pair used to collapse into the LIVE half and the
        close-confirmed alert was silently dropped). It is still deduped, so
        this can send at most ONE follow-up per bar+object, never a stream.
        """
        sc = self.cfg.scanner
        now = datetime.now()
        if dedup_key in self.state["alerted"]:
            return False
        cd_key = cooldown_key or f"{sym}|{kind}"
        last = self.state["cooldown"].get(cd_key)
        if last and not (follow_up_of and follow_up_of in self.state["alerted"]):
            try:
                if now - datetime.fromisoformat(last) < timedelta(minutes=sc.alert_cooldown_minutes):
                    return False
            except Exception:  # noqa: BLE001
                pass
        ok = self.notifier.send(message)
        if ok:
            # In-memory dedupe/cooldown always applies (so a --dry-run preview
            # shows exactly what a live pass would send), but dry runs never
            # touch the persisted state file — previewing must not consume
            # live alerts.
            self.state["alerted"][dedup_key] = now.isoformat()
            self.state["cooldown"][cd_key] = now.isoformat()
            if not self.cfg.telegram.dry_run:
                self._save_state()
        return ok

    # -- single symbol pass ----------------------------------------------------
    def scan_symbol(self, sym: str, df: pd.DataFrame | None = None) -> int:
        cfg = self.cfg
        tf = timeframe_label(cfg.data.interval)
        if df is None:
            try:
                df = load_symbol(sym, cfg.data)
            except DataError as e:
                log.warning("%s: %s", sym, e)
                return 0
        if len(df) < cfg.scanner.min_bars:
            log.info("%s: only %d bars (< %d), skipped", sym, len(df), cfg.scanner.min_bars)
            return 0
        # ---- size filters (market cap / price) ------------------------------
        # Before the staleness checks and, above all, before the engine: the
        # whole point of `min_market_cap_cr` / `min_price` is that a stock that
        # cannot produce a tradeable alert costs nothing. Price is the last
        # close of the frame the pass already fetched (₹, raw exchange price),
        # market cap is that price x the cached share count.
        price_last = float(df["close"].iloc[-1])
        keep, why = self.size_ok(sym, price_last)
        if not keep:
            self.filtered = getattr(self, "filtered", 0) + 1
            log.info("%s: skipped by the size filters — %s", sym, why)
            return 0
        self.scanned = getattr(self, "scanned", 0) + 1
        now_mkt = market_now(cfg)
        last_ts = df.index[-1]
        if cfg.data.source == "yahoo":
            stale_days = trading_days_between(last_ts, now_mkt)
            if stale_days > cfg.scanner.max_stale_days:
                log.warning("%s: last bar %s is %d trading days old, skipped",
                            sym, last_ts, stale_days)
                return 0
            if cfg.data.is_intraday() and market_is_open(cfg, now_mkt):
                open_dt, _ = _session_bounds(cfg, now_mkt)
                if pd.Timestamp(last_ts).to_pydatetime() >= open_dt:
                    # the feed is inside today's session -> a large gap is real lag
                    lag_min = (now_mkt - pd.Timestamp(last_ts).to_pydatetime()).total_seconds() / 60.0
                    if lag_min > cfg.scanner.max_lag_minutes:
                        log.warning("%s: feed lags %.0f min (last bar %s), skipped",
                                    sym, lag_min, last_ts)
                        return 0
                else:
                    log.info("%s: no bar from today's session yet (last %s) — "
                             "holiday or feed not started", sym, last_ts)
        # A restarted scanner (fresh Actions runner, evicted dedup cache) would
        # otherwise re-announce the last `recent_bars` bars of a *previous*
        # session as if they were news. Intraday alerts must belong to the
        # current session: the last bar has to carry today's date, and even
        # then events whose stamp is before today's open are dropped (the
        # first 09:15 print still has yesterday's 15:00/15:15 in a 3-bar
        # window).
        session_open = None
        if cfg.data.source == "yahoo" and cfg.data.is_intraday():
            if pd.Timestamp(last_ts).date() != now_mkt.date():
                log.info("%s: newest bar %s is from a previous session — warm-up only, "
                         "nothing to alert yet", sym, last_ts)
                return 0
            session_open, _ = _session_bounds(cfg, now_mkt)
        # NB: never cut the frame down to history_bars here. The engine's
        # footprint/TAP state machine is path-dependent — an OB born 600 bars
        # ago can be the TAP reference that fires today — so every bar the
        # feed serves is live state. `data.max_bars` is the only cap.
        tick = cfg.data.tick_overrides.get(sym, detect_tick(df, sym))
        live_last = is_live_last_bar(cfg, df.index[-1], now_mkt)
        res = Engine(sym, cfg.engine, tick, tf=cfg.data.interval).run(df, live_last_bar=live_last)
        c = res.counters
        fmt = "%Y-%m-%d %H:%M" if cfg.data.is_intraday() else "%Y-%m-%d"
        log.info("%s [%s]: %d bars %s → %s%s | OBs %d (active %d) | taps %d | eSSL taps %d | active eSSL %d | fresh %d",
                 sym, tf, len(df), df.index[0].strftime(fmt), last_ts.strftime(fmt),
                 " (LIVE last bar)" if live_last else "",
                 c.get("footprint_ob_created", 0), c.get("active_zones", 0),
                 c.get("taps", 0), c.get("essl_taps", 0), c.get("active_e_ssl", 0), c.get("fresh_active", 0))
        for line in watch_lines(res, cfg, tick):
            log.info("%s: %s", sym, line)

        sc = cfg.scanner
        want = set(sc.alert_events or [])
        sent = 0
        cutoff_bar = len(df) - max(1, int(sc.recent_bars or 3))
        # TAP filters (tap_first_only / fresh_ob_only) need the tapped zone's
        # birth bar to measure OB age: tap_bar - ob_born_bar.
        zone_by_id = {z.id: z for z in res.zones}

        def _tap_ok(tap_ev) -> tuple[bool, str]:
            tap_no = tap_ev.extra.get("taps", 1)
            if sc.tap_first_only and tap_no != 1:
                return False, f"TAP {tap_no}/{tap_ev.extra.get('max_taps', '?')} (first-tap-only filter)"
            if sc.fresh_ob_only:
                zone = zone_by_id.get(tap_ev.zone_id)
                if zone is None:
                    return False, "tapped OB not found (fresh-OB-only filter)"
                max_age = max(0, int(sc.fresh_ob_max_age_bars or 0))
                age = tap_ev.bar - zone.born_bar
                if age > max_age:
                    return False, f"OB #{zone.id} age {age} bars > fresh window {max_age}"
            return True, ""

        by_bar: dict[str, dict] = {}
        for ev in res.events:
            if ev.bar < cutoff_bar:
                continue  # only the latest bars can be new to the user
            if session_open is not None and ev.date_dt < session_open:
                continue  # previous session, even if still inside recent_bars
            d = by_bar.setdefault(ev.date,
                                  {"tap": None, "essl": None, "essl_all": [], "other": []})
            if ev.kind == K_TAP:
                d["tap"] = ev  # latest tap on the bar
            elif ev.kind == K_ESSL_TAP:
                d["essl"] = ev            # last touch = the composite's eSSL context
                d["essl_all"].append(ev)  # every eSSL level touched on this bar
            else:
                d["other"].append(ev)

        for date, d in sorted(by_bar.items()):
            composite_bar = d["tap"] is not None and d["essl"] is not None
            # Set when the composite message went out (this pass or an earlier
            # one): only that one eSSL level is skipped below, because its
            # level is already inside that message. A composite *filtered out*
            # by the TAP rules does NOT silence the eSSL touch — price did
            # reach the level, so that still alerts.
            composite_sent_pool = None
            tap_filtered = ""  # why a footprint TAP on this bar stayed silent
            # composite: ALL RULES = eSSL tap + footprint tap on the same bar
            if "essl_ob_tap" in want and composite_bar:
                tap, essl = d["tap"], d["essl"]
                ok, reason = _tap_ok(tap)
                if not ok:
                    tap_filtered = reason
                    log.info("%s [%s] %s: 🚨 composite skipped — %s",
                             sym, tf, date, reason)
                else:
                    provisional = not tap.confirmed
                    if not (provisional and not sc.provisional_alerts):
                        msg = format_composite(sym, tf, tap, essl) + self._size_tail(sym, price_last)
                        key = f"{sym}|{cfg.data.interval}|essl_ob_tap|{date}|{tap.confirmed}|{tap.zone_id}|{essl.pool_id}"
                        covered = key in self.state["alerted"]  # sent on an earlier pass
                        # the confirmed counterpart of an already-alerted LIVE
                        # composite must not be eaten by the spam guard
                        follow_up = (f"{sym}|{cfg.data.interval}|essl_ob_tap|{date}|False|"
                                     f"{tap.zone_id}|{essl.pool_id}" if tap.confirmed else None)
                        if self._try_alert(sym, "essl_ob_tap", msg, key,
                                           follow_up_of=follow_up):
                            sent += 1
                            covered = True
                        if covered:
                            # the composite message already carries this level,
                            # so the bare touch alert would only duplicate it
                            composite_sent_pool = essl.pool_id
            # individual events
            singles = []
            if d["tap"] is not None and "footprint_tap" in want:
                ok, reason = _tap_ok(d["tap"])
                if not ok:
                    log.info("%s [%s] %s: footprint TAP skipped — %s",
                             sym, tf, date, reason)
                else:
                    singles.append(("footprint_tap", d["tap"], d["tap"].zone_id))
            # eSSL LEVEL TOUCH: alert whenever price reaches an active eSSL
            # level — fresh or old, first touch or repeat, with or without a
            # footprint TAP on the bar, and independent of tap_first_only /
            # fresh_ob_only (those gate the footprint side only). Every level
            # touched on the bar gets its own alert + its own cooldown, except
            # the one a SENT composite already reported.
            if "essl_tap" in want:
                touched = []
                for ev in d["essl_all"]:
                    if ev.pool_id == composite_sent_pool:
                        continue
                    if not ev.confirmed and not sc.provisional_alerts:
                        continue  # forming-bar touches stay silent (config)
                    if tap_filtered:
                        # explain in the message why this is a bare eSSL alert
                        ev.extra["tap_filtered"] = tap_filtered
                    singles.append(("essl_tap", ev, ev.pool_id))
                    touched.append(ev)
                if touched:
                    log.info("%s [%s] %s: 💧 eSSL level touch on %d level(s)%s",
                             sym, tf, date, len(touched),
                             f" (footprint TAP filtered: {tap_filtered})" if tap_filtered else "")
            for ev in d["other"]:
                mapping = {
                    K_DEFENCE: "defence",
                    K_ZONE_INVALID: "zone_invalid",
                    K_ESSL_SWEEP: "essl_sweep",
                    K_ESSL_BREAK: "essl_break",
                    K_SSL_CREATED: "essl_created",
                    K_FOOTPRINT: "footprint_created",
                }
                kind = mapping.get(ev.kind)
                if kind and kind in want:
                    singles.append((kind, ev, ev.zone_id or ev.pool_id))
            for kind, ev, obj in singles:
                if not ev.confirmed and not sc.provisional_alerts:
                    continue
                msg = format_event(sym, tf, ev) + self._size_tail(sym, price_last)
                key = f"{sym}|{cfg.data.interval}|{kind}|{date}|{ev.confirmed}|{obj}"
                # eSSL touches: one cooldown per LEVEL, so two levels tapped on
                # the same bar cannot mask each other (all other kinds keep the
                # single per-symbol+kind spam guard).
                cd = f"{sym}|{kind}|{obj}" if kind == "essl_tap" else None
                # A confirmed alert whose LIVE twin on this very bar already went
                # out is the close-confirmed version of the same fact, not a
                # repeat: it bypasses the cooldown (dedup still allows exactly
                # one of them). That is what delivers the "RECLAIMED ✅" state
                # the indicator shows at the bar close (FMGOETZE 432.65).
                follow_up = (f"{sym}|{cfg.data.interval}|{kind}|{date}|False|{obj}"
                             if ev.confirmed else None)
                if self._try_alert(sym, kind, msg, key, cooldown_key=cd,
                                   follow_up_of=follow_up):
                    sent += 1
        return sent

    # -- loops -----------------------------------------------------------------
    def _pass_budget(self) -> float | None:
        """Seconds a single pass may take (`scanner.max_pass_minutes`, None = off)."""
        minutes = float(getattr(self.cfg.scanner, "max_pass_minutes", 0) or 0)
        return minutes * 60.0 if minutes > 0 else None

    def pass_out_of_time(self) -> bool:
        return (getattr(self, "_pass_deadline", None) is not None
                and time.monotonic() >= self._pass_deadline)

    def _timed(self, fn, timeout: float | None, what: str):
        """Run `fn()` in a DAEMON worker and abandon it (never the process) at the ceiling.

        Returns `(value, stalled)`. A daemon thread rather than a
        ThreadPoolExecutor is deliberate: an abandoned yfinance request must not
        be able to keep the process alive at shutdown — interpreter exit joins
        executor worker threads, which is exactly how "one slow pass" becomes
        "the scanner is stuck and the CI job never ends".
        """
        box: dict = {}

        def work():
            try:
                box["value"] = fn()
            except BaseException as e:  # noqa: BLE001 - reported by the caller
                box["err"] = e

        th = threading.Thread(target=work, name=f"fpfssl-{what}", daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            return None, True
        if "err" in box:
            raise box["err"]
        return box.get("value"), False

    def scan_once(self) -> int:
        t0 = time.time()
        total = 0
        budget = self._pass_budget()
        self._pass_deadline = (time.monotonic() + budget) if budget else None
        self.filtered = 0
        self.scanned = 0
        # One batched yahoo fetch for the whole universe (full-NSE daily pass
        # = thousands of tickers: per-symbol fetches would crawl). Each symbol
        # then runs the engine over the same frame the per-symbol path would
        # have fetched, so signals are identical — only the transport differs.
        frames: dict[str, pd.DataFrame] = {}
        if self.cfg.data.source == "yahoo" and len(self.symbols) > 1 and self.cfg.data.batch:
            left = budget
            run_deadline = getattr(self, "_runtime_deadline", None)
            if run_deadline is not None:
                rem = run_deadline - time.monotonic()
                left = rem if left is None else min(left, rem)
            try:
                got, stalled = self._timed(
                    lambda: load_all(self.symbols, self.cfg.data),
                    left if left and left > 0 else None, "fetch")
            except DataError as e:
                log.error("batch load failed: %s", e)
                got, stalled = {}, False
            except Exception as e:  # noqa: BLE001
                log.error("batch load failed: %s", e)
                got, stalled = {}, False
            if stalled:
                # The ceiling exists precisely so a hung feed cannot hold the
                # runner open forever: report it and end the run cleanly
                # (dedup state saved) instead of retrying per-symbol against the
                # same stalled endpoint for hours.
                log.error("no bars this pass: the yahoo fetch stalled past "
                          "scanner.max_pass_minutes (%d symbol(s) affected)",
                          len(self.symbols))
                self.request_stop("yahoo fetch stalled past the pass ceiling")
                return 0
            frames = got or {}
            log.info("fetched %d/%d symbols in %.1fs",
                     len(frames), len(self.symbols), time.time() - t0)
        # Market-cap size filter: warm the share-count cache for exactly the
        # symbols that can reach it, once per pass (in-memory afterwards, so
        # scan_symbol never touches the network). Also under the pass ceiling:
        # a slow metadata endpoint must not become a stuck scanner either, and
        # unprimed symbols simply fail open (scanned, not silently skipped).
        if self.filters.mcap_on:
            deadline = getattr(self, "_pass_deadline", None)
            rem = (deadline - time.monotonic()) if deadline is not None else None
            try:
                _n, stalled = self._timed(
                    lambda: self.prime_fundamentals(
                        self.symbols, frames,
                        refresh=getattr(self, "_refresh_fundamentals", False)),
                    rem if rem and rem > 0 else None, "fundamentals")
                if stalled:
                    log.warning("share-count fetch hit the pass ceiling — continuing "
                                "with what the cache already knows")
            except Exception as e:  # noqa: BLE001 - filters fail open, never fatal
                log.warning("share-count fetch failed (%s) — the market-cap filter "
                            "fails open for this pass", e)
        missing = 0
        done = 0
        stopped = ""
        for sym in self.symbols:
            # A stop request (or an expired budget) must not wait for the rest
            # of a full-NSE pass (thousands of symbols): bail out at the next
            # symbol boundary.
            stopped = self._stop_now()
            if stopped:
                break
            if self.pass_out_of_time():
                stopped = "pass ceiling reached (%d min)" % int((budget or 0) / 60)
                log.warning("stopping the pass early: %s — %d/%d symbols scanned",
                            stopped, done, len(self.symbols))
                break
            try:
                if frames:
                    df = frames.get(sym)
                    if df is None:
                        missing += 1
                        continue  # no bars for this symbol (dead ticker etc.)
                    total += self.scan_symbol(sym, df)
                else:
                    total += self.scan_symbol(sym)
            except Exception as e:  # noqa: BLE001
                log.exception("symbol %s failed: %s", sym, e)
            done += 1
        if missing:
            log.info("%d symbol(s) returned no bars and were skipped", missing)
        if stopped:
            log.info("pass stopped early (%s): %d/%d symbols scanned", stopped,
                     done, len(self.symbols))
        if not self.cfg.telegram.dry_run:
            self._save_state()
        tf = timeframe_label(self.cfg.data.interval)
        skipped = getattr(self, "filtered", 0)
        flt = self.filters
        tail = ""
        if flt.enabled:
            # make the filter visible in the log: how much of the universe the
            # size filters removed, so "no alerts" is never mistaken for "no run"
            tail = (" — %d symbol(s) scanned, %d filtered out [%s]"
                    % (getattr(self, "scanned", 0), skipped, flt.describe()))
        log.info("scan finished in %.1fs: %d new alert(s) across %d symbols [%s]%s",
                 time.time() - t0, total, len(self.symbols), tf, tail)
        return total

    def run_forever(self) -> str:
        """Poll the market (or run a single pass) until it is time to stop.

        Two modes, and the reason `exit_after_pass` exists is that the first
        one is easy to mistake for a hang:

        * `scanner.exit_after_pass: true` — ONE complete pass over the universe,
          save the dedup state, exit. A scheduled run therefore ends as soon as
          it has scanned the market instead of sitting in the runner until the
          close, and the next cron tick does the next pass.
        * `scanner.exit_after_pass: false` — poll until the session ends
          (`stop_after_close_minutes` past the close). This is the design for a
          single scheduled trigger that has to cover the whole NSE session
          (09:15-15:30 IST), because GitHub's cron is best-effort and drops most
          ticks of a 5-minute schedule; it costs a ~6h job that every later tick
          queues behind.

        Other behaviour (both modes):

        * started before the open    -> waits for the open (only when the open
          is within `preopen_wait_minutes`), then scans
        * started after the close    -> one final pass, then exits
        * a pass longer than `max_pass_minutes` -> cut at the next symbol
          boundary, state saved, so a slow feed cannot outrun the job timeout

        Stops (saving dedup state) on any of:
        * `exit_after_pass` (one pass per run);
        * the session ending (`stop_after_close_minutes` past the close);
        * SIGINT / SIGTERM — Ctrl-C or a cancelled CI job. Handlers are
          installed explicitly because a backgrounded process inherits SIGINT
          as SIG_IGN, which would otherwise swallow every cancel request;
        * `scanner.max_runtime_minutes` (0 = unlimited), so a long CI job
          always ends by itself instead of being killed mid-pass.

        Returns the reason it stopped.
        """
        poll = max(1.0, self.cfg.scanner.poll_minutes)
        self._init_runtime()
        self._runtime_deadline = self._compute_deadline()
        self.install_stop_handlers()
        tf = timeframe_label(self.cfg.data.interval)
        now = market_now(self.cfg)
        log.info("starting live scanner: %d symbols [%s], %s, "
                 "session %s-%s %s (ctrl-c to stop)%s",
                 len(self.symbols), tf,
                 "one pass then exit" if self.exit_after_pass
                 else f"poll every {poll:.1f} min until the close",
                 self.cfg.scanner.market_open, self.cfg.scanner.market_close,
                 self.cfg.scanner.market_timezone,
                 "" if not self.filters.enabled
                 else f" — size filters: {self.filters.describe()}")
        # pre-open: wait for the bell instead of hammering the feed — but only
        # when the open is close. A run started hours early does one pass and
        # exits so it does not hold the runner (a later tick starts the session).
        reason = "session finished"
        try:
            if not market_is_open(self.cfg, now):
                wait_min = minutes_until_open(self.cfg, now)
                if wait_min is None or wait_min > self.cfg.scanner.preopen_wait_minutes:
                    log.info("market closed (%s; next open in %s min) — single pass, then exit",
                             now.strftime("%a %H:%M"),
                             "?" if wait_min is None else f"{wait_min:.0f}")
                    self.scan_once()
                    return "finished single pass (market closed)"
                if wait_min > 0:
                    log.info("market closed now (%s) — waiting %.0f min for the open "
                             "(ctrl-c / cancel stops the wait)",
                             now.strftime("%a %H:%M"), wait_min)
                    deadline = now + timedelta(minutes=wait_min)
                    while market_now(self.cfg) < deadline:
                        if not self._sleep(min(60.0, max(1.0, wait_min * 60.0))):
                            reason = self._stop_now() or "stop requested"
                            log.info("stopped waiting for the open (%s)", reason)
                            return reason
            while True:
                try:
                    self.scan_once()
                except Exception as e:  # noqa: BLE001
                    log.exception("scan pass failed: %s", e)
                stopped = self._stop_now()
                if stopped:
                    reason = stopped
                    if reason == "runtime limit reached":
                        log.info("%s — scanner exiting (dedup state saved; the next "
                                 "run picks the session back up)", reason)
                    break
                # scanner.exit_after_pass: the point is that a completed pass is
                # a finished job. Napping here to wait for the next poll is what
                # makes a scheduled run look stuck (and blocks the concurrency
                # group for hours); the dedup state is already saved by
                # scan_once, so the next tick resumes exactly where this ended.
                if self.exit_after_pass:
                    reason = "scan complete (scanner.exit_after_pass)"
                    log.info("%s — %d symbol(s) scanned%s, scanner exiting "
                             "(dedup state saved; the next run does the next pass)",
                             reason, getattr(self, "scanned", 0),
                             f", {getattr(self, 'filtered', 0)} filtered out"
                             if self.filters.enabled else "")
                    break
                now = market_now(self.cfg)
                if _session_finished(self.cfg, now):
                    reason = "session finished"
                    log.info("session finished (%s) — scanner exiting",
                             now.strftime("%a %H:%M"))
                    break
                # fixed cadence, but never oversleep the close; inside the
                # settle window after the close keep the same cadence (the
                # sleep is chunked so a stop request wakes us immediately)
                until_close = minutes_until_close(self.cfg, now)
                if until_close <= 0:
                    nap = poll * 60.0
                else:
                    nap = min(poll * 60.0, max(30.0, until_close * 60.0))
                if not self._sleep(nap):
                    reason = self._stop_now() or "stop requested"
                    break
        finally:
            self.restore_stop_handlers()
            # never lose the dedup state on the way out: without it the next
            # pass would re-announce every alert of this run
            try:
                self._save_state()
            except Exception as e:  # noqa: BLE001
                log.warning("could not save scanner state: %s", e)
            log.info("scanner stopped (%s)", reason)
        return reason
