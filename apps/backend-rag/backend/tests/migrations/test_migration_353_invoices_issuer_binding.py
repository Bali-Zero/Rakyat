"""Migration 353 DDL contracts and real guarded-upsert semantics (both variants).

Runs against ``TEST_DATABASE_URL`` like test_migration_312_*.py: it
drops-then-rebuilds ONLY the synthetic ``public.invoices`` table it creates.
Never touches any other object in whatever database the DSN names.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY
from backend.services.invoicing.invoice_service import (
    _INVOICE_UPSERT_PRE158_SQL,
    _INVOICE_UPSERT_SQL,
)

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
MIGRATION = "353_invoices_issuer_binding.sql"

SYNTHETIC_INVOICES_DDL = """
CREATE TABLE public.invoices (
    practice_id integer UNIQUE,
    client_id integer,
    invoice_number text,
    invoice_source text,
    amount_idr numeric,
    discount_amount_idr numeric,
    drive_file_id text,
    drive_web_link text,
    email_sent_to_client boolean,
    accounting_notified boolean,
    generated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz
)
"""

B_KEY = "mandiri_bali_nol_impresariat"
C_KEY = "bni_bayu_santero"
ROUTED_AT = datetime(2026, 11, 11, 15, 59, 59, tzinfo=timezone.utc)


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


def _forward() -> str:
    forward, _ = split_migration_sql((MIGRATIONS_DIR / MIGRATION).read_text(encoding="utf-8"))
    return forward


def _rollback() -> str:
    _, rollback = split_migration_sql((MIGRATIONS_DIR / MIGRATION).read_text(encoding="utf-8"))
    return rollback or ""


def _service_args(key: str, routed_at: datetime | None, practice_id: int = 7001) -> tuple:
    """Exactly the tuple the service builds (14 args); pre-158 drops index 5."""
    return (
        practice_id,
        500,
        "INV-353-1",
        "local_pdf",
        1_000_000.0,
        25_000.0,
        "drive-1",
        "https://drive.example/x",
        True,
        True,
        datetime(2026, 11, 11, tzinfo=timezone.utc),
        key,
        routed_at,
        DEFAULT_ENTITY_KEY,
    )


UPSERT_VARIANTS = [
    pytest.param(_INVOICE_UPSERT_SQL, id="normal"),
    pytest.param(_INVOICE_UPSERT_PRE158_SQL, id="pre158"),
]


def _variant_args(sql: str, key: str, routed_at: datetime | None, practice_id: int = 7001) -> tuple:
    args = _service_args(key, routed_at, practice_id)
    if sql is _INVOICE_UPSERT_PRE158_SQL:
        return (*args[:5], *args[6:])
    return args


@pytest.fixture
async def db() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("SELECT pg_advisory_lock(hashtext('migration-353-test'))")
        await conn.execute("DROP TABLE IF EXISTS public.invoices")
        await conn.execute(SYNTHETIC_INVOICES_DDL)
        yield conn
    finally:
        await conn.execute("DROP TABLE IF EXISTS public.invoices")
        await conn.execute("SELECT pg_advisory_unlock(hashtext('migration-353-test'))")
        await conn.close()


async def _row(conn: asyncpg.Connection, practice_id: int = 7001):
    return await conn.fetchrow("SELECT * FROM public.invoices WHERE practice_id = $1", practice_id)


# ---------------------------------------------------------------------------
# split_migration_sql on the real file
# ---------------------------------------------------------------------------


def test_split_migration_sql_forward_and_rollback() -> None:
    full = (MIGRATIONS_DIR / MIGRATION).read_text(encoding="utf-8")
    forward, rollback = split_migration_sql(full)
    flat_forward = " ".join(forward.split())
    flat_rollback = " ".join(rollback.split())
    assert "ADD COLUMN IF NOT EXISTS issuer_entity_key TEXT NULL" in flat_forward
    assert "ADD COLUMN IF NOT EXISTS issuer_routed_at TIMESTAMPTZ NULL" in flat_forward
    assert forward.count("COMMENT ON COLUMN") == 2
    assert "ROLLBACK" not in forward
    assert "DROP COLUMN IF EXISTS issuer_routed_at" in flat_rollback
    assert "DROP COLUMN IF EXISTS issuer_entity_key" in flat_rollback
    assert "ADD COLUMN" not in flat_rollback


# ---------------------------------------------------------------------------
# Forward DDL contracts on an existing invoices table
# ---------------------------------------------------------------------------


async def test_columns_nullable_typed_default_free_commented(db: asyncpg.Connection) -> None:
    await db.execute("INSERT INTO public.invoices (practice_id) VALUES (1), (2)")
    await db.execute(_forward())
    columns = {
        r["column_name"]: r
        for r in await db.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'invoices'
              AND column_name IN ('issuer_entity_key', 'issuer_routed_at')
            """
        )
    }
    assert set(columns) == {"issuer_entity_key", "issuer_routed_at"}
    assert columns["issuer_entity_key"]["data_type"] == "text"
    assert columns["issuer_entity_key"]["is_nullable"] == "YES"
    assert columns["issuer_entity_key"]["column_default"] is None
    assert columns["issuer_routed_at"]["data_type"] == "timestamp with time zone"
    assert columns["issuer_routed_at"]["is_nullable"] == "YES"
    assert columns["issuer_routed_at"]["column_default"] is None
    comments = dict(
        await db.fetch(
            """
            SELECT a.attname, d.description
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum
            WHERE c.relname = 'invoices' AND c.relnamespace = 'public'::regnamespace
              AND a.attname IN ('issuer_entity_key', 'issuer_routed_at')
            """
        )
    )
    assert all(comments.values()), comments
    assert "legacy" in comments["issuer_entity_key"]
    assert "adoption instant" in comments["issuer_routed_at"]
    rows = await db.fetch("SELECT issuer_entity_key, issuer_routed_at FROM public.invoices")
    assert len(rows) == 2
    assert all(r["issuer_entity_key"] is None and r["issuer_routed_at"] is None for r in rows)


