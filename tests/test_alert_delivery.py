"""Alert DELIVERY + run-reliability tests (offline, no network).

Everything here is about the failure mode "the scanner ran, exited green, and
the chat received nothing" — the layer the signal engine tests do not cover:

 1. Telegram pacing: sends to one chat are spaced out (`min_interval_sec`).
 2. `429 Too Many Requests` is honoured (`retry_after`) and the message is
    DEFERRED, never dropped; the same for 5xx and connection errors.
 3. A rejected HTML entity costs the formatting, not the alert (plain retry).
 4. Over-long messages are trimmed on a tag boundary; a message is never sent
    half-formatted (that is what makes Telegram answer 400).
 5. A dry run never persists dedup state — a preview that recorded "sent" keys
    used to swallow the following live session's alerts.
 6. A pass that gets no data (stalled/rate-limited feed) does NOT end the
    session worker; it retries, and only a run of failures gives up.
 7. A batch fetch that returns nothing for a big universe must not fan out into
    one sequential request per ticker (that is how a job burns its whole timeout).
 8. Universe rotation: a pass cut by `max_pass_minutes` resumes where it
    stopped, so the tail of the list can alert at all.
 9. Staleness is measured against the MARKET (an NSE holiday must not drop every
    symbol) while a delisted ticker is still skipped.
10. The CI self re-arm: decisions (never after a cancel / dry run / end of day),
    the per-day cap, and the successor request it sends.
11. The end-of-run report — the thing that makes "0 alerts" explainable — and
    the shipped config.yaml that turns all of this on.

Run:  .venv/bin/python tests/test_alert_delivery.py     (or via pytest)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from fpfssl import ci  # noqa: E402
from fpfssl import scanner as scn  # noqa: E402
from fpfssl import telegram as tg  # noqa: E402
from fpfssl.config import (AppConfig, DataConfig, ScannerConfig,  # noqa: E402
                           TelegramConfig, load_config)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ORIG_POST = tg.requests.post
_ORIG_LOAD_ALL = scn.load_all
_ORIG_LOAD_SYM = scn.load_symbol
_ORIG_NOW = scn.market_now


def _restore():
    tg.requests.post = _ORIG_POST
    scn.load_all = _ORIG_LOAD_ALL
    scn.load_symbol = _ORIG_LOAD_SYM
    scn.market_now = _ORIG_NOW


class Resp:
    """Stands in for a requests.Response."""

    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {"ok": True}
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakePost:
    """Records calls, replays a scripted list of replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "payload": json or {}, "headers": headers or {}})
        r = self.replies.pop(0) if self.replies else Resp()
        if isinstance(r, Exception):
            raise r
        return r


def sleep_log():
    """Patch telegram.time.sleep: record the naps, never actually wait."""
    naps: list[float] = []
    real = tg.time.sleep

    def fake(s):
        naps.append(float(s))

    tg.time.sleep = fake
    return naps, real


def cfg_telegram(**kw) -> TelegramConfig:
    base = dict(enabled=True, dry_run=False, token="123:abc", chat_id="42",
                api_base="https://tl.test", timeout=5.0, min_interval_sec=1.0,
                max_retries=4, retry_backoff_sec=0.01, max_wait_sec=60.0)
    base.update(kw)
    return TelegramConfig(**base)


# ---------------------------------------------------------------------------
# 1-4. Telegram delivery
# ---------------------------------------------------------------------------
def test_sends_are_paced():
    """Telegram allows ~1 msg/s per chat: back-to-back sends must space out."""
    post = FakePost([Resp(), Resp()])
    tg.requests.post = post
    naps, real = sleep_log()
    n = tg.TelegramNotifier(cfg_telegram(min_interval_sec=1.0))
    try:
        assert n.send("first") and n.send("second")
    finally:
        _restore()
        tg.time.sleep = real
    assert len(post.calls) == 2, post.calls
    assert naps and naps[0] >= 0.9, f"second send was not paced: {naps}"
    assert n.stats["sent"] == 2, n.stats
    print(f"ok test_sends_are_paced (napped {naps[0]:.2f}s between messages)")


