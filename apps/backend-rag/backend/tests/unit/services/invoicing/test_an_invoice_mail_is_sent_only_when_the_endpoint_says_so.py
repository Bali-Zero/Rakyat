"""An invoice mail counts as sent only when the send endpoint says it was sent.

The internal send endpoint answers HTTP 200 with ``success: false`` when every provider failed
(a rejected or mistyped address ends there too) and with ``delivery_uncertain`` when a keyed send
cannot be confirmed. The client's invoice mail and the accounting notice looked at the status
code only: the invoice was recorded as sent, accounting was told the client had it, the client's
timeline showed it, and nobody was paged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import backend.services.invoicing.invoice_service as invoice_module
from backend.services.invoicing.client_email_outcome import FAILED, SENT
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES
from backend.services.invoicing.invoice_service import ACCOUNTING_EMAIL, InvoiceAutomationService
from backend.services.notifications.email_audit import EmailNotDelivered

ALL_PROVIDERS_FAILED = {"success": False, "message": "All providers failed: brevo, resend, zoho"}
UNCERTAIN = {"success": False, "message": "delivery uncertain", "delivery_uncertain": True}
SENT_ANSWER = {"success": True, "message": "Email sent"}

NOT_SENT = [
    pytest.param(ALL_PROVIDERS_FAILED, id="every provider failed"),
    pytest.param(UNCERTAIN, id="delivery uncertain"),
]


class _Mail:
    """The mail endpoint: answers the client's mail and the accounting notice separately."""

    def __init__(self, client_answer: Any, accounting_answer: Any) -> None:
        self.answers = {"client": client_answer, "accounting": accounting_answer}
        self.sent_to: list[str] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._answer))

    def _answer(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        which = "accounting" if payload["to"] == ACCOUNTING_EMAIL else "client"
        self.sent_to.append(which)
        return httpx.Response(200, json=self.answers[which])


@pytest.fixture
def service():
    connection = AsyncMock()
    now = datetime.now(tz=timezone.utc)
    connection.fetchrow = AsyncMock(
        side_effect=[
            {  # the practice
                "id": 1,
                "client_id": 100,
                "practice_type_code": "VISA_B1_VOA",
                "notes": "KITAS processing",
                "quoted_price": 5_000_000,
                "client_drive_folder_id": "folder-123",
            },
            {  # the client
                "id": 100,
                "full_name": "Zz Client",
                "email": "client@example.test",
                "phone": "+620000000",
                "address": "Jl. Example 1",
                "nationality": "Australian",
            },
            {  # issuer routing lookup
                "db_now": now,
                "practice_exists": True,
                "invoice_row_exists": False,
                "issuer_entity_key": None,
                "issuer_routed_at": None,
                "practice_documents": None,
            },
            {"client_id": 100, "quoted_price": 5_000_000},
            {"issuer_entity_key": DEFAULT_ENTITY_KEY, "issuer_routed_at": now},
        ]
    )
    connection.fetchval = AsyncMock(return_value=None)
    connection.execute = AsyncMock()
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=connection)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquired)

    drive = MagicMock()
    drive.upload_file_to_folder = AsyncMock(return_value={"id": "file-1", "webViewLink": "link"})
    generator = MagicMock()
    generator.generate_invoice_number = MagicMock(return_value="INV-2026-001")
    generator.generate = MagicMock(return_value=b"%PDF-1.4 example")
    with (
        patch.object(invoice_module, "ServiceAccountDriveService", return_value=drive),
        patch.object(invoice_module, "InvoiceGenerator", return_value=generator),
    ):
        return InvoiceAutomationService(pool)


