"""Quoted Price gate for Sending Invoice through the REAL router (production-shaped schema).

Owner decision 2026-11-13: a practice cannot move to Sending Invoice without a Quoted Price
(null or 0). Spec rows (service-layer rows Q1-Q5 are in unit/services/crm/test_quoted_price_gate.py):

R1  PATCH to sending_invoice on a practice with a NULL quoted price -> 422, exact detail, the
    status does not change -> test_patch_without_a_price_is_refused_and_status_unchanged
R2  Same with quoted price 0 -> test_patch_with_a_zero_price_is_refused
R3  A PATCH that sets quoted_price and status together passes (the EFFECTIVE price) -> test_patch_setting_price_and_status_together_passes; setting price 0 together does not
    -> test_patch_setting_a_zero_price_together_is_refused
R4  A practice with a price passes (innocence) -> test_patch_with_a_price_passes
R5  Choosing the service in the same PATCH back-fills base_price, which counts
    -> test_patch_service_backfill_counts_as_the_price
R6  Only ENTERING is gated: a practice already in sending_invoice keeps accepting other edits,
    other transitions are untouched -> test_patch_unrelated_edits_on_a_priceless_sending_invoice_practice_pass, test_other_transitions_need_no_price
R7  Creating a practice straight in sending_invoice follows the same rule
    -> test_create_in_sending_invoice_without_a_price_is_refused, test_create_in_sending_invoice_with_a_price_passes
R8  A discount that makes the total 0 does not make the price missing
    -> test_a_discount_to_zero_total_still_passes
R10 An explicit `quoted_price: null` sent with the status gets the gate's 422, not the generic
    "cannot be null" 400 -> test_patch_with_an_explicit_null_price_gets_the_gate_refusal
R11 A concurrent write of price 0 committed between the early read and the UPDATE cannot slip
    the move through (the row is locked and re-checked in the transaction)
    -> test_a_concurrent_zero_price_write_cannot_slip_the_move_through
R12 PATCH A reads sending_invoice, then B commits waiting_documents + price 0: A re-entering
    sending_invoice gets the 422 (decided on the LOCKED status and price)
    -> test_a_concurrent_move_out_with_a_zero_price_cannot_be_undone_by_a_stale_status
R12b Same race, price kept: A passes and lands in sending_invoice
    -> test_a_concurrent_move_out_with_the_price_kept_lets_the_reentry_pass
R12c A practice that stays in sending_invoice keeps accepting unrelated edits (no gate)
    -> test_a_practice_that_stays_in_sending_invoice_is_never_gated
R9  A refused PATCH writes nothing else (price, notes sent in the same PATCH stay unchanged)
    -> test_patch_without_a_price_is_refused_and_status_unchanged

Requires the mandate-owned disposable PostgreSQL; skipped otherwise. Synthetic data only.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.app.routers import crm_practices
from backend.app.routers.crm_practices import PracticeCreate, PracticeUpdate
from backend.services.crm.practice_state_machine import QUOTED_PRICE_REQUIRED_DETAIL
from backend.tests.db.test_invoice_company_production_shape_pg import (  # noqa: F401
    USER,
    client,
    create,
    pool,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def invoice_automation_double(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A move into sending_invoice that passes the gate spawns the invoice automation (Drive,
    mail). The gate is what is under test, so only that background step is doubled; it also
    records whether the automation was triggered at all."""
    service = MagicMock()
    service.trigger_on_sending_invoice = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(crm_practices, "InvoiceAutomationService", lambda _pool: service)
    return service


async def priceless(pool: asyncpg.Pool, person: int, status: str = "waiting_documents") -> int:
    """A practice of a service with no base price, so it has no quoted price (like prod rows)."""
    await pool.execute(
        "INSERT INTO practice_types (code, name, category, base_price)"
        " VALUES ('no_price_type', 'No price type', 'misc', NULL) ON CONFLICT (code) DO NOTHING"
    )
    created = await create(pool, person, code="no_price_type")
    await pool.execute("UPDATE practices SET status = $1 WHERE id = $2", status, created.id)
    assert await pool.fetchval("SELECT quoted_price FROM practices WHERE id=$1", created.id) is None
    return created.id


async def patch(pool: asyncpg.Pool, practice_id: int, **fields):
    return await crm_practices.update_practice(
        request=None,
        practice_id=practice_id,
        updates=PracticeUpdate(**fields),
        db_pool=pool,
        current_user=USER,
    )


async def status_of(pool: asyncpg.Pool, practice_id: int) -> str:
    return await pool.fetchval("SELECT status FROM practices WHERE id=$1", practice_id)