def test_429_is_deferred_not_dropped():
    """A flood-limited alert must come out eventually (that's the whole point)."""
    post = FakePost([
        Resp(429, {"ok": False, "description": "Too Many Requests: retry after 3",
                   "parameters": {"retry_after": 3}}),
        Resp(200, {"ok": True}),
    ])
    tg.requests.post = post
    naps, real = sleep_log()
    n = tg.TelegramNotifier(cfg_telegram(min_interval_sec=0.0))
    try:
        ok = n.send("💧 RELIANCE.NS eSSL tap")
    finally:
        _restore()
        tg.time.sleep = real
    assert ok is True, "the alert was dropped on a 429 instead of retried"
    assert n.stats["throttled"] == 1 and n.stats["sent"] == 1, n.stats
    assert 3.0 in naps, f"did not honour retry_after: {naps}"
    print("ok test_429_is_deferred_not_dropped (waited the retry_after, then sent)")


def test_rate_limit_exhaustion_is_reported():
    """When Telegram will not take it, the failure must be loud and counted."""
    post = FakePost([Resp(429, {"ok": False, "description": "Too Many Requests",
                                "parameters": {"retry_after": 5}})] * 8)
    tg.requests.post = post
    naps, real = sleep_log()
    n = tg.TelegramNotifier(cfg_telegram(min_interval_sec=0.0, max_retries=3,
                                         max_wait_sec=5.0))
    try:
        ok = n.send("alert")
    finally:
        _restore()
        tg.time.sleep = real
    assert ok is False
    assert n.stats["failed"] == 1 and n.stats["dropped"] == 1, n.stats
    assert "429" in n.last_error, n.last_error
    print(f"ok test_rate_limit_exhaustion_is_reported ({n.last_error})")


def test_html_rejection_falls_back_to_plain_text():
    """One bad entity must cost the formatting, never the signal."""
    post = FakePost([
        Resp(400, {"ok": False, "description": "Bad Request: can't parse entities: "
                                               "unsupported start tag at byte offset 12"}),
        Resp(200, {"ok": True}),
    ])
    tg.requests.post = post
    naps, real = sleep_log()
    n = tg.TelegramNotifier(cfg_telegram(min_interval_sec=0.0))
    try:
        ok = n.send("💧 <b>RAW & UNCLOSED")
    finally:
        _restore()
        tg.time.sleep = real
    assert ok is True
    second = post.calls[1]["payload"]
    assert "parse_mode" not in second, second
    assert "&amp;" in second["text"] and "<" not in second["text"], second["text"]
    assert n.stats["deformatted"] == 1, n.stats
    print("ok test_html_rejection_falls_back_to_plain_text")


def test_auth_error_is_not_retried():
    post = FakePost([Resp(401, {"ok": False, "description": "Unauthorized"})])
    tg.requests.post = post
    n = tg.TelegramNotifier(cfg_telegram(min_interval_sec=0.0))
    try:
        ok = n.send("alert")
    finally:
        _restore()
    assert ok is False and len(post.calls) == 1, post.calls
    assert "401" in n.last_error, n.last_error
    print("ok test_auth_error_is_not_retried (misconfiguration fails fast)")


def test_connection_error_retries():
    post = FakePost([ConnectionError("connection reset"), Resp(200, {"ok": True})])
    tg.requests.post = post
    naps, real = sleep_log()
    n = tg.TelegramNotifier(cfg_telegram(min_interval_sec=0.0))
    try:
        ok = n.send("alert")
    finally:
        _restore()
        tg.time.sleep = real
    assert ok is True and len(post.calls) == 2, post.calls
    assert n.stats["retried"] == 1, n.stats
    print("ok test_connection_error_retries")


def test_long_message_is_trimmed_safely():
    """Never send a half-open tag (Telegram 400s it) and stay under the limit."""
    body = "🚨 <b>RELIANCE</b>\n" + ("detail line <b>bold</b>\n" * 500)
    assert len(body) > tg.SAFE_LIMIT
    # the size footer is what makes messages long; it must go first
    msg = body + "\n\n📏 <b>size</b> price ₹1,432.00 · mcap ₹950,000 Cr · " \
                  "filter: mcap > ₹1,000 Cr + price > ₹100.00"
    fitted = tg._fit(msg)
    assert len(fitted) <= tg.SAFE_LIMIT, len(fitted)
    assert "📏" not in fitted, "the footer survived the trim"
    opens = fitted.count("<b>")
    closes = fitted.count("</b>")
    assert opens == closes, f"unbalanced tags after trimming: {opens}/{closes}"

    post = FakePost([Resp(200, {"ok": True})])
    tg.requests.post = post
    n = tg.TelegramNotifier(cfg_telegram(min_interval_sec=0.0))
    try:
        assert n.send(msg)
    finally:
        _restore()
    sent = post.calls[0]["payload"]["text"]
    assert len(sent) <= tg.SAFE_LIMIT + 2 and sent.endswith("…") is (len(msg) > tg.SAFE_LIMIT)
    print(f"ok test_long_message_is_trimmed_safely ({len(msg)} -> {len(sent)} chars)")


