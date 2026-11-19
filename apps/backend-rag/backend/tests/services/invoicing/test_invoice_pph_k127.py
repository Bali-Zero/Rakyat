"""K127 — PPh withholding on the invoice, end to end with the real ReportLab generator.

Guard: a Tax Lead sets PPh 23 2% on a 2,500,000 process through the CRM API; the
invoice then prints the tax base, the withheld PPh and TOTAL DUE 2,450,000, and
``invoices.amount_idr`` is 2,450,000 (what the client actually transfers, so the
unpaid notifier's ``amount_idr - paid_amount_idr`` is 0 once they pay).

Innocence: a process without PPh issues the same invoice and amount as before.
Email, Drive and the database are mocked; every name and address is synthetic.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pypdf import PdfReader

from backend.services.invoicing.entities import ENTITIES
from backend.services.invoicing.invoice_generator import _REPORTLAB_AVAILABLE
from backend.services.invoicing.invoice_service import (
    _INVOICE_UPSERT_SQL,
    InvoiceAutomationService,
    InvoiceIssuerDecision,
)

ISSUER = ENTITIES["permata_bali_nol_konsultan"]
ROUTED_AT = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)
TAX_LEAD = {"id": "u-tax", "email": "tax.lead@example.test", "role": "tax lead"}


def _pdf_text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() for page in reader.pages)


async def _issue_invoice(
    *, quoted: int, discount: int | None = None, metadata: Any = None
) -> dict[str, Any]:
    """Run the real trigger_on_sending_invoice; returns the PDF text and what was persisted."""
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
    }
    if metadata is not None:
        practice["metadata"] = metadata
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
    upsert = next(c.args for c in conn.fetchrow.await_args_list if c.args[0] is _INVOICE_UPSERT_SQL)
    documents = conn.execute.await_args_list[0].args[1]
    return {
        "text": _pdf_text(drive.upload_file_to_folder.await_args.kwargs["file_content"]),
        "amount_idr": upsert[5],
        "discount_amount_idr": upsert[6],
        "client_email_amount": service._send_invoice_email_to_client.await_args.kwargs["amount"],
        "accounting_amount": service._send_accounting_notification.await_args.kwargs["amount"],
        "invoice_doc": documents["invoice"],
    }


def _metadata_from_tax_lead_patch(body: dict[str, Any]) -> dict[str, Any]:
    """Send the PATCH a Tax Lead's form would send; return the metadata the API writes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.dependencies import get_current_user, get_database_pool
    from backend.app.routers import crm_practices

    conn = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=ctx)
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "status": "on_process",
                "client_id": 42,
                "client_visible": True,
                "created_by": TAX_LEAD["email"],
                "assigned_to": TAX_LEAD["email"],
                "discount_amount": None,
                "metadata": {},
                "quoted_price": Decimal("2500000"),
                "client_lead": TAX_LEAD["email"],
                "practice_type_code": "tax_monthly",
            },
            {"id": 7, "status": "on_process", "client_id": 42, "practice_type_id": 10},
        ]
    )
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)

    app = FastAPI()
    app.include_router(crm_practices.router)
    app.dependency_overrides[get_current_user] = lambda: TAX_LEAD
    app.dependency_overrides[get_database_pool] = lambda: pool
    with patch.object(crm_practices, "invalidate_cache", new=AsyncMock()):
        response = TestClient(app).patch("/api/crm/practices/7", json=body)
    assert response.status_code == 200, response.text

    for call in conn.fetchrow.await_args_list:
        query = call.args[0]
        if "UPDATE practices" in query:
            for i, value in enumerate(call.args[1:], start=1):
                if f"metadata = ${i}::jsonb" in query:
                    return value
    raise AssertionError("the PATCH did not write practices.metadata")


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_tax_lead_pph23_2_percent_on_2_5_million():
    metadata = _metadata_from_tax_lead_patch({"pph": {"type": "PPH23", "rate_percent": 2}})
    issued = await _issue_invoice(quoted=2_500_000, metadata=metadata)
    text = issued["text"]

    assert "Subtotal before tax\n IDR 2,500,000" in text
    assert "Tax PPh 23 2%\n − IDR 50,000" in text
    assert "TOTAL DUE\n IDR 2,450,000" in text
    assert "Amount in words: Two million four hundred fifty thousand Rupiah" in text
    assert issued["amount_idr"] == 2_450_000
    assert issued["client_email_amount"] == issued["accounting_amount"] == 2_450_000
    assert issued["invoice_doc"]["pph"] == {
        "dpp": 2_500_000,
        "pph_type": "PPH23",
        "pph_rate": 2.0,
        "pph_amount": 50_000,
    }


