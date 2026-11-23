from __future__ import annotations

import asyncio
from datetime import date
from uuid import uuid4

import pytest

from backend.services.accounting.cash_receipts import cash_invoices, record_cash
from backend.services.accounting.cashout_service import list_cashout
from backend.services.accounting.reconcile_service import (
    ReconcileError,
    confirm_payment,
    link_recorded,
)
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES
from backend.tests.services.test_accounting_reconcile_integration import (
    _connect,
    _drop_db,
    _fresh_db,
    _world,
)


@pytest.mark.asyncio
async def test_receipt_search_replay_partial_balance_and_bank_ceiling():
    conn, database, user = await _fresh_db()
    second = await _connect(database)
    try:
        practices, invoices, bank = await _world(conn)
        pid, iid = practices[0], invoices[0]
        await conn.execute("UPDATE invoices SET pph_expected_idr=80000 WHERE id=$1", iid)
        await conn.execute("UPDATE practices SET paid_amount=20000 WHERE id=$1", pid)
        other = next(key for key in ENTITIES if key != DEFAULT_ENTITY_KEY)
        assert await cash_invoices(conn, other, "INV-C") == []
        assert (await cash_invoices(conn, DEFAULT_ENTITY_KEY, "inv-c"))[0][
            "outstanding_idr"
        ] == 3_900_000
        payment = {
            "company_key": DEFAULT_ENTITY_KEY,
            "invoice_id": iid,
            "amount_idr": 1_000_000,
            "movement_date": date(2026, 10, 4),
            "reference": "Cash slip",
            "request_id": uuid4(),
            "confirmed_by": "accountant@example.test",
        }
        with pytest.raises(ReconcileError):
            await record_cash(conn, **(payment | {"company_key": other}))
        first, replay = await asyncio.gather(
            record_cash(conn, **payment), record_cash(second, **payment)
        )
        assert first == replay
        assert first["outstanding_idr"] == 2_900_000 and first["status_after"] == "partial"
        with pytest.raises(ReconcileError):
            await record_cash(conn, **(payment | {"amount_idr": 2}))
        with pytest.raises(ReconcileError):
            await record_cash(conn, **(payment | {"amount_idr": 3_000_000, "request_id": uuid4()}))
        with pytest.raises(ReconcileError):
            await confirm_payment(
                conn,
                bank_txn_id=bank,
                practice_id=pid,
                invoice_id=iid,
                amount_applied_idr=4_000_000,
                new_status="paid",
                confirmed_by="test",
            )

        # Two distinct slips race for the remaining balance. Only one may settle it.
        async def settle(connection):
            return await record_cash(
                connection, **(payment | {"amount_idr": 2_900_000, "request_id": uuid4()})
            )

        results = await asyncio.gather(settle(conn), settle(second), return_exceptions=True)
        assert sum(isinstance(result, ReconcileError) for result in results) == 1
        assert [result["status_after"] for result in results if isinstance(result, dict)] == [
            "paid"
        ]
        assert await record_cash(conn, **payment) == first
        assert await conn.fetchval("SELECT paid_amount_idr FROM invoices WHERE id=$1", iid) == 3_920_000
        assert await conn.fetchval("SELECT paid_amount FROM practices WHERE id=$1", pid) == 3_920_000
        assert await cash_invoices(conn, DEFAULT_ENTITY_KEY, "") == []
        with pytest.raises(ReconcileError):
            await link_recorded(
                conn, bank_txn_id=bank, practice_id=pid, invoice_id=iid, linked_by="test"
            )
        rows = await list_cashout(conn, month=date(2026, 10, 1))
        assert len(rows) == 2
        assert {r["cash_company_key"] for r in rows} == {DEFAULT_ENTITY_KEY}
        assert {r["payment_method"] for r in rows} == {"cash"}
        assert (
            await conn.fetchval("SELECT count(*) FROM reconciliation_log WHERE bank_txn_id IS NULL")
            == 2
        )
        assert (
            await conn.fetchval("SELECT reconciled_status FROM bank_transactions WHERE id=$1", bank)
            == "unmatched"
        )
    finally:
        await second.close()
        await _drop_db(conn, database, user)