def test_persistent_rate_limit_mutes_sends_not_the_pass():
    """A chat that refuses to be talked to must not turn a pass into hours of waiting."""
    replies = [Resp(429, {"ok": False, "description": "Too Many Requests: retry after 50",
                          "parameters": {"retry_after": 50}})]
    post = FakePost(replies)
    tg.requests.post = post
    naps, real = sleep_log()
    n = tg.TelegramNotifier(cfg_telegram(min_interval_sec=0.0, max_retries=2,
                                         max_wait_sec=5.0, mute_after_failure_sec=30.0))
    t0 = time.monotonic()
    try:
        first = n.send("alert 1")
        second = n.send("alert 2")
        third = n.send("alert 3")
    finally:
        _restore()
        tg.time.sleep = real
    dur = time.monotonic() - t0
    assert (first, second, third) == (False, False, False)
    assert len(post.calls) == 1, f"kept hammering a refusing chat: {len(post.calls)} calls"
    assert dur < 1.0, f"the send loop cost {dur:.1f}s of pass time"
    assert n.stats["dropped"] == 3 and n.stats["failed"] == 1, n.stats
    print(f"ok test_persistent_rate_limit_mutes_sends_not_the_pass "
          f"(1 attempt then muted, {dur*1000:.0f}ms)")


# ---------------------------------------------------------------------------
# scanner fixtures
# ---------------------------------------------------------------------------
def daily_frame(bars: int = 700, end: datetime | None = None, seed: int = 7,
                dip: bool = True) -> pd.DataFrame:
    """A daily frame whose last bar touches an active eSSL level (a tap)."""
    from fpfssl.engine import Engine
    from fpfssl.events import K_ESSL_TAP

    end = pd.Timestamp((end or datetime.now()).date())
    idx = pd.bdate_range(end=end, periods=bars)
    rng = __import__("numpy").random.default_rng(seed)
    close = 400.0 * __import__("numpy").exp(__import__("numpy").cumsum(
        rng.normal(0.0002, 0.013, bars)))
    op = close * (1 + rng.normal(0, 0.003, bars))
    hi = __import__("numpy").maximum(op, close) + abs(rng.normal(0, 1, bars)) * 1.2
    lo = __import__("numpy").minimum(op, close) - abs(rng.normal(0, 1, bars)) * 1.2
    df = pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close,
                       "volume": (1e6 * __import__("numpy").exp(rng.normal(0, 0.3, bars)))},
                      index=idx).round(2)
    if dip:
        # drag the last bar down onto a level the engine has already published
        res = Engine("SYMB.NS", _eng_cfg(), 0.05, tf="1d").run(df.iloc[:-1], live_last_bar=False)
        levels = [p.lower for p in res.pools if p.active and p.scope == 1]
        assert levels, "fixture: no active eSSL level to tap"
        px = float(df["close"].iloc[-1])
        cand = min(levels, key=lambda lv: abs(lv - px))
        i = len(df) - 1
        df.iloc[i, df.columns.get_loc("low")] = round(cand * 0.997, 2)
        df.iloc[i, df.columns.get_loc("close")] = round(max(px, cand * 1.02), 2)
        df.iloc[i, df.columns.get_loc("high")] = round(max(df["high"].iloc[i], px * 1.01), 2)
        out = Engine("SYMB.NS", _eng_cfg(), 0.05, tf="1d").run(df, live_last_bar=True)
        assert any(e.kind == K_ESSL_TAP and e.bar == i for e in out.events), \
            "fixture: the last bar does not tap an eSSL level"
    return df


def _eng_cfg():
    from fpfssl.config import EngineConfig
    return EngineConfig()


class Recorder:
    def __init__(self):
        self.messages: list[str] = []
        self.stats = {"sent": 0, "failed": 0, "dropped": 0, "throttled": 0,
                      "dry_run": 0, "retried": 0, "deformatted": 0}
        self.last_error = ""

    @property
    def ready(self):
        return True

    def describe(self) -> str:
        return f"{self.stats['sent']} sent, {self.stats['throttled']} throttled, 0 failed"

    def send(self, text: str) -> bool:
        self.messages.append(text)
        self.stats["sent"] += 1
        return True


