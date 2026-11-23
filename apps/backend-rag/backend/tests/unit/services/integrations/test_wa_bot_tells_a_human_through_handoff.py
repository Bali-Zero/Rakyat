"""The inbox bot's "tell a human" helper must reach a colleague.

The helper used to hand its alert to the Telegram transport, which the owner
retired: its send always fails, so every abstain / apology that told the client
"a colleague will look" told nobody. The helper now calls
``wa_human_handoff.notify_human_handoff`` (in-app bell + internal e-mail).

Guilt: on a path that tells a human, the handoff is called once for the
customer's thread and nothing reaches the retired transport.
Innocence: paths that did not tell a human still do not, and a handoff that
raises leaves the customer's reply and the helper's return shape alone.

Synthetic data only. No real message or e-mail is sent: the e-mail sender is a
mock, the transport is a mock.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.core.config import settings
from backend.services.integrations import human_escalation_notifier, wa_human_handoff
from backend.services.rag.agentic._reasoning_stubs import get_localized_stub

BOT = "backend.services.integrations.wa_inbox_bot"
HANDOFF = "backend.services.integrations.wa_human_handoff"
PHONE = "628400001000"


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _thread(thread_id: int = 42) -> dict:
    return {"thread_id": thread_id, "counterpart_phone": PHONE}


@pytest.fixture(autouse=True)
def _clean_dedup():
    human_escalation_notifier._recent_escalations.clear()
    wa_human_handoff._recent_failed_attempts.clear()
    yield
    human_escalation_notifier._recent_escalations.clear()
    wa_human_handoff._recent_failed_attempts.clear()


@pytest.fixture
def mail():
    sent = AsyncMock(return_value=True)
    with (
        patch(f"{HANDOFF}.send_internal_email", sent),
        patch(f"{HANDOFF}._resolve_assignee", AsyncMock(return_value=(None, None))),
    ):
        yield sent


@pytest.fixture
def retired_transport():
    send = AsyncMock(side_effect=RuntimeError("transport retired"))
    with (
        patch.object(settings, "admin_telegram_chat_id", "synthetic-chat"),
        patch.object(human_escalation_notifier.telegram_bot, "send_message", send),
    ):
        yield send


async def _generate(payload: dict, *, pool: object = None, thread_id: int = 42):
    from backend.services.integrations import wa_inbox_bot

    client = AsyncMock()
    client.post = AsyncMock(return_value=_Resp(payload))
    with (
        patch(f"{BOT}.is_bot_autoreply_enabled", return_value=True),
        patch(
            f"{BOT}._load_thread_context",
            AsyncMock(return_value=("Synthetic Client asks about a permit", [])),
        ),
        patch(f"{BOT}._get_rag_client", AsyncMock(return_value=client)),
    ):
        return await wa_inbox_bot.generate_bot_reply(pool, _thread(thread_id))


class TestGuiltAColleagueIsActuallyTold:
    @pytest.mark.asyncio
    async def test_an_abstain_sends_one_handoff_for_the_thread(
        self, mail: AsyncMock, retired_transport: AsyncMock
    ) -> None:
        pool = object()

        reply = await _generate(
            {"abstain": True, "abstain_reason": "no_relevant_context"}, pool=pool
        )

        mail.assert_awaited_once()
        kwargs = mail.await_args.kwargs
        assert kwargs["email_type"] == "wa_human_handoff"
        assert "42" in kwargs["subject"]
        assert kwargs["pool"] is pool
        assert PHONE not in kwargs["subject"] + kwargs["body"]
        retired_transport.assert_not_awaited()
        assert reply == get_localized_stub("abstain", "ENGLISH")

    @pytest.mark.asyncio
    async def test_the_escalate_marker_sends_one_handoff_and_keeps_the_answer(
        self, mail: AsyncMock, retired_transport: AsyncMock
    ) -> None:
        reply = await _generate({"abstain": False, "answer": "Here is your answer.[ESCALATE]"})

        mail.assert_awaited_once()
        retired_transport.assert_not_awaited()
        assert reply == "Here is your answer."

    @pytest.mark.asyncio
    async def test_a_silent_exit_tells_a_colleague_and_still_raises(
        self, mail: AsyncMock, retired_transport: AsyncMock
    ) -> None:
        with pytest.raises(RuntimeError, match="empty RAG answer"):
            await _generate({"abstain": False, "answer": "   "})

        mail.assert_awaited_once()
        retired_transport.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_helper_reports_delivery_the_way_it_always_did(self, mail: AsyncMock) -> None:
        from backend.services.integrations.wa_inbox_bot import _tell_a_human

        assert await _tell_a_human(phone=PHONE, reason="rag_abstain", thread_id=7) is True

        mail.return_value = False
        assert await _tell_a_human(phone=PHONE, reason="rag_abstain", thread_id=8) is False

    @pytest.mark.asyncio
    async def test_the_outbox_apology_caller_reaches_a_colleague_without_a_pool(
        self, mail: AsyncMock, retired_transport: AsyncMock
    ) -> None:
        """Callers outside this module do not pass a pool: the e-mail leg
        still goes out (to the fixed recipient) instead of nothing."""
        from backend.services.integrations.wa_inbox_bot import _tell_a_human

        delivered = await _tell_a_human(
            phone=PHONE, reason="terminal_apology:max_attempts", thread_id=9
        )

        assert delivered is True
        mail.assert_awaited_once()
        retired_transport.assert_not_awaited()


class TestInnocenceNothingChangesWhereNobodyWasTold:
    @pytest.mark.asyncio
    async def test_a_normal_answer_tells_nobody(
        self, mail: AsyncMock, retired_transport: AsyncMock
    ) -> None:
        reply = await _generate({"abstain": False, "answer": "A KITAS needs X, Y, Z."})

        mail.assert_not_awaited()
        retired_transport.assert_not_awaited()
        assert "KITAS needs X, Y, Z" in reply

    @pytest.mark.asyncio
    async def test_a_handoff_that_raises_leaves_the_reply_as_it_is(self) -> None:
        boom = AsyncMock(side_effect=RuntimeError("handoff broke"))
        with patch(f"{HANDOFF}._resolve_assignee", boom):
            reply = await _generate({"abstain": True, "abstain_reason": "no_relevant_context"})

        assert reply == get_localized_stub("abstain", "ENGLISH")

    @pytest.mark.asyncio
    async def test_a_failing_handoff_is_reported_false_not_raised(self) -> None:
        from backend.services.integrations.wa_inbox_bot import _tell_a_human

        boom = AsyncMock(side_effect=RuntimeError("handoff broke"))
        with patch(f"{HANDOFF}._resolve_assignee", boom):
            assert await _tell_a_human(phone=PHONE, reason="rag_abstain", thread_id=5) is False
