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


class TestABotEscalationIsNotAHumanRequest:
    """Gate round 1: bot give-ups used to be sent as ``human_request``."""

    @pytest.mark.asyncio
    async def test_abstain_then_human_request_sends_two_with_their_own_wording(
        self, sent: AsyncMock
    ) -> None:
        assert await _notify(601, "bot_escalation:rag_abstain") is True
        assert await _notify(601) is True

        assert sent.await_count == 2
        bot_call, human_call = sent.await_args_list
        assert "Bot" in bot_call.kwargs["subject"]
        assert "manusia" in human_call.kwargs["subject"]
        assert "meminta untuk berbicara" in human_call.kwargs["body"]
        assert "meminta untuk berbicara" not in bot_call.kwargs["body"]

    @pytest.mark.asyncio
    async def test_the_bot_wording_never_says_the_client_asked_for_a_person(
        self, sent: AsyncMock
    ) -> None:
        await _notify(602, "bot_escalation:rag_abstain")

        text = (sent.await_args.kwargs["subject"] + sent.await_args.kwargs["body"]).lower()
        assert "meminta" not in text
        assert "minta bicara" not in text

    @pytest.mark.asyncio
    async def test_the_same_bot_reason_twice_sends_once(self, sent: AsyncMock) -> None:
        assert await _notify(603, "bot_escalation:rag_abstain") is True
        assert await _notify(603, "bot_escalation:rag_abstain") is True
        assert sent.await_count == 1

    @pytest.mark.asyncio
    async def test_two_bot_causes_are_two_alerts(self, sent: AsyncMock) -> None:
        assert await _notify(604, "bot_escalation:rag_abstain") is True
        assert await _notify(604, "bot_escalation:terminal_apology:x") is True
        assert sent.await_count == 2

    @pytest.mark.asyncio
    async def test_the_inbox_bot_helper_sends_bot_wording_and_own_key(
        self, sent: AsyncMock
    ) -> None:
        from backend.services.integrations.wa_inbox_bot import _tell_a_human

        assert await _tell_a_human(phone=PHONE, reason="rag_abstain", thread_id=605) is True
        assert await _notify(605) is True  # explicit request, same thread

        assert sent.await_count == 2
        assert "Klien" not in sent.await_args_list[0].kwargs["subject"]
        assert "Klien minta bicara" in sent.await_args_list[1].kwargs["subject"]