def mk_cfg(tmp: str, **sc_kw) -> AppConfig:
    cfg = AppConfig()
    cfg.symbols = ["SYMB.NS"]
    cfg.data = DataConfig(source="yahoo", interval="1d", history_bars=700)
    cfg.scanner = ScannerConfig()
    cfg.scanner.min_bars = 10
    cfg.scanner.alert_events = ["essl_tap"]
    cfg.scanner.alert_cooldown_minutes = 60
    cfg.scanner.state_file = os.path.join(tmp, "scanner_state.json")
    cfg.scanner.report_file = os.path.join(tmp, "scan_report.json")
    cfg.scanner.reschedule_in_ci = False
    for k, v in sc_kw.items():
        setattr(cfg.scanner, k, v)
    cfg.telegram.dry_run = False
    cfg.telegram.token = "1:2"
    cfg.telegram.chat_id = "9"
    cfg.telegram.min_interval_sec = 0.0
    return cfg


# ---------------------------------------------------------------------------
# 5. a dry run must not consume live alerts
# ---------------------------------------------------------------------------
def test_dry_run_never_persists_dedup_state():
    tmp = tempfile.mkdtemp(prefix="fpfssl-dry-")
    cfg = mk_cfg(tmp)
    cfg.telegram.dry_run = True
    cfg.scanner.exit_after_pass = True
    df = daily_frame(end=datetime(2026, 9, 11))
    rec = Recorder()
    now = datetime(2026, 9, 11, 10, 0)
    scn.load_symbol = lambda s, d: df
    scn.market_now = lambda c: now
    try:
        dry = scn.LiveScanner(cfg, rec, symbols=["SYMB.NS"])
        dry.run_forever()
        path = cfg.scanner.state_file
        assert not os.path.exists(path), (
            "a dry run wrote the dedup state: the next live pass would believe "
            "those alerts were delivered and swallow them")
        assert dry.stats["alerts"] > 0, "dry run produced no preview to guard"

        # the same session, live: the alert must still be deliverable
        cfg.telegram.dry_run = False
        live = scn.LiveScanner(cfg, rec, symbols=["SYMB.NS"])
        assert live.scan_once() > 0, "the live pass was silenced by the dry-run state"
    finally:
        _restore()
    print("ok test_dry_run_never_persists_dedup_state (preview did not consume the live alert)")


# ---------------------------------------------------------------------------
# 6. a failed pass must not end the session worker
# ---------------------------------------------------------------------------
def test_stalled_pass_keeps_the_session_alive():
    tmp = tempfile.mkdtemp(prefix="fpfssl-stall-")
    cfg = mk_cfg(tmp)
    cfg.data.batch = True
    cfg.symbols = ["A.NS", "B.NS"]
    cfg.scanner.max_pass_minutes = 0.0
    cfg.scanner.exit_after_pass = False
    cfg.scanner.poll_minutes = 1.0
    cfg.scanner.max_pass_failures = 3
    calls = {"n": 0}

    def boom(symbols, d):
        calls["n"] += 1
        raise scn.DataError("yahoo is rate limiting us")

    def dead(sym, d):                      # the small-universe per-symbol retry
        raise scn.DataError("yahoo is rate limiting us")

    clock = {"t": datetime(2026, 9, 11, 10, 0)}
    real_sleep = scn.time.sleep
    naps: list[float] = []

    def fake_sleep(sec):
        naps.append(float(sec))
        clock["t"] += timedelta(seconds=float(sec))

    scn.load_all = boom
    scn.load_symbol = dead
    scn.market_now = lambda c: clock["t"]
    try:
        scn.time.sleep = fake_sleep
        sc = scn.LiveScanner(cfg, Recorder(), symbols=cfg.symbols)
        reason = sc.run_forever()
    finally:
        scn.time.sleep = real_sleep
        _restore()
    assert calls["n"] >= 3, f"the poller gave up after one failed pass: {calls}"
    assert "no data" in reason, reason
    assert sc.stats["failed_passes"] >= 3, sc.stats
    assert not sc.stop_requested or "no data" in sc.stop_reason, sc.stop_reason
    assert sc._pass_failed, "the last pass must still be marked failed"
    print(f"ok test_stalled_pass_keeps_the_session_alive (retried {calls['n']}x, then: {reason})")