@pytest.mark.asyncio
async def test_discount_and_pph_use_the_post_discount_base():
    metadata = {"pph": {"type": "PPH23", "rate_percent": 2}}
    issued = await _issue_invoice(quoted=3_000_000, discount=500_000, metadata=metadata)
    text = issued["text"]

    assert "Subtotal\n IDR 3,000,000\nDiscount\n − IDR 500,000" in text
    assert "Subtotal before tax\n IDR 2,500,000\nTax PPh 23 2%\n − IDR 50,000" in text
    assert "60,000" not in text, "PPh must not be computed on the pre-discount price"
    assert "TOTAL DUE\n IDR 2,450,000" in text
    assert issued["amount_idr"] == 2_450_000
    assert issued["discount_amount_idr"] == 500_000


@pytest.mark.asyncio
async def test_pph_rounds_half_up_to_the_rupiah():
    # 1,234,567 x 2% = 24,691.34 -> 24,691 ; 1,000,025 x 2% = 20,000.5 -> 20,001
    issued = await _issue_invoice(
        quoted=1_234_567, metadata={"pph": {"type": "PPH23", "rate_percent": 2}}
    )
    assert "Tax PPh 23 2%\n − IDR 24,691" in issued["text"]
    assert "TOTAL DUE\n IDR 1,209,876" in issued["text"]
    assert issued["amount_idr"] == 1_209_876

    issued = await _issue_invoice(
        quoted=1_000_025, metadata={"pph": {"type": "PPH23", "rate_percent": 2}}
    )
    assert "Tax PPh 23 2%\n − IDR 20,001" in issued["text"]
    assert issued["amount_idr"] == 980_024


@pytest.mark.asyncio
async def test_other_types_and_decimal_rates_label_the_row():
    issued = await _issue_invoice(
        quoted=2_000_000, metadata={"pph": {"type": "PPH21", "rate_percent": 2.5}}
    )
    assert "Tax PPh 21 2.5%" in issued["text"]
    assert issued["amount_idr"] == 1_950_000

    issued = await _issue_invoice(
        quoted=2_000_000, metadata={"pph": {"type": "PPH4_2", "rate_percent": 10}}
    )
    assert "Tax PPh 4(2) 10%" in issued["text"]
    assert issued["amount_idr"] == 1_800_000


# ---------------------------------------------------------------------------
# Innocence (green on main and on this branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, {}, {"discount_log": []}])
async def test_no_pph_issues_the_same_invoice_as_before(metadata):
    issued = await _issue_invoice(quoted=2_500_000, metadata=metadata)
    text = issued["text"]

    for absent in ("PPh", "Tax", "before tax", "Subtotal", "Discount"):
        assert absent not in text, absent
    assert "TOTAL DUE\n IDR 2,500,000" in text
    assert "Amount in words: Two million five hundred thousand Rupiah" in text
    assert issued["amount_idr"] == 2_500_000
    assert issued["client_email_amount"] == 2_500_000
    assert "pph" not in issued["invoice_doc"]


@pytest.mark.asyncio
async def test_discount_without_pph_is_unchanged():
    issued = await _issue_invoice(quoted=3_000_000, discount=500_000)
    text = issued["text"]

    assert "Subtotal\n IDR 3,000,000\nDiscount\n − IDR 500,000" in text
    assert "before tax" not in text
    assert "PPh" not in text
    assert "TOTAL DUE\n IDR 2,500,000" in text
    assert issued["amount_idr"] == 2_500_000
    assert issued["discount_amount_idr"] == 500_000
