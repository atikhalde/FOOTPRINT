"""Telegram notifier (plain HTTPS, no bot framework) with delivery guarantees.

Credentials come from config.yaml or the environment:
    TELEGRAM_BOT_TOKEN / telegram.token
    TELEGRAM_CHAT_ID   / telegram.chat_id

Create a bot with @BotFather, get your chat id from @userinfobot, then run:
    python -m fpfssl test-telegram

Why this module is more than a `requests.post`
----------------------------------------------
A full-universe pass can produce hundreds of taps at once, and Telegram allows
roughly ONE message per second per chat (bursting to ~30/s globally). The old
implementation fired every alert back-to-back and treated any non-`ok` reply as
"not my problem": Telegram answered `429 Too Many Requests` (or `400` for one
malformed entity) and the message was gone forever — the scanner logged an
error, reported a green run and the user received nothing. That is exactly the
"the scanner ran but no alert arrived" failure mode.

So delivery is now explicit:

* `min_interval_sec` paces sends to what one chat can actually take;
* `429` honours `parameters.retry_after` (with a `max_wait_sec` budget) and
  retries — the alert is *deferred*, never dropped;
* `5xx` / connection errors retry with exponential backoff;
* `400` on an unparseable entity retries ONCE as escaped plain text, so a
  formatting problem can never swallow a signal;
* a message longer than Telegram's limit is trimmed on a tag boundary (and
  de-formatted if that is impossible) instead of being rejected;
* every outcome is counted in `TelegramNotifier.stats`, which the scanner folds
  into its run report — `throttled` / `failed` are what make "0 delivered"
  distinguishable from "0 to deliver".
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time

import requests

from .config import TelegramConfig

log = logging.getLogger("fpfssl.telegram")

_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

#: Telegram hard limit for a message (sendMessage `text`).
TG_LIMIT = 4096
#: Keep a little headroom so a late-appended footer cannot tip it over.
SAFE_LIMIT = 3800


def _plain(text: str) -> str:
    """Strip formatting tags and escape what is left (plain-text fallback)."""
    return html.escape(_TAG_RE.sub("", text), quote=False)


def _fit(text: str, limit: int = SAFE_LIMIT) -> str:
    """Trim to `limit` chars without producing malformed HTML.

    Prefers dropping the trailing footer (the `📏 size` block the scanner
    appends), then cuts at a newline. A cut that would leave a tag half-open is
    closed by falling back to plain text — an unparseable message is a
    *dropped* alert, and losing formatting beats losing the signal.
    """
    if len(text) <= limit:
        return text
    for marker in ("\n\n📏", "\n\nℹ️"):
        i = text.rfind(marker)
        if i > 0:
            text = text[:i]
            if len(text) <= limit:
                return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit * 0.5:
        cut = cut[:nl]
    # unbalanced tags after the cut -> plain text
    opens = len(re.findall(r"<(?!/)([a-zA-Z][^>]*)>", cut))
    closes = len(re.findall(r"</([a-zA-Z]+)>", cut))
    if opens != closes:
        cut = _plain(cut)
    return cut + "\n…"


class TelegramNotifier:
    """Send alerts to one chat, paced and retried (thread-safe)."""

    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._last_send = 0.0
        # circuit breaker: while Telegram is refusing this chat, stop hammering it
        self._muted_until = 0.0
        #: delivery counters (folded into the scanner's run report)
        self.stats: dict[str, int] = {
            "sent": 0, "dry_run": 0, "throttled": 0, "retried": 0,
            "failed": 0, "deformatted": 0, "dropped": 0,
        }
        self.last_error: str = ""

    # -- helpers --------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return bool(self.cfg.token and self.cfg.chat_id)

    @property
    def live(self) -> bool:
        """True when a send actually hits the Telegram API."""
        return bool(self.cfg.enabled and not self.cfg.dry_run and self.ready)

    def _count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def _pace(self) -> None:
        """Space sends out (`telegram.min_interval_sec`, ~1/s per chat)."""
        gap = float(getattr(self.cfg, "min_interval_sec", 0.0) or 0.0)
        if gap <= 0:
            return
        with self._lock:
            wait = self._last_send + gap - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, 10.0))
            self._last_send = time.monotonic()

    def describe(self) -> str:
        s = self.stats
        return (f"{s.get('sent', 0)} sent, {s.get('throttled', 0)} throttled, "
                f"{s.get('failed', 0)} failed")

    def _muted(self) -> bool:
        return time.monotonic() < self._muted_until

    def _mute(self) -> None:
        sec = float(getattr(self.cfg, "mute_after_failure_sec", 60.0) or 0.0)
        if sec > 0:
            self._muted_until = time.monotonic() + sec
            log.warning("Telegram delivery is failing: pausing sends for %.0fs "
                        "(alerts not recorded as sent, so the next pass retries them)", sec)

    # -- one API call ---------------------------------------------------------
    def _post(self, text: str, parse_mode: str | None) -> tuple[int, dict]:
        """`(status_code, decoded_body)` — raises requests.RequestException."""
        url = f"{self.cfg.api_base}/bot{self.cfg.token}/sendMessage"
        payload = {
            "chat_id": self.cfg.chat_id,
            "text": _fit(text),
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = requests.post(url, json=payload, timeout=self.cfg.timeout)
        try:
            body = r.json()
        except Exception:  # noqa: BLE001 - a non-JSON error page is still an error
            body = {"ok": False, "description": (r.text or "")[:200]}
        return r.status_code, body

    # -- public API -----------------------------------------------------------
    def send(self, text: str) -> bool:
        """Deliver one alert. True = the chat got it (or it was a dry run).

        Never raises: the scanner must keep scanning the rest of the universe
        when Telegram is unhappy.
        """
        if not self.cfg.enabled or self.cfg.dry_run:
            log.info("[DRY RUN] Telegram message:\n%s", text)
            print("\n" + "-" * 74)
            print("[DRY RUN Telegram]")
            print(_TAG_RE.sub("", text))
            print("-" * 74)
            self._count("dry_run")
            return True
        if not self.ready:
            self.last_error = "Telegram is not configured (token/chat_id missing)"
            log.error("%s; message dropped:\n%s", self.last_error, text)
            self._count("failed")
            self._count("dropped")
            return False

        if self._muted():
            # a rate-limited or misconfigured bot must not turn a pass into hours
            # of waiting: fail fast now, let the next pass retry (nothing was
            # recorded as sent, so no alert is lost by this)
            self._count("dropped")
            log.info("Telegram sends are paused after an earlier failure — "
                     "message deferred to the next pass")
            return False
        retries = max(1, int(getattr(self.cfg, "max_retries", 4) or 4))
        backoff = max(0.1, float(getattr(self.cfg, "retry_backoff_sec", 2.0) or 2.0))
        budget = max(5.0, float(getattr(self.cfg, "max_wait_sec", 90.0) or 90.0))
        waited = 0.0
        mode: str | None = "HTML"
        last_desc = ""

        for attempt in range(retries):
            if attempt:
                self._count("retried")
            try:
                self._pace()
                code, body = self._post(text, mode)
            except Exception as e:  # noqa: BLE001 - network errors must not kill the scanner
                last_desc = f"{type(e).__name__}: {e}"
                log.warning("Telegram send error (%s), attempt %d/%d", last_desc,
                            attempt + 1, retries)
                nap = backoff * (2 ** attempt)
                if waited + nap < budget:
                    time.sleep(nap)
                    waited += nap
                continue

            if body.get("ok"):
                self._count("sent")
                self.last_error = ""
                self._muted_until = 0.0
                return True

            desc = str(body.get("description") or f"HTTP {code}")
            last_desc = desc
            params = body.get("parameters") or {}
            retry_after = float(params.get("retry_after") or 0.0)

            if code == 429 or "Too Many Requests" in desc:
                # the ONLY acceptable answer to a flood limit is to slow down
                nap = retry_after or backoff * (2 ** attempt)
                self._count("throttled")
                if waited + nap < budget:
                    log.info("Telegram flood limit: waiting %.1fs before retrying "
                             "(%d message(s) queued behind the limit)", nap, retries - attempt - 1)
                    time.sleep(nap)
                    waited += nap
                    continue
                log.error("Telegram kept rate-limiting (waited %.0fs of the %.0fs "
                          "budget) — deferring this alert to the next pass",
                          waited, budget)
                self._count("failed")
                self._count("dropped")
                self.last_error = f"rate limited (429): {desc}"
                self._mute()
                return False

            if code == 400 and mode and ("entity" in desc.lower() or "parse" in desc.lower()
                                        or "can't parse" in desc.lower()):
                # one malformed tag must not cost the user the signal
                log.warning("Telegram rejected the HTML formatting (%s) — resending "
                            "as plain text", desc)
                mode = None
                text = _plain(text)
                self._count("deformatted")
                continue

            if 500 <= code < 600:
                nap = backoff * (2 ** attempt)
                if waited + nap < budget:
                    time.sleep(nap)
                    waited += nap
                    continue
                break

            # 401/403/404: a misconfigured bot/chat — retrying is pointless
            log.error("Telegram send failed (HTTP %s): %s — check TELEGRAM_BOT_TOKEN "
                      "and TELEGRAM_CHAT_ID", code, desc)
            self.last_error = f"HTTP {code}: {desc}"
            self._count("failed")
            self._count("dropped")
            return False

        log.error("Telegram delivery failed after %d attempt(s): %s", retries, last_desc)
        self._count("failed")
        self._count("dropped")
        self.last_error = last_desc
        self._mute()
        return False


__all__ = ["TelegramNotifier", "SAFE_LIMIT", "TG_LIMIT"]