def test_stalled_fetch_does_not_stop_the_run():
    """Regression: the fetch ceiling called request_stop(), ending the session."""
    tmp = tempfile.mkdtemp(prefix="fpfssl-stall2-")
    cfg = mk_cfg(tmp)
    cfg.data.batch = True
    cfg.symbols = ["A.NS", "B.NS"]
    cfg.scanner.exit_after_pass = False
    cfg.scanner.max_pass_minutes = 0.0
    scn.load_all = lambda symbols, d: (_ for _ in ()).throw(scn.DataError("stalled"))
    scn.load_symbol = lambda sym, d: (_ for _ in ()).throw(scn.DataError("stalled"))
    scn.market_now = lambda c: datetime(2026, 9, 11, 10, 0)
    sc = scn.LiveScanner(cfg, Recorder(), symbols=cfg.symbols)
    sc._runtime_deadline = None
    try:
        assert sc.scan_once() == 0
    finally:
        _restore()
    assert not sc.stop_requested, "a failed pass must not request a stop"
    assert sc.consecutive_failures() == 1, sc.stats
    assert sc.stats["failed_passes"] == 1, sc.stats
    print("ok test_stalled_fetch_does_not_stop_the_run")


# ---------------------------------------------------------------------------
# 7. no per-symbol fan-out over a big universe
# ---------------------------------------------------------------------------
def test_empty_batch_does_not_refetch_thousands_one_by_one():
    tmp = tempfile.mkdtemp(prefix="fpfssl-fanout-")
    cfg = mk_cfg(tmp)
    cfg.data.batch = True
    cfg.data.batch_size = 10
    cfg.symbols = [f"S{i}.NS" for i in range(120)]
    calls: list[str] = []
    scn.load_all = lambda symbols, d: {}
    scn.load_symbol = lambda s, d: calls.append(s) or (_ for _ in ()).throw(
        scn.DataError("nope"))
    sc = scn.LiveScanner(cfg, Recorder(), symbols=cfg.symbols)
    try:
        sc.scan_once()
    finally:
        _restore()
    assert calls == [], f"fell back to {len(calls)} sequential fetches for a dead feed"
    print("ok test_empty_batch_does_not_refetch_thousands_one_by_one")


def test_small_universe_still_gets_the_per_symbol_retry():
    tmp = tempfile.mkdtemp(prefix="fpfssl-fanout2-")
    cfg = mk_cfg(tmp)
    cfg.data.batch = True
    cfg.data.batch_size = 10
    df = daily_frame(end=datetime(2026, 9, 11))
    seen: list[str] = []

    def load(sym, d):
        seen.append(sym)
        return df

    scn.load_all = lambda symbols, d: {}
    scn.load_symbol = load
    scn.market_now = lambda c: datetime(2026, 9, 11, 10, 0)
    sc = scn.LiveScanner(cfg, Recorder(), symbols=["SYMB.NS"])
    try:
        sent = sc.scan_once()
    finally:
        _restore()
    assert seen == ["SYMB.NS"] and sent > 0, (seen, sent)
    print("ok test_small_universe_still_gets_the_per_symbol_retry")


# ---------------------------------------------------------------------------
# 8. rotation
# ---------------------------------------------------------------------------
def test_cut_pass_resumes_where_it_stopped():
    tmp = tempfile.mkdtemp(prefix="fpfssl-rotate-")
    syms = [f"{chr(65 + i)}X.NS" for i in range(6)]
    cfg = mk_cfg(tmp)
    cfg.symbols = syms
    cfg.data.batch = False          # per-symbol path: cheap, and stubbed below
    cfg.scanner.rotate_universe = True
    cfg.scanner.max_pass_minutes = 0.0
    order: list[list[str]] = []
    df = daily_frame(end=datetime(2026, 9, 11))

    def run_pass(sc, limit):
        seen: list[str] = []

        def fake_scan(sym, frame=None, market_last=None):
            seen.append(sym)
            if len(seen) >= limit:
                sc.request_stop("pass ceiling reached (0 min)")   # like the ceiling
            return 0

        sc.scan_symbol = fake_scan
        sc.scan_once()
        order.append(list(seen))
        return sc.state.get("cursor", 0)

    scn.load_symbol = lambda s, d: df
    try:
        sc = scn.LiveScanner(cfg, Recorder(), symbols=syms)
        cur1 = run_pass(sc, 2)
        assert order[-1] == syms[:2], order[-1]
        assert cur1 == 2, cur1
        sc2 = scn.LiveScanner(cfg, Recorder(), symbols=syms)   # a fresh CI runner
        run_pass(sc2, 2)
        assert order[-1] == syms[2:4], f"did not resume after the cut: {order[-1]}"
        # a pass that finishes the list resumes at the cursor and resets it
        sc3 = scn.LiveScanner(cfg, Recorder(), symbols=syms)
        sc3.scan_symbol = lambda sym, frame=None, market_last=None: order[-1].append(sym) or 0
        order[-1] = []
        sc3._pass_bars = 1
        sc3.scan_once()
        assert order[-1] == syms[4:] + syms[:4], order[-1]
        assert sc3.state.get("cursor", -1) == 0, sc3.state
    finally:
        _restore()
    print("ok test_cut_pass_resumes_where_it_stopped (no more permanently unscanned tail)")


