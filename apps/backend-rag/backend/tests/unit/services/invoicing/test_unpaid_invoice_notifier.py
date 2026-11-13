"""Unpaid-invoice reminder: behaviour pinned against a prod-shaped Postgres.

The schema below carries the columns the notifier reads, with the production
types (invoices.created_at timestamptz, practices.payment_status text, ...).
Migration 360 is applied from the real file. The email transport is stubbed;
everything else (SQL, advisory lock, persistence) is real.

Guilt tests: a due invoice IS reminded, once, in one digest, with a heartbeat.
Innocence tests: young, paid, cancelled, pre-flow and already-reminded
invoices are never named; a failed send records nothing.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from backend.services.invoicing import unpaid_invoice_notifier as mod
from backend.services.invoicing.unpaid_invoice_notifier import (
    HEARTBEAT_KEY,
    INVOICE_REMINDER_FROM,
    UnpaidInvoiceNotifier,
    _build_digest,
)

pytestmark = pytest.mark.integration

MIGRATION_360 = (
    Path(__file__).resolve().parents[3].parent
    / "db"
    / "migrations_v2"
    / "360_invoice_payment_reminders.sql"
)

SCHEMA = """
CREATE TABLE clients (id SERIAL PRIMARY KEY, full_name TEXT, phone TEXT);
CREATE TABLE practice_types (id SERIAL PRIMARY KEY, name TEXT);
CREATE TABLE practices (
    id SERIAL PRIMARY KEY, client_id INT, practice_type_id INT, status TEXT,
    payment_status TEXT DEFAULT 'unpaid', assigned_to TEXT);
