"""The handoff dedup window is per thread AND reason.

A caption-less attachment alert used to silence, for the whole window, a later
alert on the same thread where the client explicitly asked for a human: the
second call returned True ("someone was told") while nobody had been told
about the request. Same reason twice inside the window still sends once.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.services.integrations import human_escalation_notifier, wa_human_handoff
from backend.services.integrations.wa_human_handoff import notify_human_handoff

HANDOFF = "backend.services.integrations.wa_human_handoff"
PHONE = "628400001000"
ATTACHMENT = "media_attachment_no_caption"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    human_escalation_notifier._recent_escalations.clear()
    wa_human_handoff._recent_failed_attempts.clear()
    monkeypatch.setattr(f"{HANDOFF}._resolve_assignee", AsyncMock(return_value=(None, None)))
    yield
    human_escalation_notifier._recent_escalations.clear()
    wa_human_handoff._recent_failed_attempts.clear()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mail = AsyncMock(return_value=True)
    monkeypatch.setattr(f"{HANDOFF}.send_internal_email", mail)
    return mail


async def _notify(thread_id: int, reason: str | None = None) -> bool:
    kwargs = {} if reason is None else {"reason": reason}
    return await notify_human_handoff(
        object(), thread_id=thread_id, counterpart_phone=PHONE, language="en", **kwargs
    )


class TestGuiltAnotherReasonIsNotSilenced:
    @pytest.mark.asyncio
    async def test_attachment_alert_then_human_request_sends_both(self, sent: AsyncMock) -> None:
        assert await _notify(501, ATTACHMENT) is True
        assert await _notify(501) is True

        assert sent.await_count == 2
        subjects = [call.kwargs["subject"] for call in sent.await_args_list]
        assert "lampiran" in subjects[0]
        assert "manusia" in subjects[1]

    @pytest.mark.asyncio
    async def test_a_failed_attempt_for_one_reason_does_not_throttle_another(
        self, sent: AsyncMock
    ) -> None:
        sent.return_value = False
        assert await _notify(502, ATTACHMENT) is False

        sent.return_value = True
        assert await _notify(502) is True
        assert sent.await_count == 2


class TestInnocenceTheSameReasonIsStillDeduplicated:
    @pytest.mark.asyncio
    async def test_the_same_reason_twice_sends_once(self, sent: AsyncMock) -> None:
        assert await _notify(503) is True
        assert await _notify(503) is True
        assert sent.await_count == 1

    @pytest.mark.asyncio
    async def test_the_same_attachment_reason_twice_sends_once(self, sent: AsyncMock) -> None:
        assert await _notify(504, ATTACHMENT) is True
        assert await _notify(504, ATTACHMENT) is True
        assert sent.await_count == 1

    @pytest.mark.asyncio
    async def test_other_threads_are_unaffected(self, sent: AsyncMock) -> None:
        assert await _notify(505) is True
        assert await _notify(506) is True
        assert sent.await_count == 2
