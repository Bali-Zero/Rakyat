"""PII log hygiene for the omnichannel router's thread-update and outbound-reply lines.

Class B of the 2026-11-04 PII-log window. ``update_thread`` logged the acting staff
member's email and the whole update body (whose ``assigned_to`` is another email) at
INFO; ``_send_via_channel`` logged the first 50 characters of the outbound reply, and
echoed the request's free-text ``channel`` when no adapter matched it. The lines now carry
the redacted actor, the NAMES of the changed fields and the reply length, and the
unmatched-channel warning is a constant. GUILT: no email, note, reply fragment or
requested channel string reaches a log record, even when the request body carries an
extra free-text field. INNOCENCE: the lines are still emitted with the thread id, the
redacted actor, the field names, the channel and the length.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from backend.app.routers import omnichannel
from backend.security.pii_log_identifier import redact_identifier_for_log

_LOGGER = "backend.app.routers.omnichannel"
_ACTOR = "actor.probe@example.com"
_ASSIGNEE = "assignee.probe@example.com"
_NOTE = "SENTINELNOTE the client said their passport expires soon"
_REPLY = "SENTINELREPLY your KITAS application was approved"
_CHANNEL = "channel.probe@example.com"


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER]


@pytest.mark.asyncio
async def test_thread_update_line_carries_field_names_not_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    thread_id = uuid4()
    body = omnichannel.ThreadUpdateRequest.model_validate({"assigned_to": _ASSIGNEE, "note": _NOTE})
    manager = MagicMock()
    manager.update_thread = AsyncMock(return_value=True)
    with (
        patch.object(omnichannel, "_get_db", return_value=MagicMock()),
        patch.object(omnichannel, "can_view_all_clients", return_value=True),
        patch.object(omnichannel, "_get_thread_manager", return_value=manager),
        patch.object(omnichannel, "_emit_thread_event", new=AsyncMock()),
        patch.object(omnichannel, "invalidate_cache", new=AsyncMock()),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
    ):
        caplog.clear()
        result = await omnichannel.update_thread(
            MagicMock(), thread_id=thread_id, body=body, user={"email": _ACTOR}
        )

    assert result == {"status": "updated", "thread_id": str(thread_id)}
    lines = _messages(caplog)
    blob = "\n".join(lines)
    for needle in (_ACTOR, _ASSIGNEE, "SENTINELNOTE", "@example.com", "note"):
        assert needle not in blob
    assert lines == [
        f"Thread {thread_id} updated by {redact_identifier_for_log(_ACTOR)}: fields=['assigned_to']"
    ]
    manager.update_thread.assert_awaited_once()
    assert manager.update_thread.await_args.kwargs["assigned_to"] == _ASSIGNEE


@pytest.mark.asyncio
async def test_outbound_reply_line_carries_the_length_not_the_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = MagicMock()
    request.app.state.channel_router = MagicMock(adapters={"whatsapp": object()})
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        await omnichannel._send_via_channel(request, None, "whatsapp", _REPLY)

    lines = _messages(caplog)
    assert "SENTINELREPLY" not in "\n".join(lines)
    assert lines == [f"Outbound reply via whatsapp: {len(_REPLY)} chars"]


@pytest.mark.asyncio
async def test_unmatched_channel_warning_carries_no_requested_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = MagicMock()
    request.app.state.channel_router = MagicMock(adapters={"whatsapp": object()})
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        await omnichannel._send_via_channel(request, None, _CHANNEL, _REPLY)

    lines = _messages(caplog)
    blob = "\n".join(lines)
    for needle in (_CHANNEL, "channel.probe", "@example.com", "SENTINELREPLY"):
        assert needle not in blob
    assert lines == ["No adapter registered for the requested outbound channel"]
