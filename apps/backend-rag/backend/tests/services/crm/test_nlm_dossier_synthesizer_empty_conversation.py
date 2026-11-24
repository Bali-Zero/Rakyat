"""Regression tests for dossiers without renderable conversation text."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.crm.nlm_dossier_synthesizer import synthesize_client_dossier


def _message(*, body: str | None, message_text: str | None = None) -> dict[str, object]:
    return {
        "message_date": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "direction": "inbound",
        "team_member_email": None,
        "body": body,
        "message_text": message_text,
    }


class TestEmptyConversation:
    @pytest.mark.asyncio
    async def test_skips_messages_without_renderable_source_text(self):
        model_call = AsyncMock(return_value={})
        messages = [
            _message(body=None),
            _message(body="   "),
            _message(body=None, message_text="\t"),
        ]

        with patch(
            "backend.services.crm.nlm_dossier_synthesizer._call_ollama_json",
            new=model_call,
        ):
            result = await synthesize_client_dossier(
                client_id=144,
                display_name="Synthetic Client",
                messages=messages,
            )

        assert result is None
        model_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_synthesizes_when_one_message_has_source_text(self):
        model_call = AsyncMock(return_value={})
        messages = [
            _message(body="  "),
            _message(body="Please prepare the PT PMA quote."),
            _message(body=None),
        ]

        with patch(
            "backend.services.crm.nlm_dossier_synthesizer._call_ollama_json",
            new=model_call,
        ):
            result = await synthesize_client_dossier(
                client_id=144,
                display_name="Synthetic Client",
                messages=messages,
            )

        assert result is not None
        assert result.msg_count == len(messages)
        model_call.assert_awaited_once()
