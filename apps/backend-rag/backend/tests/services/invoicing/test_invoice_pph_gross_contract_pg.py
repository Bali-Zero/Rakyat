"""K127 x accounting against a real Postgres: the SQL behind the gross contract.

Companion to test_invoice_pph_gross_contract.py (mocked). Each test runs in its own
throwaway database with a synthetic, prod-shaped ``invoices`` table:

- ``_INVOICE_UPSERT_PPH_SQL`` writes amount_idr gross and pph_expected_idr, resets
  pph_expected_idr when the PPh is cleared, and keeps the issuer-binding guard;
- the reminder query and the portal billing query read pph_expected_idr when the
  column exists and 0 when it does not (databases without migration 238).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from backend.services.invoicing import unpaid_invoice_notifier as notifier
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY
from backend.services.invoicing.invoice_service import _INVOICE_UPSERT_PPH_SQL
from backend.services.portal.portal_service import PortalService

pytestmark = pytest.mark.integration

ISSUER_KEY = "permata_bali_nol_konsultan"
ROUTED_AT = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)

SCHEMA = """
CREATE TABLE practice_types (id SERIAL PRIMARY KEY, name TEXT, category TEXT);
CREATE TABLE practices (
    id SERIAL PRIMARY KEY, client_id INT, practice_type_id INT, status TEXT,
    payment_status TEXT DEFAULT 'unpaid', assigned_to TEXT, paid_amount NUMERIC,
    quoted_price NUMERIC);
CREATE TABLE invoices (
    id SERIAL PRIMARY KEY, practice_id INT UNIQUE, client_id INT, invoice_number TEXT,
    invoice_source TEXT, amount_idr NUMERIC, discount_amount_idr NUMERIC,
    drive_file_id TEXT, drive_web_link TEXT, email_sent_to_client BOOLEAN,
    accounting_notified BOOLEAN, generated_at TIMESTAMPTZ, issuer_entity_key TEXT,
    issuer_routed_at TIMESTAMPTZ, paid_amount_idr NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ);
