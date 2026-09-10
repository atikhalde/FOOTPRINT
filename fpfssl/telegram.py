"""Minimal Telegram notifier (plain HTTPS, no bot framework).

Credentials come from config.yaml or the environment:
    TELEGRAM_BOT_TOKEN / telegram.token
    TELEGRAM_CHAT_ID   / telegram.chat_id

Create a bot with @BotFather, get your chat id from @userinfobot, then run:
    python -m fpfssl test-telegram
"""
from __future__ import annotations

import logging
import re

import requests

from .config import TelegramConfig

log = logging.getLogger("fpfssl.telegram")

_TAG_RE = re.compile(r"<[^>]+>")


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg

    @property
    def ready(self) -> bool:
        return bool(self.cfg.token and self.cfg.chat_id)

    def send(self, text: str) -> bool:
        if self.cfg.dry_run or not self.cfg.enabled:
            log.info("[DRY RUN] Telegram message:\n%s", text)
            print("\n" + "-" * 74)
            print("[DRY RUN Telegram]")
            print(_TAG_RE.sub("", text))
            print("-" * 74)
            return True
        if not self.ready:
            log.warning("Telegram not configured (token/chat_id missing); message dropped:\n%s", text)
            return False
        url = f"{self.cfg.api_base}/bot{self.cfg.token}/sendMessage"
        payload = {
            "chat_id": self.cfg.chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=self.cfg.timeout)
            data = r.json()
            if not data.get("ok"):
                log.error("Telegram send failed: %s", data)
                return False
            return True
        except Exception as e:  # noqa: BLE001 - network errors must not kill the scanner
            log.error("Telegram send error: %s", e)
            return False
