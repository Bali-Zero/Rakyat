"""Migration 359 (security_audit_log promoted to v2): SQL contract, idempotence, real writer path.

The text tests run everywhere. The integration test builds its own throwaway DATABASE (the
migration names `public.` explicitly, so a schema would not isolate it) from the server behind
TEST_DATABASE_URL, applies the migration twice and writes one row through
SecurityAuditService.log_event, the real insert path of the portal impersonation audit and the auth
revoke-all route. It never skips on pre-existing state and never touches the target database: only
the throwaway database it created is dropped. All values are synthetic.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql
from backend.services.security.audit_service import SecurityAuditService

MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "359_security_audit_log.sql"
)
LEGACY = Path(__file__).resolve().parents[2] / "migrations" / "migration_090_security_audit_log.py"

COLUMNS = (
    "id BIGSERIAL PRIMARY KEY",
    "user_id VARCHAR(255)",
    "user_email VARCHAR(255)",
    "action VARCHAR(100) NOT NULL",
    "resource_type VARCHAR(100)",
    "resource_id VARCHAR(255)",
    "ip_address INET",
    "user_agent TEXT",
    "details JSONB",
    "success BOOLEAN NOT NULL DEFAULT TRUE",
    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
)
INDEXES = ("idx_security_audit_user", "idx_security_audit_action", "idx_security_audit_created")


def _parts() -> tuple[str, str]:
    forward, rollback = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
    assert rollback is not None
    return forward, rollback


def test_forward_sql_mirrors_legacy_090_and_is_non_destructive() -> None:
    forward, rollback = _parts()
    legacy = LEGACY.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.security_audit_log" in forward
    for column in COLUMNS:
        assert column in forward
        assert column in legacy
    for index in INDEXES:
        assert f"CREATE INDEX IF NOT EXISTS {index}" in forward
        assert index in legacy
    sql_only = "\n".join(
        line for line in forward.splitlines() if not line.lstrip().startswith("--")
    ).upper()
    for word in ("DROP", "GRANT", "INSERT", "DELETE", "UPDATE", "TRUNCATE"):
        assert word not in sql_only
    # Rollback is a deliberate no-op: an audit trail is never dropped by a generic rollback.
    rollback_sql = "\n".join(
        line for line in rollback.splitlines() if not line.lstrip().startswith("--")
    ).strip()
    assert rollback_sql == "SELECT 1;"


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


def _dsn_for(dbname: str) -> str:
    parts = urlsplit(_dsn())
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """A connection to a brand-new throwaway database; only that database is dropped."""
    name = f"mig359_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_dsn())
    c: asyncpg.Connection | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        c = await asyncpg.connect(_dsn_for(name))
        # Same jsonb codec the application pool registers (encoder=json.dumps).
        await c.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        yield c
    finally:
        if c is not None:
            await c.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_twice_then_write_through_audit_service(conn: asyncpg.Connection) -> None:
    assert await conn.fetchval("SELECT to_regclass('public.security_audit_log')") is None

    forward, _ = _parts()
    await conn.execute(forward)
    await conn.execute(forward)  # second apply is a no-op

    assert await conn.fetchval("SELECT to_regclass('public.security_audit_log')") is not None
    indexes = {
        r["indexname"]
        for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'security_audit_log'"
        )
    }
    assert set(INDEXES) <= indexes

    await SecurityAuditService().log_event(
        conn=conn,
        action="portal_impersonation",
        user_email="fake.actor@example.test",
        user_id="u-synthetic-1",
        resource_type="client",
        resource_id="42",
        ip_address="192.0.2.7",
        user_agent="synthetic-agent",
        success=True,
        details={"path": "/api/portal/me"},
    )
    rows = await conn.fetch("SELECT * FROM public.security_audit_log")
    assert len(rows) == 1  # the writer swallows errors, so a missing row would show here
    row = rows[0]
    assert row["action"] == "portal_impersonation"
    assert row["user_email"] == "fake.actor@example.test"
    assert row["success"] is True
    assert str(row["ip_address"]) == "192.0.2.7"
    assert row["created_at"] is not None
    assert json.loads(
        await conn.fetchval("SELECT details::text FROM public.security_audit_log")
    ) == {"path": "/api/portal/me"}