@pytest.mark.asyncio
async def test_patch_without_a_price_is_refused_and_status_unchanged(
    pool: asyncpg.Pool, invoice_automation_double: MagicMock
) -> None:
    practice = await priceless(pool, await client(pool))
    with pytest.raises(HTTPException) as caught:
        await patch(pool, practice, status="sending_invoice", notes="should not be written")
    assert caught.value.status_code == 422
    assert caught.value.detail == QUOTED_PRICE_REQUIRED_DETAIL
    assert await status_of(pool, practice) == "waiting_documents"
    assert await pool.fetchval("SELECT notes FROM practices WHERE id=$1", practice) is None
    invoice_automation_double.trigger_on_sending_invoice.assert_not_called()


@pytest.mark.asyncio
async def test_patch_with_a_zero_price_is_refused(pool: asyncpg.Pool) -> None:
    practice = await priceless(pool, await client(pool))
    await pool.execute("UPDATE practices SET quoted_price = 0 WHERE id = $1", practice)
    with pytest.raises(HTTPException) as caught:
        await patch(pool, practice, status="sending_invoice")
    assert (caught.value.status_code, caught.value.detail) == (422, QUOTED_PRICE_REQUIRED_DETAIL)
    assert await status_of(pool, practice) == "waiting_documents"


@pytest.mark.asyncio
async def test_patch_setting_price_and_status_together_passes(pool: asyncpg.Pool) -> None:
    practice = await priceless(pool, await client(pool))
    out = await patch(pool, practice, status="sending_invoice", quoted_price=Decimal("750000"))
    assert out["status"] == "sending_invoice"
    assert await pool.fetchval("SELECT quoted_price FROM practices WHERE id=$1", practice) == 750000


@pytest.mark.asyncio
async def test_patch_setting_a_zero_price_together_is_refused(pool: asyncpg.Pool) -> None:
    practice = await priceless(pool, await client(pool))
    await pool.execute("UPDATE practices SET quoted_price = 500 WHERE id = $1", practice)
    with pytest.raises(HTTPException) as caught:
        # The row has a price, but this PATCH writes 0: the EFFECTIVE price is what counts.
        await patch(pool, practice, status="sending_invoice", quoted_price=Decimal("0"))
    assert caught.value.status_code == 422
    assert await status_of(pool, practice) == "waiting_documents"


@pytest.mark.asyncio
async def test_patch_with_a_price_passes(pool: asyncpg.Pool) -> None:
    created = await create(pool, await client(pool))  # priced from the service's base price
    await pool.execute("UPDATE practices SET status = 'waiting_documents' WHERE id=$1", created.id)
    out = await patch(pool, created.id, status="sending_invoice")
    assert out["status"] == "sending_invoice"


@pytest.mark.asyncio
async def test_patch_service_backfill_counts_as_the_price(pool: asyncpg.Pool) -> None:
    practice = await priceless(pool, await client(pool))
    out = await patch(
        pool, practice, status="sending_invoice", practice_type_code="tax_annual_company"
    )
    assert out["status"] == "sending_invoice"
    assert (
        await pool.fetchval("SELECT quoted_price FROM practices WHERE id=$1", practice) == 1000000
    )


@pytest.mark.asyncio
async def test_patch_unrelated_edits_on_a_priceless_sending_invoice_practice_pass(
    pool: asyncpg.Pool,
) -> None:
    practice = await priceless(pool, await client(pool), status="sending_invoice")
    out = await patch(pool, practice, notes="legacy row, unrelated edit")
    assert out["notes"] == "legacy row, unrelated edit"
    assert await status_of(pool, practice) == "sending_invoice"
    # Re-sending the same status is not an entry either.
    await patch(pool, practice, status="sending_invoice", priority="high")


@pytest.mark.asyncio
async def test_other_transitions_need_no_price(pool: asyncpg.Pool) -> None:
    practice = await priceless(pool, await client(pool), status="inquiry")
    assert (await patch(pool, practice, status="waiting_documents"))[
        "status"
    ] == "waiting_documents"
    assert (await patch(pool, practice, status="cancelled"))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_discount_to_zero_total_still_passes(pool: asyncpg.Pool) -> None:
    practice = await priceless(pool, await client(pool))
    out = await patch(
        pool,
        practice,
        status="sending_invoice",
        quoted_price=Decimal("100"),
        discount_amount=Decimal("100"),
        discount_reason="synthetic full discount",
    )
    assert out["status"] == "sending_invoice"