CREATE TABLE invoices (
    id SERIAL PRIMARY KEY, practice_id INT UNIQUE, invoice_number TEXT,
    amount_idr NUMERIC, paid_amount_idr NUMERIC,
    generated_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE system_settings (key VARCHAR PRIMARY KEY, value TEXT, updated_at TIMESTAMPTZ DEFAULT now());
"""

CLIENT_SENTINEL = "ZZ-SENTINEL-CLIENT-NAME"
PHONE_SENTINEL = "+620000SENTINEL"


def _base_dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/postgres",
    )


@pytest.fixture
async def pool():
    try:
        admin = await asyncpg.connect(_base_dsn(), database="postgres")
    except Exception:
        if os.environ.get("TEST_DATABASE_URL"):
            raise  # a configured database that is unreachable is a failure, never a skip
        pytest.skip("no Postgres reachable for the unpaid-invoice reminder test")
    dbname = f"unpaid_rem_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    await admin.close()
    p = await asyncpg.create_pool(_base_dsn(), database=dbname, min_size=2, max_size=6)
    async with p.acquire() as conn:
        await conn.execute(SCHEMA)
        forward = re.split(
            r"^-- === ROLLBACK ===\s*$", MIGRATION_360.read_text(), maxsplit=1, flags=re.M
        )[0]
        await conn.execute(forward)
        await conn.execute("INSERT INTO practice_types (name) VALUES ('Sample Visa Service')")
        await conn.execute(
            "INSERT INTO clients (full_name, phone) VALUES ($1, $2)",
            CLIENT_SENTINEL,
            PHONE_SENTINEL,
        )
    yield p
    await p.close()
    admin = await asyncpg.connect(_base_dsn(), database="postgres")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    finally:
        await admin.close()


async def _invoice(
    pool,
    number: str,
    *,
    age: timedelta,
    payment_status: str = "unpaid",
    status: str = "sending_invoice",
    amount: int = 5_000_000,
    paid: int = 0,
    assigned_to: str = "sample.member@balizero.com",
    generated_age: timedelta | None = None,
) -> int:
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        pid = await conn.fetchval(
            "INSERT INTO practices (client_id, practice_type_id, status, payment_status, assigned_to)"
            " VALUES (1, 1, $1, $2, $3) RETURNING id",
            status,
            payment_status,
            assigned_to,
        )
        return await conn.fetchval(
            "INSERT INTO invoices (practice_id, invoice_number, amount_idr, paid_amount_idr,"
            " generated_at, created_at) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            pid,
            number,
            amount,
            paid,
            now - (generated_age or age),
            now - age,
        )


@pytest.fixture
def send():
    with patch.object(mod, "send_internal_email", new=AsyncMock(return_value=True)) as m:
        yield m


@pytest.fixture(autouse=True)
def _flow_start_in_the_past(monkeypatch):
    # Real constant is 2026-11-11 00:00 WITA; tests pin it explicitly where it matters.
    monkeypatch.setattr(
        mod, "INVOICE_REMINDER_FROM", datetime.now(tz=timezone.utc) - timedelta(days=30)
    )


async def _reminded(pool) -> set[int]:
    async with pool.acquire() as conn:
        return {
            r["invoice_id"]
            for r in await conn.fetch("SELECT invoice_id FROM invoice_payment_reminders")
        }


# ── guilt ────────────────────────────────────────────────────────────────────


async def test_due_invoice_is_reminded_once_in_one_digest(pool, send):
    a = await _invoice(pool, "INV-T-1", age=timedelta(days=4))
    b = await _invoice(
        pool, "INV-T-2", age=timedelta(days=5), payment_status="partial", paid=2_000_000
    )
    result = await UnpaidInvoiceNotifier(pool).check_and_notify()
    assert result == {"due_count": 2, "notified": True, "skipped": False}
    assert send.await_count == 1
    kwargs = send.await_args.kwargs
    assert kwargs["to"] == "asya@balizero.com"
    assert kwargs["email_type"] == "unpaid_invoice_digest"
    for needle in (
        "INV-T-1",
        "INV-T-2",
        "Sample Visa Service",
        "Rp 5.000.000",
        "sample.member@balizero.com",
    ):
        assert needle in kwargs["body"]
    assert "outstanding Rp 3.000.000" in kwargs["body"]
    assert await _reminded(pool) == {a, b}


async def test_second_run_does_not_repeat(pool, send):
    await _invoice(pool, "INV-T-1", age=timedelta(days=4))
    await UnpaidInvoiceNotifier(pool).check_and_notify()
    again = await UnpaidInvoiceNotifier(pool).check_and_notify()
    assert again == {"due_count": 0, "notified": False, "skipped": True}
    assert send.await_count == 1


async def test_a_new_notifier_instance_after_restart_does_not_repeat(pool, send):
    await _invoice(pool, "INV-T-1", age=timedelta(days=4))
    await UnpaidInvoiceNotifier(pool).check_and_notify()
    await _invoice(pool, "INV-T-2", age=timedelta(days=4))
    result = await UnpaidInvoiceNotifier(pool).check_and_notify()
    assert result["due_count"] == 1
    assert "INV-T-1" not in send.await_args.kwargs["body"]
    assert "INV-T-2" in send.await_args.kwargs["body"]


async def test_three_day_boundary(pool, send):
    await _invoice(pool, "INV-OLD", age=timedelta(days=3, minutes=1))
    await _invoice(pool, "INV-YOUNG", age=timedelta(days=2, hours=23))
    await UnpaidInvoiceNotifier(pool).check_and_notify()
    body = send.await_args.kwargs["body"]
    assert "INV-OLD" in body and "INV-YOUNG" not in body


async def test_overlapping_runs_send_once(pool):
    await _invoice(pool, "INV-T-1", age=timedelta(days=4))
    gate = asyncio.Event()

    async def slow_send(**_kw):
        await gate.wait()
        return True

    with patch.object(mod, "send_internal_email", new=AsyncMock(side_effect=slow_send)) as m:
        first = asyncio.create_task(UnpaidInvoiceNotifier(pool).check_and_notify())
        await asyncio.sleep(0.2)
        second = await UnpaidInvoiceNotifier(pool).check_and_notify()
        gate.set()
        first_result = await first
    assert second["locked"] is True and second["notified"] is False
    assert first_result["notified"] is True
    assert m.await_count == 1


async def test_heartbeat_written_even_when_nothing_is_due(pool, send):
    await UnpaidInvoiceNotifier(pool).check_and_notify()
    async with pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT value, updated_at FROM system_settings WHERE key = $1", HEARTBEAT_KEY
        )
    assert (
        first is not None
        and '"due_count": 0' in first["value"]
        and '"notified": false' in first["value"]
    )
    await UnpaidInvoiceNotifier(pool).check_and_notify()
    async with pool.acquire() as conn:
        second = await conn.fetchrow(
            "SELECT updated_at FROM system_settings WHERE key = $1", HEARTBEAT_KEY
        )
    assert second["updated_at"] > first["updated_at"]
    send.assert_not_awaited()


# ── innocence ────────────────────────────────────────────────────────────────


async def test_invoices_that_must_not_be_named(pool, send):
    await _invoice(pool, "INV-PAID", age=timedelta(days=9), payment_status="paid")
    await _invoice(pool, "INV-CANCELLED", age=timedelta(days=9), status="cancelled")
    await _invoice(pool, "INV-YOUNG", age=timedelta(days=1))
    result = await UnpaidInvoiceNotifier(pool).check_and_notify()
    assert result == {"due_count": 0, "notified": False, "skipped": True}
    send.assert_not_awaited()
    assert await _reminded(pool) == set()


async def test_backlog_before_the_new_flow_is_never_emailed(pool, send, monkeypatch):
    monkeypatch.setattr(
        mod, "INVOICE_REMINDER_FROM", datetime.now(tz=timezone.utc) - timedelta(days=6)
    )
    await _invoice(pool, "INV-BACKLOG", age=timedelta(days=20))
    # regenerated yesterday: generated_at is recent, created_at (first issue) is not
    await _invoice(pool, "INV-REGEN", age=timedelta(days=20), generated_age=timedelta(days=1))
    await _invoice(pool, "INV-NEW", age=timedelta(days=4))
    await UnpaidInvoiceNotifier(pool).check_and_notify()
    body = send.await_args.kwargs["body"]
    assert "INV-NEW" in body and "INV-BACKLOG" not in body and "INV-REGEN" not in body


async def test_flow_start_is_inclusive_and_is_wita_midnight_of_11_nov():
    assert INVOICE_REMINDER_FROM == datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)


async def test_failed_send_records_nothing_and_next_run_retries(pool):
    await _invoice(pool, "INV-T-1", age=timedelta(days=4))
    with patch.object(mod, "send_internal_email", new=AsyncMock(return_value=False)):
        failed = await UnpaidInvoiceNotifier(pool).check_and_notify()
    assert failed == {"due_count": 1, "notified": False, "skipped": False, "error": "send_failed"}
    assert await _reminded(pool) == set()
    with patch.object(mod, "send_internal_email", new=AsyncMock(return_value=True)) as ok:
        retried = await UnpaidInvoiceNotifier(pool).check_and_notify()
    assert retried["notified"] is True and ok.await_count == 1


async def test_digest_carries_no_client_data(pool, send):
    await _invoice(pool, "INV-T-1", age=timedelta(days=4))
    await UnpaidInvoiceNotifier(pool).check_and_notify()
    kw = send.await_args.kwargs
    blob = kw["subject"] + kw["body"]
    assert CLIENT_SENTINEL not in blob and PHONE_SENTINEL not in blob


async def test_database_error_is_reported_not_raised():
    class BrokenPool:
        def acquire(self, **_kw):
            raise RuntimeError("db down")

    result = await UnpaidInvoiceNotifier(BrokenPool()).check_and_notify()
    assert result["notified"] is False and result["error"] == "RuntimeError"


def test_digest_escapes_html():
    _, body = _build_digest(
        [
            {
                "invoice_number": "<b>X</b>",
                "service_name": "<script>",
                "amount_idr": 1,
                "payment_status": "unpaid",
                "assigned_to": "a&b",
                "days_unpaid": 3,
            }
        ],
        "13 November 2026",
    )
    # every interpolated field is escaped, and no raw form survives
    assert "&lt;b&gt;X&lt;/b&gt;" in body and "<b>X</b>" not in body
    assert "&lt;script&gt;" in body and "<script>" not in body
    assert "a&amp;b" in body and "a&b<" not in body


async def test_query_failure_still_writes_a_heartbeat_with_the_error(pool, send, monkeypatch):
    monkeypatch.setattr(mod, "_DUE_SQL", "SELECT * FROM table_that_does_not_exist")
    result = await UnpaidInvoiceNotifier(pool).check_and_notify()
    assert result["notified"] is False and result["error"] == "UndefinedTableError"
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT value FROM system_settings WHERE key = $1", HEARTBEAT_KEY
        )
    assert '"error": "UndefinedTableError"' in value
    send.assert_not_awaited()
