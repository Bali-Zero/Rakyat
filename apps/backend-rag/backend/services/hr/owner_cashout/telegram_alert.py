"""Telegram alerts for owner cashout sync failures / anomalies.

Uses the same TELEGRAM_BOT_TOKEN env var as the rest of the project.
Chat ID is hardcoded to Zero's private chat (verified 2026-04-07).

Best-effort: never raises — we don't want alerting to break sync.
"""

from __future__ import annotations

import logging
import os

import httpx

from backend.core.env_lists import owner_telegram_chat_id
from backend.core.secret_log_redaction import install_telegram_token_redaction
from backend.core.telegram_retirement import telegram_is_retired

install_telegram_token_redaction()

logger = logging.getLogger(__name__)

TG_TIMEOUT_S = 10.0


async def send_alert(message: str) -> None:
    """Send a Telegram message to owner chat. Never raises."""
    if telegram_is_retired():
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("[CASHOUT] TELEGRAM_BOT_TOKEN missing — skipping alert")
        return

    chat_id = owner_telegram_chat_id()
    if not chat_id:
        logger.warning("[CASHOUT] TELEGRAM_OWNER_CHAT_ID not set — skipping alert")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=TG_TIMEOUT_S) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                logger.warning(
                    "[CASHOUT] Telegram alert failed: %s %s",
                    r.status_code,
                    r.text[:200],
                )
    except Exception as e:
        logger.warning("[CASHOUT] Telegram alert exception: %s", e)
