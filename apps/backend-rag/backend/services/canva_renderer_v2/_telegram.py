"""Best-effort Telegram notify. Failures are swallowed (no raise).

Reads TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_CHAT_ID env vars.
If TELEGRAM_BOT_TOKEN absent, silently no-op.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request

from backend.core.secret_log_redaction import install_telegram_token_redaction
from backend.core.telegram_retirement import telegram_is_retired

install_telegram_token_redaction()

logger = logging.getLogger(__name__)


def send_telegram(text: str) -> None:
    if telegram_is_retired():
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "").strip()
    if not token:
        return
    if not chat_id:
        logger.warning("TELEGRAM_OWNER_CHAT_ID not set - telegram send skipped")
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        ).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data,
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram send failed (swallowed): %s", e)
