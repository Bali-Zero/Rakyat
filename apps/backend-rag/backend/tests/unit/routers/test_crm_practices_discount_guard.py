"""DAI-177: an invoice never goes out with a total of 0.

A discount needs a Quoted Price to apply to. The API refuses a discount without a price
and a discount at or above the price, on create and on every PATCH that changes either
value. A row already holding such a discount cannot enter Sending Invoice. The invoice
step refuses it too instead of capping it silently.

Database, email and Drive are mocked; every value is synthetic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.routers import crm_practices
from backend.services.crm.practice_state_machine import DISCOUNT_COVERS_PRICE_DETAIL
from backend.services.invoicing.entities import ENTITIES
from backend.services.invoicing.invoice_service import (
    InvoiceAutomationService,
    InvoiceIssuerDecision,
    InvoiceIssuerPersistenceError,
)
from backend.tests.services.invoicing.test_invoice_pph_k127 import _issue_invoice
from backend.tests.unit.routers.test_crm_practices_pph import (
    TAX_LEAD,
    _client,
    _inserted_row,
    _old_row,
    _patch,
    _post,
    _type_row,
    _updated_row,
)

ISSUER = ENTITIES["permata_bali_nol_konsultan"]
ROUTED_AT = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)
DISCOUNT = Decimal("1470000")


def _ran(conn: MagicMock, statement: str) -> bool:
    return any(statement in call.args[0] for call in conn.fetchrow.await_args_list)


def _patch_row(conn: MagicMock, body: dict[str, Any], **row: Any):
    conn.fetchrow = AsyncMock(side_effect=[_old_row(TAX_LEAD["email"], **row), _updated_row()])
    return body


# --- create -----------------------------------------------------------------------


def test_create_refuses_a_discount_without_any_price(mock_db_pool, mock_db_conn):
    mock_db_conn.fetchrow = AsyncMock(
        side_effect=[{**_type_row(), "base_price": None}, _inserted_row()]
    )
    response = _post(
        mock_db_pool,
        TAX_LEAD,
        {
            "client_id": 42,
            "practice_type_code": "tax_monthly",
            "discount_amount": 100000,
            "assigned_to": TAX_LEAD["email"],
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Fill Quoted Price before adding a discount"
    assert not _ran(mock_db_conn, "INSERT INTO practices")


def test_create_keeps_a_discount_on_the_service_base_price(mock_db_pool, mock_db_conn):
    mock_db_conn.fetchrow = AsyncMock(side_effect=[_type_row(), _inserted_row()])
    response = _post(
        mock_db_pool,
        TAX_LEAD,
        {
            "client_id": 42,
            "practice_type_code": "tax_monthly",
            "discount_amount": 100000,
            "assigned_to": TAX_LEAD["email"],
        },
    )
    assert response.status_code < 300, response.text
    assert _ran(mock_db_conn, "INSERT INTO practices")


def test_create_refuses_a_discount_equal_to_the_price(mock_db_pool, mock_db_conn):
    mock_db_conn.fetchrow = AsyncMock(side_effect=[_type_row(), _inserted_row()])
    response = _post(
        mock_db_pool,
        TAX_LEAD,
        {
            "client_id": 42,
            "practice_type_code": "tax_monthly",
            "quoted_price": 1470000,
            "discount_amount": 1470000,
            "assigned_to": TAX_LEAD["email"],
        },
    )
    assert response.status_code == 400, response.text
    assert not _ran(mock_db_conn, "INSERT INTO practices")


# --- PATCH ------------------------------------------------------------------------


def test_patch_refuses_a_price_lowered_below_the_stored_discount(mock_db_pool, mock_db_conn):
    body = _patch_row(mock_db_conn, {"quoted_price": 1000000}, discount_amount=DISCOUNT)
    response = _patch(mock_db_pool, TAX_LEAD, body)
    assert response.status_code == 400, response.text
    assert not _ran(mock_db_conn, "UPDATE practices")


def test_patch_accepts_a_price_above_the_stored_discount(mock_db_pool, mock_db_conn):
    body = _patch_row(mock_db_conn, {"quoted_price": 2000000}, discount_amount=DISCOUNT)
    response = _patch(mock_db_pool, TAX_LEAD, body)
    assert response.status_code == 200, response.text
    assert _ran(mock_db_conn, "UPDATE practices")


def test_patch_refuses_a_discount_on_a_practice_without_a_price(mock_db_pool, mock_db_conn):
    body = _patch_row(mock_db_conn, {"discount_amount": 100000}, quoted_price=None)
    response = _patch(mock_db_pool, TAX_LEAD, body)
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Fill Quoted Price before adding a discount"
    assert not _ran(mock_db_conn, "UPDATE practices")


def test_patch_can_zero_a_discount_on_a_practice_without_a_price(mock_db_pool, mock_db_conn):
    body = _patch_row(
        mock_db_conn, {"discount_amount": 0}, quoted_price=None, discount_amount=DISCOUNT
    )
    response = _patch(mock_db_pool, TAX_LEAD, body)
    assert response.status_code == 200, response.text


def test_patch_refuses_a_discount_equal_to_the_price(mock_db_pool, mock_db_conn):
    body = _patch_row(
        mock_db_conn,
        {"quoted_price": 1470000, "discount_amount": 1470000},
        quoted_price=None,
    )
    response = _patch(mock_db_pool, TAX_LEAD, body)
    assert response.status_code == 400, response.text
    assert not _ran(mock_db_conn, "UPDATE practices")


def test_patch_refuses_a_price_lowered_onto_the_stored_discount(mock_db_pool, mock_db_conn):
    body = _patch_row(mock_db_conn, {"quoted_price": 1470000}, discount_amount=DISCOUNT)
    response = _patch(mock_db_pool, TAX_LEAD, body)
    assert response.status_code == 400, response.text
    assert not _ran(mock_db_conn, "UPDATE practices")


def test_a_stored_full_discount_cannot_enter_sending_invoice(mock_db_pool, mock_db_conn):
    body = _patch_row(
        mock_db_conn,
        {"status": "sending_invoice"},
        status="waiting_documents",
        quoted_price=DISCOUNT,
        discount_amount=DISCOUNT,
    )
    response = _patch(mock_db_pool, TAX_LEAD, body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == DISCOUNT_COVERS_PRICE_DETAIL
    assert not _ran(mock_db_conn, "UPDATE practices")


def test_a_full_discount_committed_concurrently_cannot_slip_in(mock_db_pool, mock_db_conn):
    mock_db_conn.fetchrow = AsyncMock(
        side_effect=[
            _old_row(TAX_LEAD["email"], status="waiting_documents", quoted_price=DISCOUNT),
            {"status": "waiting_documents", "quoted_price": DISCOUNT, "discount_amount": DISCOUNT},
            _updated_row(),
        ]
    )
    response = _patch(mock_db_pool, TAX_LEAD, {"status": "sending_invoice"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == DISCOUNT_COVERS_PRICE_DETAIL
    assert not _ran(mock_db_conn, "UPDATE practices")


def test_a_stored_full_discount_can_be_fixed_in_the_same_move(mock_db_pool, mock_db_conn):
    mock_db_conn.fetchrow = AsyncMock(
        side_effect=[
            _old_row(
                TAX_LEAD["email"],
                status="waiting_documents",
                quoted_price=DISCOUNT,
                discount_amount=DISCOUNT,
            ),
            {"status": "waiting_documents", "quoted_price": DISCOUNT, "discount_amount": DISCOUNT},
            _updated_row(status="sending_invoice"),
        ]
    )
    with patch.object(crm_practices, "InvoiceAutomationService", return_value=MagicMock()):
        response = _patch(
            mock_db_pool, TAX_LEAD, {"status": "sending_invoice", "discount_amount": 100000}
        )
    assert response.status_code == 200, response.text
    assert _ran(mock_db_conn, "UPDATE practices")


def test_patch_unrelated_to_price_is_not_gated(mock_db_pool, mock_db_conn):
    body = _patch_row(
        mock_db_conn,
        {"notes": "synthetic note"},
        quoted_price=Decimal("1000000"),
        discount_amount=DISCOUNT,
    )
    response = _patch(mock_db_pool, TAX_LEAD, body)
    assert response.status_code == 200, response.text


# --- invoice ----------------------------------------------------------------------


def _service(quoted: int, discount: int | None) -> InvoiceAutomationService:
    with patch("backend.services.invoicing.invoice_service.ServiceAccountDriveService"):
        service = InvoiceAutomationService(MagicMock())
    service.invoice_generator = MagicMock()
    service._fetch_practice_data = AsyncMock(
        return_value={
            "id": 7,
            "client_id": 42,
            "quoted_price": Decimal(quoted),
            "discount_amount": Decimal(discount) if discount is not None else None,
        }
    )
    service._fetch_client_data = AsyncMock(return_value={"id": 42, "full_name": "Synthetic"})
    service._fetch_bill_to = AsyncMock(return_value={})
    service._resolve_issuer = AsyncMock()
    service._send_invoice_email_to_client = AsyncMock()
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quoted", "discount", "code"),
    [
        (1470000, 1470000, "invoice_total_zero"),
        (1470000, 1500000, "invoice_discount_exceeds_price"),
        (0, None, "invoice_total_zero"),
    ],
)
async def test_invoice_is_refused_before_anything_is_issued(quoted, discount, code):
    service = _service(quoted, discount)
    result = await service.trigger_on_sending_invoice(7, "agent@example.test")

    assert result["success"] is False
    assert result["error_code"] == code
    service._resolve_issuer.assert_not_awaited()
    service.invoice_generator.generate.assert_not_called()
    service._send_invoice_email_to_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_valid_discount_still_invoices_the_difference():
    issued = await _issue_invoice(quoted=1470000, discount=100000)
    assert issued["amount_idr"] == 1370000
    assert issued["discount_amount_idr"] == 100000


@pytest.mark.asyncio
async def test_persist_never_records_a_capped_discount():
    """The row changed after the check: the mismatch is loud, nothing is written."""
    conn = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    with patch("backend.services.invoicing.invoice_service.ServiceAccountDriveService"):
        service = InvoiceAutomationService(pool)
    conn.fetchrow = AsyncMock(
        return_value={
            "client_id": 42,
            "quoted_price": Decimal("1000000"),
            "discount_amount": DISCOUNT,
            "metadata": None,
        }
    )
    with pytest.raises(InvoiceIssuerPersistenceError) as caught:
        await service._persist_invoice(
            7,
            {"invoice_number": "INV-SYNTHETIC"},
            "agent@example.test",
            issuer_decision=InvoiceIssuerDecision(
                entity=ISSUER, routed_at=ROUTED_AT, lookup_at=ROUTED_AT, source="new"
            ),
            invoice_attempt_id="attempt",
        )
    assert caught.value.reason == "invoice_amount_changed"
    assert conn.fetchrow.await_count == 1


@pytest.mark.parametrize("code", ["invoice_total_zero", "invoice_discount_exceeds_price"])
def test_regenerate_answers_422_for_a_refused_amount(mock_db_pool, mock_db_conn, code):
    mock_db_conn.fetchrow = AsyncMock(return_value={"status": "sending_invoice"})
    service = MagicMock()
    service.regenerate_invoice = AsyncMock(
        return_value={"success": False, "error": f"{code}: synthetic", "error_code": code}
    )
    with patch.object(crm_practices, "InvoiceAutomationService", return_value=service):
        response = _client(mock_db_pool, TAX_LEAD).post("/api/crm/practices/7/regenerate-invoice")
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith(code)