async def _trigger(service: InvoiceAutomationService, mail: _Mail) -> dict[str, Any]:
    """One full run of the invoice trigger; what it answered and what it recorded."""
    recorded = AsyncMock()
    paged = MagicMock()
    timeline = AsyncMock()
    accounting = AsyncMock(wraps=service._send_accounting_notification)
    with (
        patch.object(invoice_module, "get_email_client", AsyncMock(return_value=mail.http)),
        patch.object(invoice_module, "log_email_attempt", AsyncMock(return_value=7)),
        patch.object(invoice_module, "record_email_result", recorded),
        patch.object(invoice_module, "notify_email_failure_critical", paged),
        patch.object(service, "_record_invoice_sent_on_client_timeline", timeline),
        patch.object(service, "_send_accounting_notification", accounting),
    ):
        result = await service.trigger_on_sending_invoice(1, "staff@example.test")
    return {
        "result": result,
        "audit": [call.kwargs["status"] for call in recorded.await_args_list],
        "paged": [call.kwargs["email_type"] for call in paged.call_args_list],
        "timeline": timeline.await_count,
        "told_accounting": accounting.await_args.kwargs["client_email_status"],
    }


@pytest.mark.parametrize("answer", NOT_SENT)
async def test_an_invoice_the_endpoint_did_not_send_is_not_recorded_as_sent(
    service: InvoiceAutomationService, answer: dict[str, Any]
) -> None:
    mail = _Mail(client_answer=answer, accounting_answer=SENT_ANSWER)

    seen = await _trigger(service, mail)

    assert seen["result"]["success"] is True  # the invoice exists; its mail did not go
    assert seen["result"]["email_attempted"] is True
    assert seen["result"]["email_sent"] is False
    assert seen["audit"] == ["failed"]
    assert seen["paged"] == ["invoice_client"]
    assert seen["timeline"] == 0
    assert seen["told_accounting"] == FAILED
    assert seen["result"]["accounting_notified"] is True
    assert mail.sent_to == ["client", "accounting"]


@pytest.mark.parametrize("answer", NOT_SENT)
async def test_an_accounting_notice_the_endpoint_did_not_send_is_not_recorded_as_sent(
    service: InvoiceAutomationService, answer: dict[str, Any]
) -> None:
    mail = _Mail(client_answer=SENT_ANSWER, accounting_answer=answer)

    seen = await _trigger(service, mail)

    assert seen["result"]["success"] is True
    assert seen["result"]["email_sent"] is True
    assert seen["result"]["accounting_notified"] is False
    assert seen["audit"] == ["sent"]
    assert seen["paged"] == []
    assert seen["timeline"] == 1


async def test_an_invoice_the_endpoint_sent_is_recorded_as_sent(
    service: InvoiceAutomationService,
) -> None:
    mail = _Mail(client_answer=SENT_ANSWER, accounting_answer=SENT_ANSWER)

    seen = await _trigger(service, mail)

    assert seen["result"]["email_sent"] is True
    assert seen["result"]["accounting_notified"] is True
    assert seen["audit"] == ["sent"]
    assert seen["paged"] == []
    assert seen["timeline"] == 1
    assert seen["told_accounting"] == SENT


@pytest.mark.parametrize("answer", NOT_SENT)
async def test_each_sender_raises_on_an_answer_that_says_not_sent(
    service: InvoiceAutomationService, answer: dict[str, Any]
) -> None:
    mail = _Mail(client_answer=answer, accounting_answer=answer)
    with patch.object(invoice_module, "get_email_client", AsyncMock(return_value=mail.http)):
        with pytest.raises(EmailNotDelivered) as client_mail:
            await service._send_invoice_email_to_client(
                client_email="client@example.test",
                client_name="Zz Client",
                invoice_number="INV-2026-001",
                pdf_bytes=b"%PDF",
                filename="Invoice_INV-2026-001.pdf",
                amount=5_000_000,
                triggered_by="staff@example.test",
            )
        with pytest.raises(EmailNotDelivered):
            await service._send_accounting_notification(
                client_data={"id": 100, "full_name": "Zz Client", "email": "client@example.test"},
                practice_data={"id": 1},
                invoice_number="INV-2026-001",
                pdf_bytes=b"%PDF",
                filename="Invoice_INV-2026-001.pdf",
                entity=ENTITIES[DEFAULT_ENTITY_KEY],
                amount=5_000_000,
            )

    assert client_mail.value.delivery_uncertain is (answer is UNCERTAIN)
