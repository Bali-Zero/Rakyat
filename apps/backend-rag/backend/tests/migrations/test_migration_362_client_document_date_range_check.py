"""Migration 362: repair by rule, then CHECK on the four date columns.

Runs against a THROWAWAY database created next to ``TEST_DATABASE_URL`` with
synthetic ``clients`` / ``documents`` tables; nothing in the DSN's own
database is touched. Skipped when the role cannot create a database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
MIGRATION = "362_client_document_date_range_check.sql"
CONSTRAINTS = {
    "clients_passport_expiry_range_chk",
    "clients_visa_expiry_date_range_chk",
    "clients_kitas_expiry_date_range_chk",
    "documents_expiry_date_range_chk",
}
DDL = """
CREATE TABLE public.clients (
    id integer PRIMARY KEY,
    passport_expiry date,
    visa_expiry_date date,
    kitas_expiry_date date,
    date_of_birth date
);
CREATE TABLE public.documents (
    id integer PRIMARY KEY,
    client_id integer,
    document_type varchar,
    expiry_date date,
    issue_date date,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);
"""


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


def _forward() -> str:
    forward, _ = split_migration_sql((MIGRATIONS_DIR / MIGRATION).read_text(encoding="utf-8"))
    return forward


@pytest.fixture
async def db() -> AsyncIterator[asyncpg.Connection]:
    parts = urlsplit(_dsn())
    admin = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    name = f"mig362_{uuid.uuid4().hex[:12]}"
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.PostgresError as exc:
        await admin.close()
        pytest.skip(f"cannot create a scratch database: {exc}")
    conn = await asyncpg.connect(urlunsplit(parts._replace(path=f"/{name}")))
    try:
        await conn.execute(DDL)
        yield conn
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _seed(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        INSERT INTO clients (id, passport_expiry, visa_expiry_date, kitas_expiry_date, date_of_birth)
        VALUES
          (1, '-infinity', NULL, NULL, '1014-01-01'),
          (2, 'infinity', NULL, NULL, NULL),
          (3, '0001-01-01', NULL, NULL, NULL),
          (4, '2030-05-05', '2101-01-01', 'infinity', NULL),
          (5, '-infinity', '1899-12-31', NULL, NULL),
          (6, NULL, NULL, NULL, NULL)
        """
    )
    await conn.execute(
        """
        INSERT INTO documents (id, client_id, document_type, expiry_date, created_at, deleted_at)
        VALUES
          (10, 1, 'passport', '2027-01-01', '2026-01-01', NULL),
          (11, 1, 'passport', '2028-02-02', '2026-06-01', NULL),
          (12, 1, 'passport', '2029-03-03', '2026-09-01', now()),
          (13, 1, 'passport', 'infinity',   '2026-10-01', NULL),
          (14, 1, 'kitas',    '2031-01-01', '2026-10-02', NULL),
          (20, 2, 'passport', '2028-08-08', '2026-01-01', NULL),
          (30, 3, 'passport', NULL,         '2026-01-01', NULL),
          (31, 3, 'passport', '2150-01-01', '2026-01-02', NULL),
          (40, 4, 'passport', '2040-04-04', '2026-01-01', NULL),
          (90, 6, 'other',    '-infinity',  '2026-01-01', NULL),
          (91, 6, 'other',    '2035-05-05', '2026-01-01', NULL)
        """
    )


async def test_repair_by_rule(db: asyncpg.Connection) -> None:
    await _seed(db)
    await db.execute(_forward())
    got = {
        r["id"]: r
        for r in await db.fetch(
            "SELECT id, passport_expiry, visa_expiry_date, kitas_expiry_date, date_of_birth "
            "FROM clients"
        )
    }
    # most recent in-range, non-deleted passport document wins
    assert got[1]["passport_expiry"] == date(2028, 2, 2)
    assert got[2]["passport_expiry"] == date(2028, 8, 8)
    # no usable passport document: NULL
    assert got[3]["passport_expiry"] is None
    assert got[5]["passport_expiry"] is None
    # an in-range value is never touched, even when a document disagrees
    assert got[4]["passport_expiry"] == date(2030, 5, 5)
    # no source for the other columns: NULL
    assert got[4]["visa_expiry_date"] is None
    assert got[4]["kitas_expiry_date"] is None
    assert got[5]["visa_expiry_date"] is None
    # date_of_birth is out of scope and untouched
    assert got[1]["date_of_birth"] == date(1014, 1, 1)
    docs = {r["id"]: r["expiry_date"] for r in await db.fetch("SELECT id, expiry_date FROM documents")}
    assert docs[13] is None and docs[90] is None and docs[31] is None
    assert docs[11] == date(2028, 2, 2) and docs[91] == date(2035, 5, 5)


