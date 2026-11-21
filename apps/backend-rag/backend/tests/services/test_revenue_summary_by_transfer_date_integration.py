"""The monthly revenue counts the money received in a month, by the date the transfer landed.

The figures come from the imported bank statements: a client transfer counts once staff
reconcile it (confirmed as a payment, or linked to a payment already recorded); transfers still
waiting in the inbox are counted apart and keep the month partial; a month with no imported
statement has no figure. Every action goes through the real reconciliation service and the
report through the real endpoint function. Invented data only; dates are relative to the
current month.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import asyncpg
import pytest

from backend.app.routers import crm_analytics
from backend.core.pg_json_codec import init_asyncpg_connection
from backend.services.accounting.reconcile_service import (
    confirm_payment,
    ignore_transaction,
    link_recorded,
)
from backend.tests.services.test_accounting_reconcile_integration import (
    DEFAULT_COMPANY_STATEMENT_SQL,
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
STAFF = "staff@example.test"


def _month(offset: int) -> date:
    """First day of the month `offset` months before the current one (UTC)."""
    return crm_analytics._months_ago(offset).date()


def _label(offset: int) -> str:
    return _month(offset).strftime("%Y-%m")


class _Bank:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self.n = 0
        self.statement: int | None = None

    async def row(self, day: date, amount: int, direction: str = "credit") -> int:
        """A row of an imported statement, waiting in the inbox."""
        if self.statement is None:
            self.statement = await self.conn.fetchval(DEFAULT_COMPANY_STATEMENT_SQL)
        return await self.conn.fetchval(
            "INSERT INTO bank_transactions (statement_id, txn_date, amount_idr, direction)"
            " VALUES ($1, $2, $3, $4) RETURNING id",
            self.statement,
            day,
            amount,
            direction,
        )

    async def practice(
        self, *, created: date, amount: int, owner: str = "owner@example.test", paid: bool = False
    ) -> tuple[int, int]:
        self.n += 1
        cid = await self.conn.fetchval(
            "INSERT INTO clients (full_name, assigned_to) VALUES ($1, $2) RETURNING id",
            f"Placeholder {self.n}",
            owner,
        )
        pid = await self.conn.fetchval(
            "INSERT INTO practices (client_id, payment_status, quoted_price, created_at)"
            " VALUES ($1, $2, $3, $4) RETURNING id",
            cid,
            "paid" if paid else "unpaid",
            amount,
            datetime.combine(created, datetime.min.time(), tzinfo=timezone.utc)
            + timedelta(hours=10),
        )
        iid = await self.conn.fetchval(
            "INSERT INTO invoices (practice_id, invoice_number, amount_idr)"
            " VALUES ($1, $2, $3) RETURNING id",
            pid,
            f"INV-TEST-{self.n}",
            amount,
        )
        return pid, iid

    async def confirmed(
        self, *, created: date, transfer: date, amount: int, owner: str = "owner@example.test"
    ) -> int:
        """A practice created on `created`, paid by a transfer of `transfer` confirmed by staff."""
        pid, iid = await self.practice(created=created, amount=amount, owner=owner)
        txid = await self.row(transfer, amount)
        await confirm_payment(
            self.conn,
            bank_txn_id=txid,
            practice_id=pid,
            invoice_id=iid,
            amount_applied_idr=amount,
            new_status="paid",
            confirmed_by=STAFF,
            movement_date=transfer,
        )
        return txid


async def _summary(pool: asyncpg.Pool, scope: list[str] | None = None) -> dict[str, dict]:
    with patch.object(crm_analytics, "get_crm_user_filter", lambda _user: scope):
        result = await crm_analytics.get_revenue_summary(
            current_user={"email": "viewer@example.test"}, db_pool=pool
        )
    return {row["period"]: row for row in result["by_month"]}


def _shape(month: dict) -> tuple:
    return (month["status"], month["collected"], month["payments"], month["to_reconcile"])


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
        bank = _Bank(conn)
        # last month's statement holds only money going out: nothing was received
        await bank.row(_month(1) + timedelta(days=20), 300_000, direction="debit")
        # created last month, paid by a transfer of this month
        await bank.confirmed(
            created=_month(1) + timedelta(days=14),
            transfer=_month(0) + timedelta(days=4),
            amount=2_415_000,
        )

        months = await _summary(pool)

        assert list(months) == [_label(i) for i in range(5, -1, -1)]
        assert _shape(months[_label(0)]) == ("complete", 2_415_000, 1, 0)
        assert _shape(months[_label(1)]) == ("complete", 0, 0, 0), "the creation month got nothing"
        for i in (2, 3, 4, 5):
            assert _shape(months[_label(i)]) == ("no_statement", None, None, 0)

    await _with_db(test)


@pytest.mark.asyncio
async def test_a_month_with_transfers_still_in_the_inbox_is_partial_never_a_known_zero() -> None:
    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        bank = _Bank(conn)
        await bank.confirmed(
            created=_month(2), transfer=_month(2) + timedelta(days=9), amount=123_000
        )
        # last month: the statement is imported, its only transfer not reconciled yet
        waiting = await bank.row(_month(1) + timedelta(days=8), 456_000)
        # this month: one transfer confirmed, one still waiting
        await bank.confirmed(
            created=_month(0), transfer=_month(0) + timedelta(days=1), amount=700_000
        )
        await bank.row(_month(0) + timedelta(days=2), 50_000)

        months = await _summary(pool)
        assert _shape(months[_label(2)]) == ("complete", 123_000, 1, 0)
        assert _shape(months[_label(1)]) == ("partial", 0, 0, 1)
        assert _shape(months[_label(0)]) == ("partial", 700_000, 1, 1)

        # confirming the waiting transfer completes its month with the money it brought
        pid, iid = await bank.practice(created=_month(1), amount=456_000)
        await confirm_payment(
            conn,
            bank_txn_id=waiting,
            practice_id=pid,
            invoice_id=iid,
            amount_applied_idr=456_000,
            new_status="paid",
            confirmed_by=STAFF,
            movement_date=_month(1) + timedelta(days=8),
        )
        months = await _summary(pool)
        assert _shape(months[_label(1)]) == ("complete", 456_000, 1, 0)

    await _with_db(test)


@pytest.mark.asyncio
async def test_a_transfer_linked_to_a_payment_already_recorded_is_money_received() -> None:
    """Linking moves no money in the ledger, but the transfer did land in the bank that month."""

    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        bank = _Bank(conn)
        pid, iid = await bank.practice(created=_month(3), amount=900_000, paid=True)
        txid = await bank.row(_month(1) + timedelta(days=3), 900_000)
        await link_recorded(
            conn, bank_txn_id=txid, practice_id=pid, invoice_id=iid, linked_by=STAFF
        )

        months = await _summary(pool)
        assert _shape(months[_label(1)]) == ("complete", 900_000, 1, 0)

    await _with_db(test)


@pytest.mark.asyncio
async def test_ignored_credits_and_money_going_out_are_not_money_received() -> None:
    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        bank = _Bank(conn)
        await bank.confirmed(created=_month(0), transfer=_month(0), amount=3_000_000)
        interest = await bank.row(_month(0) + timedelta(days=2), 58_000)
        await ignore_transaction(
            conn, bank_txn_id=interest, reason="bank interest", ignored_by=STAFF
        )
        await bank.row(_month(0) + timedelta(days=3), 500_000, direction="debit")

        months = await _summary(pool)
        assert _shape(months[_label(0)]) == ("complete", 3_000_000, 1, 0)

    await _with_db(test)


@pytest.mark.asyncio
async def test_transfers_on_the_first_and_last_day_of_a_month_stay_in_their_month() -> None:
    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        bank = _Bank(conn)
        await bank.confirmed(
            created=_month(2), transfer=_month(1) - timedelta(days=1), amount=11_000
        )
        await bank.confirmed(created=_month(2), transfer=_month(1), amount=22_000)
        await bank.confirmed(
            created=_month(2), transfer=_month(0) - timedelta(days=1), amount=44_000
        )
        await bank.confirmed(created=_month(2), transfer=_month(0), amount=88_000)
        # the first day of the window counts; the day before it and the first day after it do not
        await bank.row(_month(5), 5_000)
        await bank.row(_month(5) - timedelta(days=1), 1_000)
        await bank.row(crm_analytics._next_month_start(crm_analytics._months_ago(0)).date(), 2_000)

        months = await _summary(pool)
        assert _shape(months[_label(2)]) == ("complete", 11_000, 1, 0)
        assert _shape(months[_label(1)]) == ("complete", 66_000, 2, 0)
        assert _shape(months[_label(0)]) == ("complete", 88_000, 1, 0)
        assert _shape(months[_label(5)]) == ("partial", 0, 0, 1)
        assert _shape(months[_label(4)]) == ("no_statement", None, None, 0)

    await _with_db(test)


@pytest.mark.asyncio
async def test_a_team_member_sees_the_payments_of_their_own_clients_only() -> None:
    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        bank = _Bank(conn)
        transfer = _month(0) + timedelta(days=3)
        await bank.confirmed(
            created=_month(0), transfer=transfer, amount=1_200_000, owner="mine@example.test"
        )
        await bank.confirmed(
            created=_month(0), transfer=transfer, amount=5_000_000, owner="other@example.test"
        )

        mine = await _summary(pool, scope=["mine@example.test"])
        everyone = await _summary(pool, scope=None)
        assert _shape(mine[_label(0)]) == ("complete", 1_200_000, 1, 0)
        assert _shape(everyone[_label(0)]) == ("complete", 6_200_000, 2, 0)

    await _with_db(test)


@pytest.mark.asyncio
async def test_with_no_imported_statement_every_month_has_no_figure() -> None:
    """No record is not a zero: without a statement the report cannot say Rp 0."""

    async def test(conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        months = await _summary(pool)
        assert [_shape(m) for m in months.values()] == [("no_statement", None, None, 0)] * 6

    await _with_db(test)
