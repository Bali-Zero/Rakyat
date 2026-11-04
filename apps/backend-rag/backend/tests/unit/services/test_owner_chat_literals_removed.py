"""The owner Telegram chat id comes from TELEGRAM_OWNER_CHAT_ID only; unset means no send, never a literal."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BACKEND = Path(__file__).resolve().parents[3]
MODULES = (
    "app/routers/visa_oracle.py",
    "services/compliance/lkpm_deadline_notifier.py",
    "services/hr/owner_cashout/telegram_alert.py",
)


def _chat_id_shaped_constants(path: Path) -> list[int]:
    """Line numbers of int constants >= 1e8 or all-digit strings >= 8 chars (a chat id / phone shape)."""
    hits = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, int) and not isinstance(node.value, bool) and abs(node.value) >= 10**8:
                hits.append(node.lineno)
            elif isinstance(node.value, str) and node.value.lstrip("-").isdigit() and len(node.value) >= 8:
                hits.append(node.lineno)
    return hits


@pytest.mark.parametrize("rel", MODULES)
def test_module_holds_no_chat_id_shaped_literal(rel):
    assert _chat_id_shaped_constants(BACKEND / rel) == []


def test_cashout_alert_unset_owner_sends_nothing(monkeypatch):
    from backend.services.hr.owner_cashout import telegram_alert

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    with patch.object(telegram_alert.httpx, "AsyncClient") as client:
        asyncio.run(telegram_alert.send_alert("hello"))
    client.assert_not_called()


def test_cashout_alert_uses_the_env_chat_id(monkeypatch):
    from backend.services.hr.owner_cashout import telegram_alert

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "990000001")
    post = AsyncMock(return_value=MagicMock(status_code=200, text=""))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
    client.__aexit__ = AsyncMock(return_value=False)
    with patch.object(telegram_alert.httpx, "AsyncClient", return_value=client):
        asyncio.run(telegram_alert.send_alert("hello"))
    assert post.call_args.kwargs["json"]["chat_id"] == "990000001"


def test_lkpm_alert_unset_owner_sends_nothing(monkeypatch):
    from backend.services.compliance import lkpm_deadline_notifier as mod

    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    notifier = object.__new__(mod.LKPMDeadlineNotifier)
    with patch("backend.services.integrations.telegram_bot_service.TelegramBotService") as bot:
        asyncio.run(notifier._send_telegram_alert([{"company_name": "c", "quarter": "Q1", "year": 2026}]))
    bot.assert_not_called()