# ---------------------------------------------------------------------------
# 9. staleness is relative to the market
# ---------------------------------------------------------------------------
def test_holiday_does_not_filter_the_whole_universe():
    """5 trading days without bars for EVERY symbol = a holiday: still scanned."""
    tmp = tempfile.mkdtemp(prefix="fpfssl-stale-")
    cfg = mk_cfg(tmp)
    cfg.scanner.max_stale_days = 4
    end = datetime(2026, 9, 4)                     # Friday, one week before `now`
    df = daily_frame(bars=700, end=end, dip=False)
    now = datetime(2026, 9, 11, 10, 0)            # Friday week+1 (a 5-session break)
    scn.load_symbol = lambda s, d: df
    scn.market_now = lambda c: now
    try:
        sc = scn.LiveScanner(cfg, Recorder(), symbols=["SYMB.NS"])
        sc.scan_symbol("SYMB.NS", df, market_last=df.index[-1])
        held_out = sc.stats["skipped_stale"]
        sc2 = scn.LiveScanner(cfg, Recorder(), symbols=["SYMB.NS"])
        sc2.scan_symbol("SYMB.NS", df)            # no market reference -> clock rule
        dropped = sc2.stats["skipped_stale"]
    finally:
        _restore()
    assert held_out == 0, f"a market-wide break dropped the symbol anyway: {sc.stats}"
    assert dropped == 1, "without the market reference the calendar guard must apply"
    print("ok test_holiday_does_not_filter_the_whole_universe")


# ---------------------------------------------------------------------------
# 10. self re-arm
# ---------------------------------------------------------------------------
def test_reatm_rules():
    tmp = tempfile.mkdtemp(prefix="fpfssl-reatm-")
    cfg = mk_cfg(tmp)
    cfg.scanner.reschedule_in_ci = True
    env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    live = datetime(2026, 9, 11, 11, 0)            # Friday 11:00 IST: mid-session
    after = datetime(2026, 9, 11, 17, 30)           # after the settle window
    weekend = datetime(2026, 9, 13, 11, 0)
    try:
        ok, why = ci.should_reschedule("runtime limit reached", cfg, {}, live)
        assert ok, why
        ok, why = ci.should_reschedule("scan complete (scanner.exit_after_pass)",
                                       cfg, {}, live)
        assert ok, why
        ok, why = ci.should_reschedule("session finished", cfg, {}, live)
        assert not ok and "on schedule" in why, why
        ok, why = ci.should_reschedule("received SIGTERM", cfg, {}, live)
        assert not ok and "cancel" in why, why
        ok, why = ci.should_reschedule("runtime limit reached", cfg, {}, after)
        assert not ok, why
        ok, why = ci.should_reschedule("runtime limit reached", cfg, {}, weekend)
        assert not ok, why
        cfg.telegram.dry_run = True
        ok, why = ci.should_reschedule("runtime limit reached", cfg, {}, live)
        assert not ok and "dry run" in why, why
        cfg.telegram.dry_run = False
        state = {"chain": {"date": live.strftime("%Y-%m-%d"),
                           "count": cfg.scanner.reschedule_max_runs_per_day}}
        ok, why = ci.should_reschedule("runtime limit reached", cfg, state, live)
        assert not ok and "cap" in why, why
        # a dead feed may re-arm, but only a bounded number of times
        ok, why = ci.should_reschedule("3 consecutive passes produced no data",
                                        cfg, {}, live, feed_failure=True)
        assert ok and "retrying once" in why, why
        dead_state = {"chain": {"date": live.strftime("%Y-%m-%d"), "count": 2,
                                "fails": ci.FAILURE_REARMS_PER_DAY}}
        ok, why = ci.should_reschedule("3 consecutive passes produced no data",
                                       cfg, dead_state, live, feed_failure=True)
        assert not ok and "feed is down" in why, why
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        ok, why = ci.should_reschedule("runtime limit reached", cfg, {}, live)
        assert not ok and "not running in GitHub Actions" in why, why
    finally:
        _restore()
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    print("ok test_reatm_rules (never after a cancel, a dry run, or the close)")