@pytest.mark.asyncio
async def test_create_in_sending_invoice_without_a_price_is_refused(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    await pool.execute(
        "INSERT INTO practice_types (code, name, category, base_price)"
        " VALUES ('no_price_type', 'No price type', 'misc', NULL)"
    )
    with pytest.raises(HTTPException) as caught:
        await crm_practices.create_practice(
            request=None,
            practice=PracticeCreate(
                client_id=person, practice_type_code="no_price_type", status="sending_invoice"
            ),
            background_tasks=BackgroundTasks(),
            db_pool=pool,
            current_user=USER,
        )
    assert (caught.value.status_code, caught.value.detail) == (422, QUOTED_PRICE_REQUIRED_DETAIL)
    assert await pool.fetchval("SELECT count(*) FROM practices") == 0


@pytest.mark.asyncio
async def test_create_in_sending_invoice_with_a_price_passes(pool: asyncpg.Pool) -> None:
    person = await client(pool)
    created = await crm_practices.create_practice(
        request=None,
        practice=PracticeCreate(
            client_id=person, practice_type_code="tax_annual_company", status="sending_invoice"
        ),
        background_tasks=BackgroundTasks(),
        db_pool=pool,
        current_user=USER,
    )
    assert await status_of(pool, created.id) == "sending_invoice"


@pytest.mark.asyncio
async def test_patch_with_an_explicit_null_price_gets_the_gate_refusal(pool: asyncpg.Pool) -> None:
    practice = await priceless(pool, await client(pool))
    with pytest.raises(HTTPException) as caught:
        await patch(pool, practice, status="sending_invoice", quoted_price=None)
    assert (caught.value.status_code, caught.value.detail) == (422, QUOTED_PRICE_REQUIRED_DETAIL)
    assert await status_of(pool, practice) == "waiting_documents"


@pytest.mark.asyncio
async def test_a_concurrent_zero_price_write_cannot_slip_the_move_through(
    pool: asyncpg.Pool, invoice_automation_double: MagicMock
) -> None:
    created = await create(pool, await client(pool))  # priced: the early read sees a price
    await pool.execute("UPDATE practices SET status = 'waiting_documents' WHERE id=$1", created.id)
    async with pool.acquire() as other:
        txn = other.transaction()
        await txn.start()
        await other.execute("UPDATE practices SET quoted_price = 0 WHERE id = $1", created.id)
        mover = asyncio.create_task(patch(pool, created.id, status="sending_invoice"))
        await asyncio.sleep(0.5)  # the PATCH has read the old price and waits on the row lock
        assert not mover.done()
        await txn.commit()
    with pytest.raises(HTTPException) as caught:
        await mover
    assert (caught.value.status_code, caught.value.detail) == (422, QUOTED_PRICE_REQUIRED_DETAIL)
    assert await status_of(pool, created.id) == "waiting_documents"
    invoice_automation_double.trigger_on_sending_invoice.assert_not_called()


@pytest.mark.asyncio
async def test_a_concurrent_move_out_with_a_zero_price_cannot_be_undone_by_a_stale_status(
    pool: asyncpg.Pool, invoice_automation_double: MagicMock
) -> None:
    created = await create(pool, await client(pool))
    await pool.execute("UPDATE practices SET status = 'sending_invoice' WHERE id=$1", created.id)
    async with pool.acquire() as other:
        txn = other.transaction()
        await txn.start()
        await other.execute(
            "UPDATE practices SET status = 'waiting_documents', quoted_price = 0 WHERE id = $1",
            created.id,
        )
        mover = asyncio.create_task(patch(pool, created.id, status="sending_invoice"))
        await asyncio.sleep(0.5)  # A has read sending_invoice and waits on the row lock
        assert not mover.done()
        await txn.commit()
    with pytest.raises(HTTPException) as caught:
        await mover
    assert (caught.value.status_code, caught.value.detail) == (422, QUOTED_PRICE_REQUIRED_DETAIL)
    assert await status_of(pool, created.id) == "waiting_documents"
    invoice_automation_double.trigger_on_sending_invoice.assert_not_called()


@pytest.mark.asyncio
async def test_a_concurrent_move_out_with_the_price_kept_lets_the_reentry_pass(
    pool: asyncpg.Pool,
) -> None:
    created = await create(pool, await client(pool))
    await pool.execute("UPDATE practices SET status = 'sending_invoice' WHERE id=$1", created.id)
    async with pool.acquire() as other:
        txn = other.transaction()
        await txn.start()
        await other.execute(
            "UPDATE practices SET status = 'waiting_documents' WHERE id = $1", created.id
        )
        mover = asyncio.create_task(patch(pool, created.id, status="sending_invoice"))
        await asyncio.sleep(0.5)
        assert not mover.done()
        await txn.commit()
    out = await mover
    assert out["status"] == "sending_invoice"
    assert await status_of(pool, created.id) == "sending_invoice"


@pytest.mark.asyncio
async def test_a_practice_that_stays_in_sending_invoice_is_never_gated(
    pool: asyncpg.Pool,
) -> None:
    practice = await priceless(pool, await client(pool), status="sending_invoice")
    out = await patch(pool, practice, notes="edit while priceless in sending_invoice")
    assert out["status"] == "sending_invoice"
    again = await patch(pool, practice, status="sending_invoice")
    assert again["status"] == "sending_invoice"
