"""Telegram failure must not reopen an already-completed WhatsApp onboarding reply."""

from __future__ import annotations

import ast
import inspect
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest


async def _run_onboarding_case(
    monkeypatch,
    *,
    stale_token="synthetic-stale-token",
    detector_error=None,
    configured_notification_error=None,
):
    """Pin the business success boundary using real Telegram singleton behavior.

    All sender identifiers, content and credentials below are synthetic. The
    fallback collaborators are functional mocks so an accidental fallthrough is
    observed as a duplicate send/audit, not hidden by a mock dependency error.
    """
    from backend.app.routers import whatsapp_chat as route
    from backend.core.telegram_retirement import telegram_is_retired
    from backend.services import whatsapp_ack, whatsapp_context_builder
    from backend.services.integrations.telegram_bot_service import telegram_bot
    from backend.services.integrations.whatsapp_triage_service import TriageDecision

    assert telegram_is_retired() is True
    assert not hasattr(route, "telegram_bot")
    if stale_token is None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", stale_token)
    monkeypatch.setenv("TG_OWNER_TELEGRAM", "1")
    monkeypatch.setattr(route.settings, "telegram_bot_token", stale_token)
    monkeypatch.setattr(route.settings, "admin_telegram_chat_id", "synthetic-admin-chat")
    monkeypatch.setattr(telegram_bot, "_token", stale_token)
    monkeypatch.setattr(telegram_bot, "_client", None)
    clients = Mock(side_effect=AssertionError("Telegram must not construct an HTTP client"))
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    notification = None
    if configured_notification_error is not None:
        # The integration must be absent: even a configured failing sender is
        # never awaited. No production policy is changed by this test.
        notification = AsyncMock(side_effect=configured_notification_error)
        monkeypatch.setattr(telegram_bot, "send_message", notification)

    conn = SimpleNamespace(execute=AsyncMock())
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = SimpleNamespace(acquire=Mock(return_value=acquired))
    monkeypatch.setattr(route, "_get_db_pool", lambda request: pool)
    monkeypatch.setattr(route, "_claim_wamid_reply", AsyncMock(return_value=True))
    monkeypatch.setattr(route, "_conversation_cache", defaultdict(list))

    wa = SimpleNamespace(
        mark_message_read=AsyncMock(),
        send_message=AsyncMock(),
        chunk_message=Mock(side_effect=lambda text, **kwargs: [text]),
    )
    triage = SimpleNamespace(
        is_allowed=Mock(return_value=True),
        should_escalate=AsyncMock(
            return_value=(TriageDecision.BOT_CAN_HANDLE, "synthetic-business")
        ),
        get_escalation_message=Mock(return_value="synthetic escalation"),
        get_welcome_message=Mock(return_value="synthetic welcome"),
    )
    detector = SimpleNamespace(
        detect_and_trigger=AsyncMock(return_value={"chain": "synthetic", "status": "triggered"})
    )
    if detector_error is not None:
        detector.detect_and_trigger.side_effect = detector_error
    monkeypatch.setattr(route, "whatsapp_service", wa)
    monkeypatch.setattr(route, "whatsapp_triage_service", triage)
    monkeypatch.setattr(route, "get_onboarding_detector", lambda: detector)
    audit = AsyncMock()
    monkeypatch.setattr(route, "_persist_audit_messages", audit)

    context = {
        "_wa_user_id": "synthetic-user",
        "_session_id": "synthetic-session",
        "_existing_row_id": None,
        "client_name": "synthetic",
        "client_profile": {},
        "conversation_history": [],
        "detected_language": "en",
        "is_first_message": False,
        "sender_identity": "synthetic",
        "time_of_day": "day",
    }
    ai_context = AsyncMock(return_value=context)
    ai_reply = AsyncMock(return_value="synthetic fallback response")
    escalation = AsyncMock()
    monkeypatch.setattr(whatsapp_context_builder, "build_context", ai_context)
    monkeypatch.setattr(whatsapp_ack, "should_send_ack", lambda *args: False)
    monkeypatch.setattr(route, "ask_openclaw_whatsapp", ai_reply)
    monkeypatch.setattr(
        route,
        "sanitize_whatsapp_kbli_reply",
        lambda **kwargs: SimpleNamespace(corrected=False, reply=kwargs["reply"]),
    )
    monkeypatch.setattr(route, "notify_human_handoff", escalation)
    conversation_log = AsyncMock()
    monkeypatch.setattr(route, "notify_zero_conversation_log", conversation_log)

    await route.process_whatsapp_message(
        phone="0000000000",
        message_text="need visa",
        sender_name="synthetic fixture",
        message_id="synthetic-wamid",
        request=SimpleNamespace(),
    )

    observed = {
        "whatsapp_sends": wa.send_message.await_count,
        "audit_writes": audit.await_count,
        "ai_context_calls": ai_context.await_count,
        "ai_reply_calls": ai_reply.await_count,
        "escalation_calls": escalation.await_count,
        "escalation_message_calls": triage.get_escalation_message.call_count,
        "welcome_message_calls": triage.get_welcome_message.call_count,
        "conversation_log_calls": conversation_log.await_count,
        "http_clients": clients.call_count,
        "telegram_notification_calls": notification.await_count
        if notification is not None
        else None,
    }
    return observed


