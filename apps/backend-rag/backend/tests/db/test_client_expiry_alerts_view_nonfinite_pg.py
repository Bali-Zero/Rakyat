"""client_expiry_alerts_view must survive a non-finite date (real PostgreSQL).

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it).
Synthetic data only. The old definition (the migration's ROLLBACK section) is the
control: it must raise on a -infinity row; migration 352 must not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")

from backend.db.migration_base import split_migration_sql  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.database]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations_v2"
    / "352_client_expiry_alerts_view_finite_dates.sql"
)
COLUMNS = [
    "entity_type", "entity_id", "entity_name", "client_id", "client_name",
    "document_type", "expiry_date", "days_until_expiry", "alert_color", "assigned_to",
]
SCHEMA = """
CREATE TABLE clients (id int PRIMARY KEY, full_name text, status text,
    passport_expiry date, assigned_to text);
CREATE TABLE client_family_members (id int PRIMARY KEY, client_id int, full_name text,
    passport_expiry date, visa_expiry date);
CREATE TABLE documents (id int PRIMARY KEY, client_id int, document_type text,
    expiry_date date, status text, is_archived boolean);
"""


@pytest_asyncio.fixture
async def conn():
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not dsn or not os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID"):
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    c = await asyncpg.connect(dsn)
    schema = "expiry_view_test"
    await c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE; CREATE SCHEMA {schema}")
    await c.execute(f"SET search_path = {schema}")
    await c.execute(SCHEMA)
    await c.execute(
        "INSERT INTO clients VALUES (1,'A','active',CURRENT_DATE+30,'x'),"
        "(2,'B','active','-infinity','x'),(3,'C','active','infinity','x')"
    )
    await c.execute(
        "INSERT INTO client_family_members VALUES (1,1,'F','-infinity','infinity'),"
        "(2,1,'G',CURRENT_DATE+10,CURRENT_DATE+20)"
    )
    await c.execute(
        "INSERT INTO documents VALUES (1,1,'kitas','-infinity','ok',false),"
        "(2,1,'kitas',CURRENT_DATE+5,'ok',false)"
    )
    try:
        yield c
    finally:
        await c.execute(f"DROP SCHEMA {schema} CASCADE")
        await c.close()


@pytest.mark.asyncio
async def test_old_definition_fails_and_new_one_survives(conn):
    forward, rollback = split_migration_sql(MIGRATION.read_text())
    assert rollback

    await conn.execute(rollback)  # the pre-352 definition
    with pytest.raises(asyncpg.DataError):
        await conn.fetch("SELECT * FROM client_expiry_alerts_view")

    await conn.execute(forward)
    rows = await conn.fetch("SELECT * FROM client_expiry_alerts_view")
    assert list(rows[0].keys()) == COLUMNS
    assert all(r["days_until_expiry"] is not None for r in rows)
    # finite rows only: client passport, family passport+visa, document
    assert sorted((r["entity_type"], r["document_type"]) for r in rows) == [
        ("client", "passport"),
        ("document", "kitas"),
        ("family_member", "passport"),
        ("family_member", "visa"),
    ]
