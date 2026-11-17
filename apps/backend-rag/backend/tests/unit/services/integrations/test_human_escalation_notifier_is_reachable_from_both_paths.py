"""The human-escalation notifier must be reachable without a router.

Why this exists. Two WhatsApp surfaces need to tell a human that a conversation
needs them: the router (`whatsapp_chat.py`) and the inbox bot behind
`wa_outbox_worker` (a background service). Until 2026-08-11 the notifier lived
inside the router, so only the router could escalate — and `wa_inbox_bot.py`
stripped the persona's `[ESCALATE]` marker under a comment saying *"mirror
whatsapp_chat.py"* while calling nothing. It mirrored the FORM and not the
EFFECT.

The property that keeps that from happening again is not "the function moved"
(a grep proves that and proves nothing). It is: **a background service can
reach the notifier without importing a FastAPI router.** That is what the first
test asserts, by importing the notifier into a fresh interpreter with the router
absent and checking the router never gets pulled in — a plain import in this
process would pass even if the module dragged the whole web app behind it,
because pytest has already imported half the app.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

NOTIFIER = "backend.services.integrations.human_escalation_notifier"
ROUTER = "backend.app.routers.whatsapp_chat"


def test_importing_the_notifier_does_not_drag_in_the_router() -> None:
    """A background service must not have to import a router to escalate.

    Run in a SUBPROCESS on purpose: inside the pytest process the router is
    already imported by other tests, so `sys.modules` would show it present no
    matter what this module does, and the assertion would be vacuous.
    """
    code = (
        "import importlib, sys\n"
        f"importlib.import_module({NOTIFIER!r})\n"
        f"print({ROUTER!r} in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"notifier failed to import standalone: {proc.stderr[-800:]}"
    assert proc.stdout.strip() == "False", (
        "importing the notifier pulled the FastAPI router in with it — the "
        f"extraction bought nothing. stdout={proc.stdout!r}"
    )


def test_the_router_still_exposes_the_same_function_object() -> None:
    """The router uses the exact wa_human_handoff notifier, not a fork."""
    from backend.app.routers import whatsapp_chat
    from backend.services.integrations import wa_human_handoff

    assert whatsapp_chat.notify_human_handoff is wa_human_handoff.notify_human_handoff


@pytest.mark.asyncio
async def test_compat_notifier_delegates_to_wa_human_handoff_without_forwarding_pii() -> None:
    """Legacy wrapper forwards escalation to wa_human_handoff and drops PII text."""
    mock_handoff = AsyncMock(return_value=True)
    with patch(
        "backend.services.integrations.wa_human_handoff.notify_human_handoff",
        mock_handoff,
    ):
        from backend.services.integrations.human_escalation_notifier import (
            notify_human_telegram,
        )

        delivered = await notify_human_telegram(
            phone="628000000123",
            message_text="Synthetic confidential draft text",
            sender_name="Synthetic Sender",
            reason="ai_escalation",
            client_profile={"detected_language": "it"},
            conversation_history=[{"role": "user", "content": "Synthetic msg"}],
            thread_ref="77",
        )

    assert delivered is True
    mock_handoff.assert_awaited_once_with(
        None,
        thread_id="77",
        counterpart_phone="628000000123",
        language="it",
        reason="ai_escalation",
    )


@pytest.mark.asyncio
async def test_compat_notifier_defaults_missing_language_and_thread_ref() -> None:
    """Wrapper safely handles missing client_profile/thread_ref without Telegram."""
    mock_handoff = AsyncMock(return_value=False)
    with patch(
        "backend.services.integrations.wa_human_handoff.notify_human_handoff",
        mock_handoff,
    ):
        from backend.services.integrations.human_escalation_notifier import (
            notify_human_telegram,
        )

        delivered = await notify_human_telegram(phone="62123", message_text="Help")

    assert delivered is False
    mock_handoff.assert_awaited_once_with(
        None,
        thread_id=None,
        counterpart_phone="62123",
        language="en",
        reason="personal_contact",
    )
