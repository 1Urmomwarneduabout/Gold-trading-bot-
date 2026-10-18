"""Alerts to Telegram (optional) and heartbeat pings. Failures here never stop trading."""

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("goldbot")


class Notifier:
    def __init__(self, telegram_token: str | None = None, telegram_chat_id: str | None = None, prefix: str = "goldbot"):
        self.token = telegram_token
        self.chat_id = telegram_chat_id
        self.prefix = prefix

    @classmethod
    def from_env(cls, prefix: str = "goldbot") -> "Notifier":
        return cls(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"), prefix)

    def send(self, text: str, level: int = logging.INFO) -> None:
        log.log(level, text)
        if not (self.token and self.chat_id):
            return
        body = json.dumps({"chat_id": self.chat_id, "text": f"[{self.prefix}] {text}"}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:  # an alert failing must never crash the bot
            log.warning("Telegram alert failed: %s", exc)


def ping(url: str | None, suffix: str = "") -> None:
    """Hit a dead-man's-switch URL (e.g. healthchecks.io). It alerts you when pings stop."""
    if not url:
        return
    try:
        urllib.request.urlopen(url.rstrip("/") + suffix, timeout=10).close()
    except Exception as exc:
        log.warning("Heartbeat ping failed: %s", exc)
