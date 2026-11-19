"""Historical mode of the payment confirmation: the same rows, no email to anybody.

The practice listener decides from the notification the production trigger sends. So the
test database carries that trigger, with the function body taken from migration 146 as it is,
and the payload under test is the NOTIFY text Postgres delivers, caught with LISTEN. The
listener's two senders are replaced by recorders: the emails themselves are not under test
here, the decision to send them is. Invented data only.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from backend.core.pg_json_codec import init_asyncpg_connection
from backend.services.accounting.reconcile_service import (
    HISTORICAL_NOTE,
    confirm_payment,
    recorded_without_notifications,
)
from backend.services.crm.practice_status_listener import PracticeStatusListener
from backend.tests.services.test_accounting_reconcile_integration import (
    _base_dsn,
    _connect,
    _drop_db,
    _fresh_db,
)

MIGRATION_146 = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations_v2"
    / "146_eventbus_triggers_use_outbox.sql"
)

# What the trigger reads and writes beyond the reconcile stubs: production has these columns.
TRIGGER_TABLES = """
ALTER TABLE practices ADD COLUMN IF NOT EXISTS status VARCHAR;
ALTER TABLE practices ADD COLUMN IF NOT EXISTS assigned_to VARCHAR;
CREATE TABLE IF NOT EXISTS events_outbox (
    id BIGSERIAL PRIMARY KEY, channel TEXT NOT NULL, payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), consumed_at TIMESTAMPTZ, consumer_id TEXT);