def test_reatm_dispatch_shape():
    os.environ["GITHUB_TOKEN"] = "tok"
    os.environ["GITHUB_REPOSITORY"] = "atikhalde/FOOTPRINT"
    os.environ["GITHUB_REF_NAME"] = "main"
    os.environ["FPFSSL_REARM_INPUTS"] = '{"poll_session": true, "symbols": "", "interval": ""}'
    posts: list[dict] = []

    def post(url, json=None, headers=None, timeout=None):
        posts.append({"url": url, "json": json, "headers": headers})
        return Resp(204, {"ok": True})

    try:
        ctx = ci.ci_context()
        assert ctx and ctx["repository"] == "atikhalde/FOOTPRINT"
        assert ctx["inputs"]["once"] is False and ctx["inputs"]["dry_run"] is False
        ok, detail = ci.dispatch_next(post=post)
        assert ok, detail
        assert posts[0]["url"].endswith("/repos/atikhalde/FOOTPRINT/actions/workflows/"
                                       "scanner.yml/dispatches"), posts[0]["url"]
        assert posts[0]["json"]["ref"] == "main"
        assert posts[0]["headers"]["Authorization"].startswith("Bearer ")
        # a malformed inputs blob degrades to the defaults, never an exception
        os.environ["FPFSSL_REARM_INPUTS"] = "{not json"
        assert ci._env_inputs() == {"once": False, "dry_run": False}
        # a denied token must be reported, not raised
        ok, detail = ci.dispatch_next(post=lambda *a, **k: Resp(403, {"ok": False},
                                                                "Resource not accessible"))
        assert not ok and "403" in detail, detail
    finally:
        for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_REF_NAME",
                  "FPFSSL_REARM_INPUTS"):
            os.environ.pop(k, None)
    print("ok test_reatm_dispatch_shape")


def test_reatm_bumps_the_persisted_counter():
    tmp = tempfile.mkdtemp(prefix="fpfssl-reatm2-")
    cfg = mk_cfg(tmp)
    cfg.scanner.reschedule_in_ci = True
    sc = scn.LiveScanner.__new__(scn.LiveScanner)
    sc.cfg, sc.state, sc.symbols = cfg, {"alerted": {}, "cooldown": {}}, []
    sc._save_state = lambda: None
    now = datetime(2026, 9, 11, 11, 0)
    day = now.strftime("%Y-%m-%d")
    ok, detail = ci.maybe_reschedule(sc, "runtime limit reached", now=now)
    assert not ok and "not running in GitHub Actions" in detail, detail
    os.environ["GITHUB_TOKEN"] = "tok"
    os.environ["GITHUB_REPOSITORY"] = "o/r"
    try:
        ci.dispatch_next(post=lambda *a, **k: Resp(204, {"ok": True}))   # sanity
        n = ci.bump_chain(sc.state, day)
        assert n == 1 and sc.state["chain"]["date"] == day, sc.state
        ok, why = ci.should_reschedule("runtime limit reached", cfg, sc.state, now)
        assert ok, why
        assert ci.bump_chain(sc.state, day) == 2
        assert ci.bump_chain(sc.state, "2026-09-12") == 1, "a new day must reset"
    finally:
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GITHUB_REPOSITORY", None)
    print("ok test_reatm_bumps_the_persisted_counter (one successor per pass, capped)")


# ---------------------------------------------------------------------------
# 11. the run report + the shipped configuration
# ---------------------------------------------------------------------------
def test_report_explains_a_quiet_run():
    tmp = tempfile.mkdtemp(prefix="fpfssl-report-")
    cfg = mk_cfg(tmp)
    df = daily_frame(end=datetime(2026, 9, 11))
    now = datetime(2026, 9, 11, 10, 0)
    scn.load_symbol = lambda s, d: df
    scn.market_now = lambda c: now
    try:
        rec = Recorder()
        sc = scn.LiveScanner(cfg, rec, symbols=["SYMB.NS"])
        sent = sc.scan_once()
        assert sent > 0, "fixture must alert once"
        path = sc.write_report()
        rep = json.load(open(path, encoding="utf-8"))
        # a second pass over the same bar: quiet, and provably *why*
        sc2 = scn.LiveScanner(cfg, rec, symbols=["SYMB.NS"])
        assert sc2.scan_once() == 0
        rep2 = sc2.report()
    finally:
        _restore()
    assert rep["stats"]["alerts"] == sent, rep
    assert rep["stats"]["scanned"] >= 1, rep
    assert rep["alert_events"] == ["essl_tap"], rep
    assert rep["delivery"]["sent"] == sent, rep
    assert rep["mode"], rep
    assert rep2["stats"]["suppressed_dedup"] >= 1, rep2
    assert rep2["suppressed_examples"], rep2
    assert "already alerted" in " ".join(rep2["suppressed_examples"]), rep2
    print("ok test_report_explains_a_quiet_run (sent vs suppressed, in one object)")


