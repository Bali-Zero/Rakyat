"""The monthly revenue counts the money received in a month, by the date the transfer landed.

A practice created in one month and paid by a transfer of the next month belongs to the month
of the transfer. Payments are made through the real reconciliation (confirm_payment), which
writes the cash ledger row with the transfer's date; the report is read through the real
endpoint function. Invented data only; dates are relative to the current month.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import asyncpg
import pytest

from backend.app.routers import crm_analytics
from backend.core.pg_json_codec import init_asyncpg_connection
from backend.services.accounting.reconcile_service import confirm_payment
from backend.tests.services.test_accounting_reconcile_integration import (
    _base_dsn,
    _drop_db,
    _fresh_db,
)

# What the revenue summary reads beyond the reconcile stubs: production has these columns.
REPORT_COLUMNS = """
ALTER TABLE practices ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE practices ADD COLUMN IF NOT EXISTS quoted_price NUMERIC;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS assigned_to TEXT;
"""


def _month(offset: int) -> date:
    """First day of the month `offset` months before the current one (UTC)."""
    return crm_analytics._months_ago(offset).date()


def _label(offset: int) -> str:
    return _month(offset).strftime("%Y-%m")


class _Ledger:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self.n = 0

    async def paid_practice(
        self, *, created: date, transfer: date, amount: int, owner: str = "owner@example.test"
    ) -> int:
        """A practice created on `created`, paid through the reconciliation by a transfer of `transfer`."""
        self.n += 1
        cid = await self.conn.fetchval(
            "INSERT INTO clients (full_name, assigned_to) VALUES ($1, $2) RETURNING id",
            f"Placeholder {self.n}",
            owner,
        )
        pid = await self.conn.fetchval(
            "INSERT INTO practices (client_id, payment_status, quoted_price, created_at)"
            " VALUES ($1, 'unpaid', $2, $3) RETURNING id",
            cid,
            amount,
            datetime.combine(created, datetime.min.time(), tzinfo=timezone.utc)
            + timedelta(hours=10),
        )
        iid = await self.conn.fetchval(
            "INSERT INTO invoices (practice_id, invoice_number, amount_idr) VALUES ($1, $2, $3) RETURNING id",
            pid,
            f"INV-TEST-{self.n}",
            amount,
        )
        stmt = await self.conn.fetchval(
            "INSERT INTO bank_statements (source_format) VALUES ('csv') RETURNING id"
        )
        txid = await self.conn.fetchval(
            "INSERT INTO bank_transactions (statement_id, txn_date, amount_idr, direction)"
            " VALUES ($1, $2, $3, 'credit') RETURNING id",
            stmt,
            transfer,
            amount,
        )
        await confirm_payment(
            self.conn,
            bank_txn_id=txid,
            practice_id=pid,
            invoice_id=iid,
            amount_applied_idr=amount,
            new_status="paid",
            confirmed_by="staff@example.test",
            movement_date=transfer,
        )
        return pid


async def _summary(pool: asyncpg.Pool, scope: list[str] | None = None) -> dict[str, dict]:
    with patch.object(crm_analytics, "get_crm_user_filter", lambda _user: scope):
        result = await crm_analytics.get_revenue_summary(
            current_user={"email": "viewer@example.test"}, db_pool=pool
        )
    return {row["period"]: row for row in result["by_month"]}


async def _with_db(test) -> None:
    conn, dbname, user = await _fresh_db()
    pool = None
    try:
        await conn.execute(REPORT_COLUMNS)
        pool = await asyncpg.create_pool(
            _base_dsn(), database=dbname, min_size=1, max_size=2, init=init_asyncpg_connection
        )
        await test(conn, pool)
    finally:
        if pool is not None:
            await pool.close()
        await _drop_db(conn, dbname, user)


@pytest.mark.asyncio
async def test_a_practice_created_in_one_month_and_paid_in_the_next_counts_in_the_month_of_payment() -> (
    None
):
    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        ledger = _Ledger(conn)
        # the first confirmed payment, two months ago: the record starts there
        await ledger.paid_practice(
            created=_month(2) + timedelta(days=3),
            transfer=_month(2) + timedelta(days=9),
            amount=1_000_000,
        )
        # created last month, paid by a transfer of this month
        await ledger.paid_practice(
            created=_month(1) + timedelta(days=14),
            transfer=_month(0) + timedelta(days=4),
            amount=2_415_000,
        )

        months = await _summary(pool)

        assert list(months) == [_label(i) for i in range(5, -1, -1)]
        assert months[_label(0)]["collected"] == 2_415_000
        assert months[_label(0)]["payments"] == 1
        assert months[_label(1)]["collected"] == 0, "the creation month received nothing"
        assert months[_label(1)]["payments"] == 0
        assert months[_label(2)]["collected"] == 1_000_000
        for i in (3, 4, 5):
            assert months[_label(i)]["collected"] is None, "before the first confirmed payment"
            assert months[_label(i)]["payments"] is None

    await _with_db(test)


@pytest.mark.asyncio
async def test_only_confirmed_incoming_payments_count() -> None:
    """Worksheet drafts, money going out and reversed rows are not money received."""

    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        ledger = _Ledger(conn)
        await ledger.paid_practice(
            created=_month(0), transfer=_month(0) + timedelta(days=1), amount=3_000_000
        )
        day = _month(0) + timedelta(days=2)
        await conn.execute(
            "INSERT INTO weekly_cashout (movement_date, direction, type, amount_idr)"
            " VALUES ($1, 'in', 'cashout_worksheet', 7000000), ($1, 'out', 'expense', 500000)",
            day,
        )
        reversed_id = await conn.fetchval(
            "INSERT INTO weekly_cashout (movement_date, direction, type, amount_idr)"
            " VALUES ($1, 'in', 'invoice_payment', 900000) RETURNING id",
            day,
        )
        reversal_id = await conn.fetchval(
            "INSERT INTO weekly_cashout (movement_date, direction, type, amount_idr)"
            " VALUES ($1, 'out', 'refund', 900000) RETURNING id",
            day,
        )
        await conn.execute(
            "UPDATE weekly_cashout SET reversed_by_id = $2 WHERE id = $1", reversed_id, reversal_id
        )

        months = await _summary(pool)
        assert months[_label(0)]["collected"] == 3_000_000
        assert months[_label(0)]["payments"] == 1

    await _with_db(test)


@pytest.mark.asyncio
async def test_a_team_member_sees_the_payments_of_their_own_clients_only() -> None:
    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        ledger = _Ledger(conn)
        transfer = _month(0) + timedelta(days=3)
        await ledger.paid_practice(
            created=_month(0), transfer=transfer, amount=1_200_000, owner="mine@example.test"
        )
        await ledger.paid_practice(
            created=_month(0), transfer=transfer, amount=5_000_000, owner="other@example.test"
        )

        mine = await _summary(pool, scope=["mine@example.test"])
        everyone = await _summary(pool, scope=None)
        assert mine[_label(0)]["collected"] == 1_200_000
        assert everyone[_label(0)]["collected"] == 6_200_000

    await _with_db(test)


@pytest.mark.asyncio
async def test_with_no_confirmed_payment_every_month_reads_not_reconciled() -> None:
    """No record is not a zero: before reconciliation started, the report cannot say Rp 0."""

    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        months = await _summary(pool)
        assert [m["collected"] for m in months.values()] == [None] * 6

    await _with_db(test)
