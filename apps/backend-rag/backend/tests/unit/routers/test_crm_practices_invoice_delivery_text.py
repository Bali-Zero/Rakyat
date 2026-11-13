"""D4b: the texts that talk about invoice delivery say what actually happened.

Spec rows (behaviour -> test), see the PR body:
T1 the outcome rule: sent / failed / no_email -> test_outcome_rule_truth_table
T2 regenerate endpoint, mail went out: says "emailed to the client" (innocence)
   -> test_regenerate_message_says_emailed_only_when_sent
T3 regenerate endpoint, client has no email: says NO email / NOT emailed / contact by WhatsApp, and
   never "sent successfully" -> test_regenerate_message_for_a_client_without_email
T4 regenerate endpoint, send failed: says FAILED / NOT received / send it yourself
   -> test_regenerate_message_for_a_failed_send
T5 the client-visible timeline entry written at the status move does not claim "Invoice sent"
   (the automation has not run yet); the other statuses are unchanged
   -> test_timeline_entry_does_not_claim_the_invoice_was_sent
T6 the trigger result carries email_attempted, so the endpoint can tell no_email from failed
   -> test_trigger_result_carries_email_attempted (in test_invoice_no_email_text-style flow)
Synthetic data only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.routers import crm_practices
from backend.services.invoicing.client_email_outcome import (
    FAILED,
    NO_EMAIL,
    SENT,
    client_email_outcome,
    regenerated_message,
)

USER: dict[str, Any] = {"id": "u1", "email": "team@example.test", "role": "admin"}


def test_outcome_rule_truth_table() -> None:
    assert client_email_outcome(True, True) == SENT
    assert client_email_outcome(True, False) == SENT
    assert client_email_outcome(False, True) == FAILED
    assert client_email_outcome(False, False) == NO_EMAIL


async def regenerate(result: dict[str, Any]) -> dict[str, Any]:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"status": "sending_invoice"})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    service = MagicMock()
    service.regenerate_invoice = AsyncMock(return_value={"success": True, **result})
    with patch.object(crm_practices, "InvoiceAutomationService", return_value=service):
        return await crm_practices.regenerate_invoice(
            request=MagicMock(), practice_id=1, db_pool=pool, current_user=USER
        )


@pytest.mark.asyncio
async def test_regenerate_message_says_emailed_only_when_sent() -> None:
    out = await regenerate({"invoice_number": "INV-1", "email_sent": True, "email_attempted": True})
    assert out["message"] == "Invoice INV-1 regenerated and emailed to the client"
    assert "NOT" not in out["message"]


@pytest.mark.asyncio
async def test_regenerate_message_for_a_client_without_email() -> None:
    out = await regenerate(
        {"invoice_number": "INV-2", "email_sent": False, "email_attempted": False}
    )
    msg = out["message"]
    assert "NO email on file" in msg and "NOT emailed" in msg
    assert "WhatsApp or another channel" in msg
    assert "sent successfully" not in msg and "emailed to the client" not in msg


@pytest.mark.asyncio
async def test_regenerate_message_for_a_failed_send() -> None:
    out = await regenerate(
        {"invoice_number": "INV-3", "email_sent": False, "email_attempted": True}
    )
    msg = out["message"]
    assert "FAILED" in msg and "NOT received it" in msg
    assert "send it to them yourself" in msg
    assert "sent successfully" not in msg


def test_regenerated_message_without_a_number_does_not_print_none() -> None:
    assert "None" not in regenerated_message(None, SENT)


def test_timeline_entry_does_not_claim_the_invoice_was_sent() -> None:
    event_type, title, _ = crm_practices.STATUS_TIMELINE_EVENTS["sending_invoice"]
    assert event_type == "payment_due"
    assert "sent" not in title.lower()
    assert title == "Invoice being prepared"
    # innocence: the other statuses keep their entries
    assert crm_practices.STATUS_TIMELINE_EVENTS["completed"][1] == "Case completed"
    assert crm_practices.STATUS_TIMELINE_EVENTS["waiting_documents"][1] == "Documents needed"
    assert crm_practices.STATUS_TIMELINE_EVENTS["on_process"][1] == "Application in process"