def test_shipped_config_covers_the_session():
    """config.yaml must ship the reliability defaults this suite guards."""
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    assert cfg.scanner.exit_after_pass is False, \
        "one pass per (unreliable) cron tick leaves the session unscanned"
    assert cfg.scanner.max_pass_failures >= 2, cfg.scanner.max_pass_failures
    assert cfg.scanner.rotate_universe is True
    assert cfg.scanner.report_file, "no report -> 'why no alert?' is unanswerable"
    assert cfg.scanner.reschedule_in_ci is True
    assert cfg.scanner.reschedule_max_runs_per_day > 0
    assert 0.2 <= cfg.telegram.min_interval_sec <= 1.5, cfg.telegram.min_interval_sec
    assert cfg.telegram.max_retries >= 2 and cfg.telegram.max_wait_sec >= 10
    assert cfg.scanner.alert_events == ["essl_ob_tap", "essl_tap", "footprint_tap"]
    assert cfg.scanner.provisional_alerts is True
    # The workflow must actually grant what the re-arm needs, run this suite, and
    # publish the report. Suite names are matched without the `.py` because the
    # step runs them through a loop (`for t in test_engine test_size_filters …`).
    wf = open(os.path.join(ROOT, ".github", "workflows", "scanner.yml"), encoding="utf-8").read()
    assert "actions: write" in wf and "GITHUB_TOKEN" in wf, "re-arm cannot dispatch"
    assert "test_alert_delivery" in wf, "this suite must gate the Scan step"
    assert "state/scan_report.json" in wf, "the report belongs in the job summary"
    assert "if: always() && env.DRY_RUN != 'true' && hashFiles(" in wf, \
        "an empty state cache must not be publishable"
    print("ok test_shipped_config_covers_the_session")


def test_unknown_config_key_is_not_silent():
    """A typo'd knob must be named, not dropped — `min_market_cap` instead of
    `min_market_cap_cr` silently disables a filter and looks like a working run."""
    import logging

    tmp = tempfile.mkdtemp(prefix="fpfssl-cfgtypo-")
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("scanner:\n  min_market_cap: 500\n  poll_minutes: 7\n")
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())

    logger = logging.getLogger("fpfssl.config")
    h = Grab(level=logging.WARNING)
    logger.addHandler(h)
    old = logger.level
    logger.setLevel(logging.WARNING)
    try:
        from fpfssl.config import load_config as lc
        cfg = lc(path)
    finally:
        logger.removeHandler(h)
        logger.setLevel(old)
    assert cfg.scanner.poll_minutes == 7, cfg.scanner.poll_minutes
    assert any("min_market_cap" in m for m in records), records
    print("ok test_unknown_config_key_is_not_silent (typo named in a warning)")


ALL = [
    test_sends_are_paced,
    test_429_is_deferred_not_dropped,
    test_rate_limit_exhaustion_is_reported,
    test_html_rejection_falls_back_to_plain_text,
    test_auth_error_is_not_retried,
    test_persistent_rate_limit_mutes_sends_not_the_pass,
    test_connection_error_retries,
    test_long_message_is_trimmed_safely,
    test_dry_run_never_persists_dedup_state,
    test_stalled_pass_keeps_the_session_alive,
    test_stalled_fetch_does_not_stop_the_run,
    test_empty_batch_does_not_refetch_thousands_one_by_one,
    test_small_universe_still_gets_the_per_symbol_retry,
    test_cut_pass_resumes_where_it_stopped,
    test_holiday_does_not_filter_the_whole_universe,
    test_reatm_rules,
    test_reatm_dispatch_shape,
    test_reatm_bumps_the_persisted_counter,
    test_report_explains_a_quiet_run,
    test_shipped_config_covers_the_session,
    test_unknown_config_key_is_not_silent,
]


def main():
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    print()
    if failed:
        print(f"{failed} of {len(ALL)} delivery/reliability tests FAILED")
        sys.exit(1)
    print(f"All {len(ALL)} alert-delivery / reliability tests passed.")


if __name__ == "__main__":
    main()