async def test_forward_twice_keeps_populated_bindings(db: asyncpg.Connection) -> None:
    await db.execute(_forward())
    await db.execute(
        """
        INSERT INTO public.invoices (practice_id, issuer_entity_key, issuer_routed_at)
        VALUES ($1, $2, $3)
        """,
        7001,
        B_KEY,
        ROUTED_AT,
    )
    await db.execute(_forward())
    row = await _row(db)
    assert row["issuer_entity_key"] == B_KEY
    assert row["issuer_routed_at"] == ROUTED_AT


# ---------------------------------------------------------------------------
# Absent invoices table: forward and rollback are safe no-ops
# ---------------------------------------------------------------------------


async def test_absent_invoices_table_forward_and_rollback_noop(db: asyncpg.Connection) -> None:
    await db.execute("DROP TABLE public.invoices")
    await db.execute(_forward())
    await db.execute(_rollback())
    exists = await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'invoices')"
    )
    assert exists is False


# ---------------------------------------------------------------------------
# Rollback: drops only these columns, repeatable
# ---------------------------------------------------------------------------


async def test_rollback_drops_only_these_columns_and_is_repeatable(db: asyncpg.Connection) -> None:
    await db.execute(_forward())
    await db.execute("ALTER TABLE public.invoices ADD COLUMN sentinel_keep_me text")
    await db.execute(_rollback())
    await db.execute(_rollback())
    columns = {
        r["column_name"]
        for r in await db.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'invoices'
            """
        )
    }
    assert "issuer_entity_key" not in columns
    assert "issuer_routed_at" not in columns
    assert "sentinel_keep_me" in columns
    assert "practice_id" in columns and "created_at" in columns


# ---------------------------------------------------------------------------
# Real guarded upsert semantics, BOTH service SQL variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", UPSERT_VARIANTS)
async def test_first_insert_stores_key_and_routed_at(db: asyncpg.Connection, sql: str) -> None:
    await db.execute(_forward())
    row = await db.fetchrow(sql, *_variant_args(sql, B_KEY, ROUTED_AT))
    assert row["issuer_entity_key"] == B_KEY
    assert row["issuer_routed_at"] == ROUTED_AT
    stored = await _row(db)
    assert stored["issuer_entity_key"] == B_KEY
    assert stored["issuer_routed_at"] == ROUTED_AT
    assert stored["created_at"] is not None


@pytest.mark.parametrize("sql", UPSERT_VARIANTS)
async def test_second_upsert_same_key_preserves_binding_and_created_at(
    db: asyncpg.Connection, sql: str
) -> None:
    await db.execute(_forward())
    await db.fetchrow(sql, *_variant_args(sql, B_KEY, ROUTED_AT))
    first = await _row(db)
    args = list(_variant_args(sql, B_KEY, ROUTED_AT))
    args[2] = "INV-353-2"
    row = await db.fetchrow(sql, *args)
    assert row["issuer_entity_key"] == B_KEY
    assert row["issuer_routed_at"] == ROUTED_AT
    second = await _row(db)
    assert second["invoice_number"] == "INV-353-2", "ordinary columns still update"
    assert second["created_at"] == first["created_at"], "created_at must never be rewritten"
    assert second["issuer_routed_at"] == ROUTED_AT


@pytest.mark.parametrize("sql", UPSERT_VARIANTS)
async def test_different_key_returns_no_row_and_changes_nothing(
    db: asyncpg.Connection, sql: str
) -> None:
    await db.execute(_forward())
    await db.fetchrow(sql, *_variant_args(sql, B_KEY, ROUTED_AT))
    before = await _row(db)
    row = await db.fetchrow(
        sql, *_variant_args(sql, C_KEY, datetime(2027, 1, 1, tzinfo=timezone.utc))
    )
    assert row is None, "PostgreSQL returns no row when the conflict guard rejects the update"
    after = await _row(db)
    assert after["issuer_entity_key"] == B_KEY
    assert after["issuer_routed_at"] == ROUTED_AT
    assert after["invoice_number"] == before["invoice_number"]


@pytest.mark.parametrize("sql", UPSERT_VARIANTS)
async def test_legacy_null_row_adopted_only_with_default_key(
    db: asyncpg.Connection, sql: str
) -> None:
    await db.execute(_forward())
    await db.execute("INSERT INTO public.invoices (practice_id) VALUES (7001)")
    adopted = await db.fetchrow(sql, *_variant_args(sql, DEFAULT_ENTITY_KEY, ROUTED_AT))
    assert adopted is not None
    row = await _row(db)
    assert row["issuer_entity_key"] == DEFAULT_ENTITY_KEY
    assert row["issuer_routed_at"] == ROUTED_AT

    await db.execute(
        "UPDATE public.invoices SET issuer_entity_key = NULL, issuer_routed_at = NULL "
        "WHERE practice_id = 7001"
    )
    rejected = await db.fetchrow(
        sql, *_variant_args(sql, C_KEY, datetime(2027, 1, 1, tzinfo=timezone.utc))
    )
    assert rejected is None, "a legacy NULL row must not be adopted as another entity"
    row = await _row(db)
    assert row["issuer_entity_key"] is None
    assert row["issuer_routed_at"] is None


@pytest.mark.parametrize("sql", UPSERT_VARIANTS)
async def test_inconsistent_legacy_row_is_not_adopted(db: asyncpg.Connection, sql: str) -> None:
    await db.execute(_forward())
    await db.execute(
        "INSERT INTO public.invoices (practice_id, issuer_entity_key, issuer_routed_at) "
        "VALUES (7001, NULL, $1)",
        ROUTED_AT,
    )
    row = await db.fetchrow(sql, *_variant_args(sql, DEFAULT_ENTITY_KEY, ROUTED_AT))
    assert row is None, "key NULL with routed_at set must fail the adoption guard"