CREATE TABLE invoice_payment_reminders (invoice_id INT PRIMARY KEY);
"""
# Migration 238's column, added only by the tests that model a database that ran it.
PPH_COLUMN = "ALTER TABLE invoices ADD COLUMN pph_expected_idr BIGINT DEFAULT 0"


def _base_dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/postgres",
    )


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    try:
        admin = await asyncpg.connect(_base_dsn(), database="postgres")
    except Exception:
        if os.environ.get("TEST_DATABASE_URL"):
            raise  # a configured database that is unreachable is a failure, never a skip
        pytest.skip("no Postgres reachable for the invoice PPh contract test")
    dbname = f"pph_gross_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    await admin.close()
    c = await asyncpg.connect(_base_dsn(), database=dbname)
    try:
        await c.execute(SCHEMA)
        yield c
    finally:
        await c.close()
        admin = await asyncpg.connect(_base_dsn(), database="postgres")
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()


def _upsert_args(*, amount: float, pph: int, key: str = ISSUER_KEY) -> tuple:
    """The 15 arguments _persist_invoice sends with _INVOICE_UPSERT_PPH_SQL."""
    return (
        7,
        42,
        "INV-SYN-PPH-1",
        "local_pdf",
        amount,
        0.0,
        "drive-1",
        "https://drive.example/x",
        True,
        True,
        ROUTED_AT,
        key,
        ROUTED_AT,
        DEFAULT_ENTITY_KEY,
        pph,
    )


async def test_pph_upsert_writes_gross_and_pph_then_resets_when_cleared(conn):
    await conn.execute(PPH_COLUMN)

    bound = await conn.fetchrow(
        _INVOICE_UPSERT_PPH_SQL, *_upsert_args(amount=2_500_000, pph=50_000)
    )
    assert bound["issuer_entity_key"] == ISSUER_KEY
    row = await conn.fetchrow("SELECT amount_idr, pph_expected_idr FROM invoices")
    assert (row["amount_idr"], row["pph_expected_idr"]) == (2_500_000, 50_000)

    await conn.fetchrow(_INVOICE_UPSERT_PPH_SQL, *_upsert_args(amount=2_500_000, pph=0))
    row = await conn.fetchrow("SELECT amount_idr, pph_expected_idr FROM invoices")
    assert (row["amount_idr"], row["pph_expected_idr"]) == (2_500_000, 0)

    # The issuer-binding guard of the other variants holds: another issuer changes nothing.
    other = await conn.fetchrow(
        _INVOICE_UPSERT_PPH_SQL, *_upsert_args(amount=9, pph=9, key="bni_bayu_santero")
    )
    assert other is None
    row = await conn.fetchrow("SELECT amount_idr, pph_expected_idr FROM invoices")
    assert (row["amount_idr"], row["pph_expected_idr"]) == (2_500_000, 0)


async def test_pph_upsert_is_42703_without_migration_238(conn):
    with pytest.raises(asyncpg.PostgresError) as exc:
        await conn.fetchrow(_INVOICE_UPSERT_PPH_SQL, *_upsert_args(amount=2_500_000, pph=50_000))
    assert exc.value.sqlstate == "42703"  # what _persist_invoice falls back on


async def _seed(conn, *, pph: int | None) -> None:
    created = datetime.now(tz=timezone.utc) - timedelta(days=5)
    await conn.execute(
        "INSERT INTO practice_types (name, category) VALUES ('Synthetic Tax', 'tax')"
    )
    await conn.execute(
        "INSERT INTO practices (client_id, practice_type_id, status, payment_status, assigned_to,"
        " paid_amount, quoted_price) VALUES (42, 1, 'sending_invoice', 'partial',"
        " 'sample.member@example.test', 2450000, 2500000)"
    )
    await conn.execute(
        "INSERT INTO invoices (practice_id, client_id, invoice_number, invoice_source, amount_idr,"
        " paid_amount_idr, generated_at, created_at) VALUES (1, 42, 'INV-SYN-PPH-1', 'local_pdf',"
        " 2500000, 2450000, $1, $1)",
        created,
    )
    if pph is not None:
        await conn.execute(PPH_COLUMN)
        await conn.execute("UPDATE invoices SET pph_expected_idr = $1", pph)


async def _due_rows(conn) -> list[dict]:
    rows = await conn.fetch(
        notifier._DUE_SQL, datetime.now(tz=timezone.utc) - timedelta(days=30), 3
    )
    return [dict(r) for r in rows]


async def test_reminder_query_reads_pph_and_the_digest_shows_nothing_outstanding(conn):
    await _seed(conn, pph=50_000)
    rows = await _due_rows(conn)
    assert rows[0]["pph_expected_idr"] == 50_000
    _, body = notifier._build_digest(rows, "2026-11-20")
    assert "outstanding Rp 0)" in body


async def test_reminder_query_reads_zero_without_migration_238(conn):
    await _seed(conn, pph=None)
    rows = await _due_rows(conn)
    assert rows[0]["pph_expected_idr"] == 0
    _, body = notifier._build_digest(rows, "2026-11-20")
    assert "outstanding Rp 50.000)" in body


class _Pool:
    """The one-connection pool PortalService.get_billing needs."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.parametrize(
    ("pph", "due", "pending"), [(50_000, 2_450_000, 0), (None, 2_500_000, 50_000)]
)
async def test_portal_billing_query_with_and_without_migration_238(conn, pph, due, pending):
    await _seed(conn, pph=pph)
    result = await PortalService(_Pool(conn)).get_billing(
        42, current_user={"client_id": 42, "email": "client@example.test"}
    )
    assert result["invoices"][0]["amount_idr"] == due
    assert result["summary"]["total_pending"] == pending
