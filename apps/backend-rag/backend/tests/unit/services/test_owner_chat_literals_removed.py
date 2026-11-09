"""The owner Telegram chat id comes from TELEGRAM_OWNER_CHAT_ID only; unset means no send, never a literal."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import patch

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
            if (
                isinstance(node.value, int)
                and not isinstance(node.value, bool)
                and abs(node.value) >= 10**8
            ):
                hits.append(node.lineno)
            elif (
                isinstance(node.value, str)
                and node.value.lstrip("-").isdigit()
                and len(node.value) >= 8
            ):
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


@pytest.mark.parametrize(
    "configured, expected",
    [("990000001", "990000001"), (" 990000001 ", "990000001"), ("", ""), ("   ", "")],
)
def test_owner_chat_configuration_is_read_and_trimmed_without_transport(
    monkeypatch, configured, expected
):
    from backend.core.env_lists import owner_telegram_chat_id

    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", configured)
    assert owner_telegram_chat_id() == expected


def test_owner_chat_configuration_unset_is_empty(monkeypatch):
    from backend.core.env_lists import owner_telegram_chat_id

    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    assert owner_telegram_chat_id() == ""


def test_retired_cashout_alert_ignores_stale_owner_configuration(monkeypatch):
    from backend.services.hr.owner_cashout import telegram_alert

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "990000001")
    with patch.object(telegram_alert.httpx, "AsyncClient") as client:
        result = asyncio.run(telegram_alert.send_alert("hello"))
    assert result is None
    client.assert_not_called()


def test_lkpm_alert_unset_owner_sends_nothing(monkeypatch):
    from backend.services.compliance import lkpm_deadline_notifier as mod

    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    notifier = object.__new__(mod.LKPMDeadlineNotifier)
    with patch("backend.services.integrations.telegram_bot_service.TelegramBotService") as bot:
        asyncio.run(
            notifier._send_telegram_alert([{"company_name": "c", "quarter": "Q1", "year": 2026}])
        )
    bot.assert_not_called()