async def test_constraints_exist_validated_and_refuse_non_finite(db: asyncpg.Connection) -> None:
    await _seed(db)
    await db.execute(_forward())
    rows = await db.fetch(
        "SELECT conname, convalidated FROM pg_constraint "
        "WHERE contype = 'c' AND conname = ANY($1::text[])",
        sorted(CONSTRAINTS),
    )
    assert {r["conname"] for r in rows} == CONSTRAINTS
    assert all(r["convalidated"] for r in rows)
    for column in ("passport_expiry", "visa_expiry_date", "kitas_expiry_date"):
        for bad in ("-infinity", "infinity", "1899-12-31", "2101-01-01"):
            with pytest.raises(asyncpg.CheckViolationError):
                await db.execute(f"INSERT INTO clients (id, {column}) VALUES (100, '{bad}')")
    for bad in ("-infinity", "infinity", "1899-12-31", "2101-01-01"):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.execute(f"INSERT INTO documents (id, expiry_date) VALUES (100, '{bad}')")
    # boundaries and NULL stay legal
    await db.execute("INSERT INTO clients (id, passport_expiry) VALUES (101, '1900-01-01')")
    await db.execute("INSERT INTO clients (id, passport_expiry) VALUES (102, '2100-12-31')")
    await db.execute("INSERT INTO clients (id, passport_expiry) VALUES (103, NULL)")


async def test_forward_twice_is_idempotent(db: asyncpg.Connection) -> None:
    await _seed(db)
    await db.execute(_forward())
    await db.execute(_forward())
    count = await db.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conname = ANY($1::text[])", sorted(CONSTRAINTS)
    )
    assert count == 4


async def test_skips_when_tables_absent(db: asyncpg.Connection) -> None:
    await db.execute("DROP TABLE documents; DROP TABLE clients;")
    await db.execute(_forward())


def test_split_forward_and_rollback_of_real_file() -> None:
    full = (MIGRATIONS_DIR / MIGRATION).read_text(encoding="utf-8")
    forward, rollback = split_migration_sql(full)
    assert "VALIDATE CONSTRAINT" in forward and "NOT VALID" in forward
    assert "DROP CONSTRAINT" not in forward
    assert rollback.count("DROP CONSTRAINT IF EXISTS") == 4
    assert "UPDATE" not in rollback


async def test_ci_shaped_clients_without_visa_and_kitas_columns(db: asyncpg.Connection) -> None:
    """The CI bootstrap schema has no visa/kitas columns on clients: guard per column."""
    await db.execute("ALTER TABLE clients DROP COLUMN visa_expiry_date")
    await db.execute("ALTER TABLE clients DROP COLUMN kitas_expiry_date")
    await db.execute("INSERT INTO clients (id, passport_expiry) VALUES (1, '-infinity')")
    await db.execute(
        "INSERT INTO documents (id, client_id, document_type, expiry_date) "
        "VALUES (10, 1, 'passport', '2028-02-02')"
    )
    await db.execute(_forward())
    assert await db.fetchval("SELECT passport_expiry FROM clients WHERE id = 1") == date(2028, 2, 2)
    names = {
        r["conname"]
        for r in await db.fetch(
            "SELECT conname FROM pg_constraint WHERE conname = ANY($1::text[]) AND convalidated",
            sorted(CONSTRAINTS),
        )
    }
    assert names == {"clients_passport_expiry_range_chk", "documents_expiry_date_range_chk"}
