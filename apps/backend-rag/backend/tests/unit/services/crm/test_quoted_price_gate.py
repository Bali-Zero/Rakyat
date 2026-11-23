"""Quoted Price gate for Sending Invoice (owner decision 2026-11-13), service layer.

Spec rows (the router rows live in db/test_quoted_price_gate_pg.py, on real Postgres):

Q1  The gate: entering sending_invoice (legacy names included) needs a Quoted Price > 0; None, 0,
    0.0, "0", "", junk, a bool and a negative number all fail; any other status passes
    -> test_gate_truth_table, test_legacy_names_are_gated, test_other_states_never_need_a_price
Q2  Only the QUOTED price counts: the total after a discount is not looked at
    -> test_discount_to_zero_total_is_not_the_gates_business
Q3  EnhancedCRMService.update_practice_status refuses to enter sending_invoice without a price,
    allows it with one, and never gates a practice already there or another target
    -> test_service_update_*
Q4  EnhancedCRMService.create_practice refuses a practice created in sending_invoice without a price
    -> test_service_create_*
Q5  CRMQueryOptimizer.batch_update_practices: the batch's own quoted_price wins over the row's
    -> test_batch_update_*
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.crm.cache_query import CRMQueryOptimizer
from backend.services.crm.client_core import EnhancedCRMService
from backend.services.crm.practice_state_machine import (
    QUOTED_PRICE_REQUIRED_DETAIL,
    QuotedPriceRequiredError,
    has_quoted_price,
    validate_quoted_price_for_invoice,
)

# --- Q1 / Q2 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "ok"),
    [
        (None, False),
        (0, False),
        (0.0, False),
        ("0", False),
        ("0.00", False),
        (Decimal("0"), False),
        ("", False),
        ("abc", False),
        (True, False),
        (-1, False),
        (Decimal("-5"), False),
        (1, True),
        (0.01, True),
        ("1.5", True),
        (Decimal("2500000"), True),
    ],
)
def test_gate_truth_table(price, ok) -> None:
    assert has_quoted_price(price) is ok
    if ok:
        assert validate_quoted_price_for_invoice("sending_invoice", price) is True
    else:
        with pytest.raises(QuotedPriceRequiredError, match=QUOTED_PRICE_REQUIRED_DETAIL):
            validate_quoted_price_for_invoice("sending_invoice", price)


@pytest.mark.parametrize("legacy", ["quotation_sent", "payment_pending", "waiting_payment"])
def test_legacy_names_are_gated(legacy: str) -> None:
    with pytest.raises(QuotedPriceRequiredError):
        validate_quoted_price_for_invoice(legacy, None)
    assert validate_quoted_price_for_invoice(legacy, 10) is True


@pytest.mark.parametrize(
    "state", ["inquiry", "waiting_documents", "on_process", "completed", "cancelled", None, ""]
)
def test_other_states_never_need_a_price(state) -> None:
    assert validate_quoted_price_for_invoice(state, None) is True
    assert validate_quoted_price_for_invoice(state, 0) is True


def test_discount_to_zero_total_is_not_the_gates_business() -> None:
    # quoted 100, discount 100 -> total 0: the gate looks at the quoted price only.
    assert validate_quoted_price_for_invoice("sending_invoice", 100) is True


# --- Q3 ---------------------------------------------------------------------------------


def _service(row: dict | None, updated: dict | None = None):
    pool = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[row, updated or {"id": 1, "status": "x"}])
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    svc = EnhancedCRMService.__new__(EnhancedCRMService)
    svc.db_pool = pool
    svc.auditor = MagicMock()
    svc.auditor.log_practice_status_change = AsyncMock()
    return svc, conn


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [None, 0, Decimal("0")])
async def test_service_update_refuses_without_a_price_and_writes_nothing(price) -> None:
    svc, conn = _service({"status": "waiting_documents", "client_id": 1, "quoted_price": price})
    with pytest.raises(QuotedPriceRequiredError):
        await svc.update_practice_status(1, "sending_invoice")
    assert conn.fetchrow.await_count == 1  # only the SELECT, never the UPDATE
    svc.auditor.log_practice_status_change.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_update_allows_with_a_price() -> None:
    svc, conn = _service(
        {"status": "waiting_documents", "client_id": 1, "quoted_price": Decimal("1000")},
        {"id": 1, "status": "sending_invoice"},
    )
    result = await svc.update_practice_status(1, "sending_invoice")
    assert result["status"] == "sending_invoice"
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_service_update_does_not_gate_a_practice_already_in_the_status() -> None:
    svc, _conn = _service(
        {"status": "sending_invoice", "client_id": 1, "quoted_price": None},
        {"id": 1, "status": "sending_invoice"},
    )
    assert (await svc.update_practice_status(1, "sending_invoice"))["id"] == 1


@pytest.mark.asyncio
async def test_service_update_does_not_gate_other_targets() -> None:
    svc, _conn = _service(
        {"status": "sending_invoice", "client_id": 1, "quoted_price": None},
        {"id": 1, "status": "waiting_documents"},
    )
    assert (await svc.update_practice_status(1, "waiting_documents"))["status"] == "waiting_documents"


# --- Q4 ---------------------------------------------------------------------------------


def _create_service():
    pool = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 5})
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    svc = EnhancedCRMService.__new__(EnhancedCRMService)
    svc.db_pool = pool
    svc.auditor = MagicMock()
    svc.auditor.log = AsyncMock()
    return svc, conn


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [None, 0])
async def test_service_create_refuses_sending_invoice_without_a_price(price) -> None:
    svc, conn = _create_service()
    with pytest.raises(QuotedPriceRequiredError):
        await svc.create_practice(
            {
                "client_id": 1,
                "practice_type_id": 2,
                "status": "sending_invoice",
                "quoted_price": price,
            }
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_create_innocence_inquiry_and_priced_sending_invoice_pass() -> None:
    for data in (
        {"client_id": 1, "practice_type_id": 2},
        {"client_id": 1, "practice_type_id": 2, "status": "sending_invoice", "quoted_price": 10},
    ):
        svc, conn = _create_service()
        await svc.create_practice(data)
        conn.fetchrow.assert_awaited()


# --- Q5 ---------------------------------------------------------------------------------


def _batch(row: dict | None):
    pool = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    opt = CRMQueryOptimizer.__new__(CRMQueryOptimizer)
    opt.db_pool = pool
    return opt, conn


@pytest.mark.asyncio
async def test_batch_update_refuses_sending_invoice_without_any_price() -> None:
    opt, conn = _batch({"status": "waiting_documents", "quoted_price": None})
    with pytest.raises(QuotedPriceRequiredError):
        await opt.batch_update_practices([{"id": 1, "status": "sending_invoice"}])
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_update_the_batchs_own_price_wins_over_the_row() -> None:
    opt, conn = _batch({"status": "waiting_documents", "quoted_price": Decimal("500")})
    with pytest.raises(QuotedPriceRequiredError):
        await opt.batch_update_practices(
            [{"id": 1, "status": "sending_invoice", "quoted_price": 0}]
        )
    opt, conn = _batch({"status": "waiting_documents", "quoted_price": None})
    assert (
        await opt.batch_update_practices(
            [{"id": 1, "status": "sending_invoice", "quoted_price": 900}]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_batch_update_row_price_is_enough_and_other_statuses_are_not_looked_up() -> None:
    opt, conn = _batch({"status": "waiting_documents", "quoted_price": Decimal("500")})
    assert await opt.batch_update_practices([{"id": 1, "status": "sending_invoice"}]) == 1
    opt, conn = _batch({"status": "waiting_documents", "quoted_price": None})
    assert await opt.batch_update_practices([{"id": 1, "status": "inquiry"}]) == 1
    conn.fetchrow.assert_not_awaited()