def _required_onboarding_counts(*, notification_calls=None):
    return {
        "whatsapp_sends": 1,
        "audit_writes": 1,
        "ai_context_calls": 0,
        "ai_reply_calls": 0,
        "escalation_calls": 0,
        "escalation_message_calls": 0,
        "welcome_message_calls": 0,
        "conversation_log_calls": 0,
        "http_clients": 0,
        "telegram_notification_calls": notification_calls,
    }


@pytest.mark.parametrize(
    "stale_token", ["synthetic-stale-token", None], ids=["stale-token", "no-token"]
)
@pytest.mark.asyncio
async def test_real_retired_singleton_cannot_duplicate_whatsapp_onboarding(
    monkeypatch, stale_token
):
    observed = await _run_onboarding_case(monkeypatch, stale_token=stale_token)
    assert observed == _required_onboarding_counts()


@pytest.mark.asyncio
async def test_genuine_detector_exception_still_allows_one_normal_ai_reply(monkeypatch):
    observed = await _run_onboarding_case(
        monkeypatch, detector_error=RuntimeError("synthetic detector failure")
    )
    expected = _required_onboarding_counts()
    expected.update(ai_context_calls=1, ai_reply_calls=1, conversation_log_calls=1)
    assert observed == expected


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("synthetic optional notification failure"),
        ValueError("synthetic optional notification failure"),
        httpx.HTTPError("synthetic optional notification failure"),
    ],
    ids=["runtime-error", "value-error", "http-error"],
)
@pytest.mark.asyncio
async def test_removed_notification_never_invokes_a_configured_failure(monkeypatch, error):
    observed = await _run_onboarding_case(monkeypatch, configured_notification_error=error)
    assert observed == _required_onboarding_counts(notification_calls=0)


def test_onboarding_processor_has_no_direct_telegram_notification_call():
    from backend.app.routers import whatsapp_chat

    tree = ast.parse(inspect.getsource(whatsapp_chat.process_whatsapp_message))
    calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_message"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "telegram_bot"
    ]
    assert calls == [], "Telegram integration must be removed, not hidden behind a guard"
    branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "onboarding_result"
    ]
    assert len(branches) == 1 and not branches[0].orelse
    assert [
        ast.unparse(node.value.func)
        for node in ast.walk(branches[0])
        if isinstance(node, ast.Await)
    ] == ["whatsapp_service.send_message", "_persist_audit_messages"]
    assert isinstance(branches[0].body[-1], ast.Return)
