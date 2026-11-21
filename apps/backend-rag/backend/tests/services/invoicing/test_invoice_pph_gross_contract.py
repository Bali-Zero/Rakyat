"""K127 x accounting: an invoice with PPh keeps ``invoices.amount_idr`` gross.

Migration 238 gave ``invoices`` a ``pph_expected_idr`` column so the matcher knows a
B2B client withholds: ``services/accounting/matcher.py`` reads ``amount_idr`` as the
gross and a transfer of ``amount_idr - pph_expected_idr`` as "net of PPh23". The PPh
invoice must therefore persist the DPP in ``amount_idr`` and the PPh in
``pph_expected_idr``, while the PDF still asks the client for DPP - PPh.

Guard (PPh 23 2% on 2,500,000): amount_idr 2,500,000, pph_expected_idr 50,000, PDF
TOTAL DUE 2,450,000; the matcher reads the 2,450,000 transfer as ``net`` (and not the
2,401,000 a second 2% cut would give); the reminder digest and the client portal show
nothing outstanding once 2,450,000 is paid.

Innocence (same result before and after the change): without PPh, amount_idr is
quoted - discount, pph_expected_idr stays 0 and the PDF text is the same.

The invoice row is read from whatever ``INSERT INTO invoices`` the service sends, by
column name, so these tests do not depend on which SQL variant writes it. Email,
Drive and the database are mocked; every name and address is synthetic.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pypdf import PdfReader

from backend.services.accounting.matcher import OpenInvoice, _amount_signals
from backend.services.invoicing.entities import ENTITIES
from backend.services.invoicing.invoice_generator import _REPORTLAB_AVAILABLE
from backend.services.invoicing.invoice_service import (
    InvoiceAutomationService,
    InvoiceIssuerDecision,
)
from backend.services.invoicing.unpaid_invoice_notifier import _build_digest
from backend.services.portal.portal_service import PortalService

ISSUER = ENTITIES["permata_bali_nol_konsultan"]
ROUTED_AT = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)
PPH23_2 = {"pph": {"type": "PPH23", "rate_percent": 2}}


def _pdf_text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() for page in reader.pages)


def _invoice_row_written(conn: AsyncMock) -> dict[str, Any]:
    """Column -> value of the ``INSERT INTO invoices`` the service sent.

    A column the INSERT does not name keeps the table default; for
    ``pph_expected_idr`` that is 0 (migration 238), so a caller reads it with
    ``.get("pph_expected_idr", 0)``.
    """
    for call in conn.fetchrow.await_args_list:
        sql = call.args[0]
        if isinstance(sql, str) and "INSERT INTO invoices" in sql:
            columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
            values = re.search(r"VALUES\s*\(([^)]*)\)", sql).group(1).split(",")
            return {
                column: call.args[int(value.strip().lstrip("$"))]
                for column, value in zip(columns, values, strict=True)
            }
    raise AssertionError("the service did not write the invoices row")


async def _issue(*, quoted: int, discount: int | None = None, metadata: Any = None) -> dict:
    """Run the real trigger_on_sending_invoice; return the PDF text and the invoices row."""
    if not _REPORTLAB_AVAILABLE:
        pytest.fail("reportlab is required for the invoice PPh test")
    conn = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)

    drive = MagicMock()
    drive.upload_file_to_folder = AsyncMock(
        return_value={"id": "drive-id", "webViewLink": "https://drive.example/x"}
    )
    with patch(
        "backend.services.invoicing.invoice_service.ServiceAccountDriveService",
        return_value=drive,
    ):
        service = InvoiceAutomationService(pool)

    practice = {
        "id": 7,
        "client_id": 42,
        "practice_type_code": "tax_monthly",
        "practice_type_name": "Synthetic Monthly Reporting Service",
        "quoted_price": Decimal(quoted),
        "discount_amount": Decimal(discount) if discount is not None else None,
        "discount_reason": None,
        "notes": "Synthetic monthly reporting",
        "client_drive_folder_id": "folder-1",
        "invoice_company_id": None,
        "metadata": metadata,
    }
    service._fetch_practice_data = AsyncMock(return_value=practice)
    service._fetch_client_data = AsyncMock(
        return_value={"id": 42, "full_name": "PT Sintetis Contoh", "email": "billing@example.test"}
    )
    service._fetch_bill_to = AsyncMock(return_value={})
    service._resolve_issuer = AsyncMock(
        return_value=InvoiceIssuerDecision(
            entity=ISSUER, routed_at=ROUTED_AT, lookup_at=ROUTED_AT, source="new"
        )
    )
    service._send_invoice_email_to_client = AsyncMock()
    service._send_accounting_notification = AsyncMock()
    service._record_invoice_sent_on_client_timeline = AsyncMock()

    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "client_id": 42,
                "quoted_price": Decimal(quoted),
                "discount_amount": Decimal(discount) if discount is not None else None,
                "metadata": metadata,
            },
            {"issuer_entity_key": ISSUER.key, "issuer_routed_at": ROUTED_AT},
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with (
        patch(
            "backend.services.invoicing.invoice_service.log_email_attempt",
            AsyncMock(return_value=1),
        ),
        patch("backend.services.invoicing.invoice_service.record_email_result", AsyncMock()),
        patch("backend.services.invoicing.invoice_service.invalidate_crm_stats", AsyncMock()),
    ):
        result = await service.trigger_on_sending_invoice(7, "agent@example.test")

    assert result["success"] is True, result
    return {
        "text": _pdf_text(drive.upload_file_to_folder.await_args.kwargs["file_content"]),
        "row": _invoice_row_written(conn),
    }


def _open_invoice(row: dict[str, Any]) -> OpenInvoice:
    """The invoice as the accounting router hands it to the matcher (accounting.py)."""
    return OpenInvoice(
        invoice_id=1,
        practice_id=row["practice_id"],
        client_name="PT Sintetis Contoh",
        amount_idr=int(row["amount_idr"]),
        pph_expected_idr=int(row.get("pph_expected_idr", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_pph_invoice_stores_gross_amount_and_pph_expected():
    issued = await _issue(quoted=2_500_000, metadata=PPH23_2)

    assert issued["row"]["amount_idr"] == 2_500_000
    assert issued["row"].get("pph_expected_idr", 0) == 50_000
    assert "Subtotal before tax\n IDR 2,500,000" in issued["text"]
    assert "Tax PPh 23 2%\n − IDR 50,000" in issued["text"]
    assert "TOTAL DUE\n IDR 2,450,000" in issued["text"]


@pytest.mark.asyncio
async def test_guard_matcher_reads_the_net_transfer_as_net_of_pph_once():
    issued = await _issue(quoted=2_500_000, metadata=PPH23_2)
    invoice = _open_invoice(issued["row"])

    _, _, _, withheld, relation = _amount_signals(2_450_000, invoice)
    assert (relation, withheld) == ("net", True)

    # 2,450,000 x 0.98: PPh taken off an amount that is already net — not a net match.
    _, _, _, _, relation = _amount_signals(2_401_000, invoice)
    assert relation != "net"


@pytest.mark.asyncio
async def test_guard_reminder_digest_shows_nothing_outstanding_once_net_is_paid():
    issued = await _issue(quoted=2_500_000, metadata=PPH23_2)
    row = issued["row"]
    _, body = _build_digest(
        [
            {
                "invoice_number": row["invoice_number"],
                "service_name": "Synthetic Monthly Reporting Service",
                "amount_idr": row["amount_idr"],
                "pph_expected_idr": row.get("pph_expected_idr", 0),
                "paid_amount_idr": 2_450_000,
                "payment_status": "partial",
                "days_unpaid": 4,
                "assigned_to": "sample.member@example.test",
            }
        ],
        "2026-11-20",
    )
    assert "outstanding Rp 0)" in body


def test_reminder_digest_outstanding_subtracts_pph_expected():
    gross = {
        "invoice_number": "INV-SYN-1",
        "service_name": "Synthetic Monthly Reporting Service",
        "amount_idr": 2_500_000,
        "pph_expected_idr": 50_000,
        "payment_status": "partial",
        "days_unpaid": 4,
        "assigned_to": "sample.member@example.test",
    }
    _, body = _build_digest([{**gross, "paid_amount_idr": 2_450_000}], "2026-11-20")
    assert "outstanding Rp 0)" in body
    _, body = _build_digest([{**gross, "paid_amount_idr": 1_000_000}], "2026-11-20")
    assert "outstanding Rp 1.450.000)" in body


async def _portal_billing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    conn = AsyncMock()
    conn.fetch.return_value = rows
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return await PortalService(pool).get_billing(
        client_id=1, current_user={"client_id": 1, "email": "client@example.test"}
    )


def _portal_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1,
        "invoice_number": "INV-SYN-1",
        "amount_idr": 2_500_000.0,
        "pph_expected_idr": 50_000,
        "invoice_source": "local_pdf",
        "drive_file_id": None,
        "drive_web_link": None,
        "email_sent_to_client": False,
        "generated_at": None,
        "created_at": None,
        "practice_id": 10,
        "practice_name": "Synthetic Monthly Reporting Service",
        "practice_category": "tax",
        "paid_amount_idr": 2_450_000,
        "payment_status": "paid",
        "paid_amount": 2_450_000,
        "quoted_price": 2_500_000.0,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_guard_portal_bills_the_client_the_total_due_not_the_gross():
    result = await _portal_billing([_portal_row()])
    assert result["invoices"][0]["amount_idr"] == 2_450_000.0
    assert result["summary"]["total_invoiced"] == 2_450_000.0
    assert result["summary"]["total_paid"] == 2_450_000.0
    assert result["summary"]["total_pending"] == 0.0


# ---------------------------------------------------------------------------
# Innocence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("discount", [None, 500_000])
async def test_no_pph_keeps_amount_pph_expected_and_pdf_text(discount):
    never = await _issue(quoted=3_000_000, discount=discount, metadata=None)
    expected_amount = 3_000_000 - (discount or 0)

    assert never["row"]["amount_idr"] == expected_amount
    assert never["row"].get("pph_expected_idr", 0) == 0
    assert never["row"]["discount_amount_idr"] == (discount or 0)
    assert "PPh" not in never["text"]
    assert "before tax" not in never["text"]
    assert f"TOTAL DUE\n IDR {expected_amount:,}" in never["text"]

    # A process whose PPh was cleared again issues the same invoice as one that never had it.
    cleared = await _issue(
        quoted=3_000_000,
        discount=discount,
        metadata={"pph_log": [{"previous": PPH23_2["pph"], "value": None, "event": "clear"}]},
    )
    assert cleared["row"]["amount_idr"] == expected_amount
    assert cleared["row"].get("pph_expected_idr", 0) == 0
    assert cleared["text"] == never["text"]


@pytest.mark.asyncio
async def test_no_pph_portal_and_digest_are_unchanged():
    result = await _portal_billing(
        [_portal_row(pph_expected_idr=0, paid_amount=1_000_000, payment_status="partial")]
    )
    assert result["invoices"][0]["amount_idr"] == 2_500_000.0
    assert result["summary"]["total_pending"] == 1_500_000.0

    row = {
        "invoice_number": "INV-SYN-2",
        "service_name": "Synthetic Monthly Reporting Service",
        "amount_idr": 2_500_000,
        "paid_amount_idr": 1_000_000,
        "payment_status": "partial",
        "days_unpaid": 4,
        "assigned_to": "sample.member@example.test",
    }
    _, body = _build_digest([row], "2026-11-20")
    assert "outstanding Rp 1.500.000)" in body