"""
TRIGGER = (
    "CREATE TRIGGER trg_practice_changed AFTER UPDATE ON practices "
    "FOR EACH ROW EXECUTE FUNCTION notify_practice_change()"
)


def _function_of_146() -> str:
    text = MIGRATION_146.read_text(encoding="utf-8")
    found = re.search(
        r"CREATE OR REPLACE FUNCTION notify_practice_change\(\).*?\$\$ LANGUAGE plpgsql;",
        text,
        flags=re.DOTALL,
    )
    assert found, "migration 146 no longer defines notify_practice_change()"
    return found.group(0)


class _Db:
    """A fresh reconcile database with the production practice trigger and a LISTEN."""

    async def __aenter__(self) -> _Db:
        self.conn, self.dbname, self.user = await _fresh_db()
        await self.conn.execute(TRIGGER_TABLES)
        await self.conn.execute(_function_of_146())
        await self.conn.execute(TRIGGER)
        self.pool = await asyncpg.create_pool(
            _base_dsn(),
            database=self.dbname,
            min_size=1,
            max_size=3,
            init=init_asyncpg_connection,
        )
        self.listen_conn = await _connect(self.dbname)
        self.notes: asyncio.Queue[str] = asyncio.Queue()
        await self.listen_conn.add_listener(
            "practice_changed", lambda *args: self.notes.put_nowait(args[3])
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.listen_conn.close()
        await self.pool.close()
        await _drop_db(self.conn, self.dbname, self.user)

    async def seed(self, n: int) -> tuple[int, int, int]:
        cid = await self.conn.fetchval(
            "INSERT INTO clients (full_name) VALUES ($1) RETURNING id", f"Placeholder {n}"
        )
        pid = await self.conn.fetchval(
            "INSERT INTO practices (client_id, payment_status, status, assigned_to)"
            " VALUES ($1, 'unpaid', 'sending_invoice', 'team.member@example.test') RETURNING id",
            cid,
        )
        iid = await self.conn.fetchval(
            "INSERT INTO invoices (practice_id, invoice_number, amount_idr)"
            " VALUES ($1, $2, 1500000) RETURNING id",
            pid,
            f"INV-TEST-{n}",
        )
        stmt = await self.conn.fetchval(
            "INSERT INTO bank_statements (source_format) VALUES ('csv') RETURNING id"
        )
        txid = await self.conn.fetchval(
            "INSERT INTO bank_transactions (statement_id, txn_date, amount_idr, direction)"
            " VALUES ($1, '2026-01-10', 1500000, 'credit') RETURNING id",
            stmt,
        )
        return pid, iid, txid

    async def confirm(self, pid: int, iid: int, txid: int, *, historical: bool) -> str:
        await confirm_payment(
            self.conn,
            bank_txn_id=txid,
            practice_id=pid,
            invoice_id=iid,
            amount_applied_idr=1_500_000,
            new_status="paid",
            confirmed_by="staff@example.test",
            movement_date=None,
            historical=historical,
        )
        return await asyncio.wait_for(self.notes.get(), timeout=10)


def _listener(pool: asyncpg.Pool) -> PracticeStatusListener:
    listener = PracticeStatusListener(db_dsn="unused", db_pool=pool)
    listener._on_payment_received = AsyncMock()
    listener._on_status_changed = AsyncMock()
    return listener


@pytest.mark.asyncio
async def test_a_historical_confirmation_writes_the_same_rows_and_notifies_nobody() -> None:
    async with _Db() as db:
        pid, iid, txid = await db.seed(1)
        payload = await db.confirm(pid, iid, txid, historical=True)
        data = json.loads(payload)
        assert (data["practice_id"], data["old_payment"], data["new_payment"]) == (
            pid,
            "unpaid",
            "paid",
        )

        listener = _listener(db.pool)
        await listener._handle_notification(payload)

        listener._on_payment_received.assert_not_awaited()
        listener._on_status_changed.assert_not_awaited()
        assert (
            await db.conn.fetchval("SELECT payment_status FROM practices WHERE id = $1", pid)
            == "paid"
        )
        assert (
            await db.conn.fetchval(
                "SELECT reconciled_status FROM bank_transactions WHERE id = $1", txid
            )
            == "matched"
        )
        assert (
            await db.conn.fetchval("SELECT paid_amount_idr FROM invoices WHERE id = $1", iid)
            == 1_500_000
        )
        assert (
            await db.conn.fetchval(
                "SELECT note FROM reconciliation_log WHERE practice_id = $1", pid
            )
            == HISTORICAL_NOTE
        )


@pytest.mark.asyncio
async def test_an_ordinary_confirmation_still_notifies_once() -> None:
    """Innocence: the mode is off by default and the payment emails follow as before."""
    async with _Db() as db:
        pid, iid, txid = await db.seed(1)
        payload = await db.confirm(pid, iid, txid, historical=False)

        listener = _listener(db.pool)
        await listener._handle_notification(payload)

        listener._on_payment_received.assert_awaited_once()
        assert listener._on_payment_received.await_args.args[0] == pid
        assert (
            await db.conn.fetchval(
                "SELECT note FROM reconciliation_log WHERE practice_id = $1", pid
            )
            is None
        )


@pytest.mark.asyncio
async def test_a_historical_confirmation_does_not_silence_the_next_ordinary_one() -> None:
    async with _Db() as db:
        old_pid, old_iid, old_txid = await db.seed(1)
        new_pid, new_iid, new_txid = await db.seed(2)
        old_payload = await db.confirm(old_pid, old_iid, old_txid, historical=True)
        new_payload = await db.confirm(new_pid, new_iid, new_txid, historical=False)

        listener = _listener(db.pool)
        await listener._handle_notification(old_payload)
        await listener._handle_notification(new_payload)

        listener._on_payment_received.assert_awaited_once()
        assert listener._on_payment_received.await_args.args[0] == new_pid


@pytest.mark.asyncio
async def test_the_stamp_must_match_to_the_microsecond() -> None:
    """The lookup joins on the trigger's instant: one microsecond off is another transaction."""
    async with _Db() as db:
        pid, iid, txid = await db.seed(1)
        payload = await db.confirm(pid, iid, txid, historical=True)
        stamp = json.loads(payload, parse_float=str)["ts"]

        assert await recorded_without_notifications(db.pool, pid, stamp) is True
        off = str(Decimal(stamp) + Decimal("0.000001"))
        assert await recorded_without_notifications(db.pool, pid, off) is False
        assert await recorded_without_notifications(db.pool, pid, None) is False
        assert await recorded_without_notifications(db.pool, pid + 1, stamp) is False


@pytest.mark.asyncio
async def test_when_the_mode_cannot_be_read_nothing_is_sent() -> None:
    """An email about an old payment cannot be taken back: an unreadable answer sends nothing."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=OSError("database unreachable"))
    listener = _listener(pool)
    payload = json.dumps(
        {
            "practice_id": 7,
            "old_status": "sending_invoice",
            "new_status": "sending_invoice",
            "old_payment": "unpaid",
            "new_payment": "paid",
            "ts": 1767225600.123456,
        }
    )
    await listener._handle_notification(payload)
    listener._on_payment_received.assert_not_awaited()
    pool.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_change_without_a_stamp_is_ordinary_and_asks_nothing() -> None:
    """Innocence: payloads without `ts` (older emitters, the listener's own tests) still send."""
    pool = MagicMock()
    pool.fetchval = AsyncMock()
    listener = _listener(pool)
    payload = json.dumps({"practice_id": 7, "old_payment": "unpaid", "new_payment": "paid"})
    await listener._handle_notification(payload)
    listener._on_payment_received.assert_awaited_once()
    pool.fetchval.assert_not_awaited()
