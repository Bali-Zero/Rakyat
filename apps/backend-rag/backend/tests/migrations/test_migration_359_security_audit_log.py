"""Migration 359 (security_audit_log promoted to v2): SQL contract, idempotence, real writer path.

The text tests run everywhere. The integration test works in its own schema (`secaudit359`)
against TEST_DATABASE_URL like the other tests in this directory, applies the migration twice and
writes one row through SecurityAuditService.log_event, the real insert path of the portal
impersonation audit and the auth revoke-all route. All values are synthetic.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql
from backend.services.security.audit_service import SecurityAuditService

MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "359_security_audit_log.sql"
)
LEGACY = Path(__file__).resolve().parents[2] / "migrations" / "migration_090_security_audit_log.py"
SCHEMA = "secaudit359"

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


async def _init_codecs(conn: asyncpg.Connection) -> None:
    # Same jsonb codec the application pool registers (encoder=json.dumps).
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(_dsn())
    try:
        await c.execute("SELECT pg_advisory_lock(hashtext('r2-migration-359-test'))")
        await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        # public stays on the path after the test schema: the migration names public.* explicitly,
        # so the table lands in public; the cleanup below removes it again.
        await c.execute(f"SET search_path TO {SCHEMA}, public")
        await _init_codecs(c)
        yield c
    finally:
        await c.execute("RESET search_path")
        await c.execute("DROP TABLE IF EXISTS public.security_audit_log")
        await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await c.execute("SELECT pg_advisory_unlock(hashtext('r2-migration-359-test'))")
        await c.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_twice_then_write_through_audit_service(conn: asyncpg.Connection) -> None:
    pre_existing = await conn.fetchval("SELECT to_regclass('public.security_audit_log')")
    if pre_existing is not None:
        pytest.skip("TEST_DATABASE_URL already has public.security_audit_log; not touching it")

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
