"""Migration 351: backend_rag_v2 must hold SELECT/INSERT/UPDATE on conversations
and USAGE on its id sequence, or the deploy fails loudly.

Contract tests read the file. The real-PostgreSQL cases execute its forward
section in a scratch database, as the production-shaped non-owner role and as the
table owner. Roles are cluster-global, so every role is created INSIDE a
transaction that is rolled back, the scratch database is dropped afterwards, and
the cases skip when a real backend_rag_v2 already exists in the cluster or the
connection is not a superuser.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from backend.db.migration_base import split_migration_sql
from backend.db import schema_audit
from backend.db.migration_manager import _extract_rollback_sql

MIGRATION_FILE = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations_v2"
    / "351_conversations_runtime_insert_assert.sql"
)
RUNTIME_ROLE = "backend_rag_v2"
TABLE = "public.conversations"
SEQUENCE = "public.conversations_id_seq"
ADMIN_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
)


def _forward() -> str:
    return split_migration_sql(MIGRATION_FILE.read_text(encoding="utf-8"))[0]


def _code(sql: str) -> str:
    """Executable lines only: the header prose discusses DELETE on purpose."""
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))


# ------------------------------------------------------------------- contract


def test_migration_351_file_exists_with_rollback_marker() -> None:
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    assert MIGRATION_FILE.exists()
    assert "-- === ROLLBACK ===" in sql
    assert _extract_rollback_sql(sql) is not None


def test_migration_351_is_guarded_on_role_and_table() -> None:
    forward = _forward()
    assert "runtime_role constant text := 'backend_rag_v2'" in forward
    assert "pg_roles WHERE rolname = runtime_role" in forward
    assert "to_regclass(object_name)" in forward
    assert "IF object_reg IS NULL THEN" in forward


def test_migration_351_probes_superuser_before_asserting() -> None:
    forward = _forward()
    assert "rolsuper" in forward
    assert forward.index("rolsuper") < forward.index("has_table_privilege")


def test_migration_351_checks_each_privilege_and_names_the_manual_statement() -> None:
    forward = _forward()
    for priv in ("SELECT", "INSERT", "UPDATE"):
        assert f"has_table_privilege(runtime_role, object_reg, '{priv}')" in forward
        assert f"GRANT {priv} ON TABLE % TO backend_rag_v2" in forward
    assert "has_sequence_privilege(runtime_role, sequence_reg, 'USAGE')" in forward
    assert "GRANT USAGE ON SEQUENCE % TO backend_rag_v2" in forward
    assert "WHEN insufficient_privilege THEN" in forward


def test_migration_351_guards_the_serial_sequence_lookup_on_the_id_column() -> None:
    code = _code(_forward())
    assert "attname = 'id'" in code
    assert code.index("attname = 'id'") < code.index("pg_get_serial_sequence")


def test_migration_351_never_grants_or_asserts_delete() -> None:
    assert "DELETE" not in _code(_forward()).upper()
    assert "DELETE" not in _code(_extract_rollback_sql(MIGRATION_FILE.read_text()) or "").upper()


def test_migration_351_rollback_revokes_nothing() -> None:
    rollback = _extract_rollback_sql(MIGRATION_FILE.read_text(encoding="utf-8"))
    assert rollback is not None
    assert "REVOKE" not in _code(rollback).upper()


# ------------------------------------------------------------- real PostgreSQL


@dataclass
class World:
    conn: asyncpg.Connection
    owner: str

    async def build(
        self,
        *,
        role: bool = True,
        table: bool = True,
        privs: tuple[str, ...] = ("SELECT", "INSERT", "UPDATE"),
        seq_usage: bool = True,
    ) -> None:
        c = self.conn
        if role:
            await c.execute(f'CREATE ROLE "{RUNTIME_ROLE}" NOSUPERUSER NOLOGIN')
        if not table:
            return
        await c.execute(
            "CREATE TABLE public.conversations (id serial PRIMARY KEY, "
            "user_id varchar(255) NOT NULL, session_id varchar(255), messages jsonb)"
        )
        await c.execute(f'ALTER TABLE public.conversations OWNER TO "{self.owner}"')
        # Premise: the owned sequence follows the table owner, like production.
        assert await c.fetchval(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = $1::regclass", SEQUENCE
        ) == self.owner
        if role and privs:
            await c.execute(f'GRANT {", ".join(privs)} ON public.conversations TO "{RUNTIME_ROLE}"')
        if role and seq_usage:
            await c.execute(f'GRANT USAGE ON SEQUENCE public.conversations_id_seq TO "{RUNTIME_ROLE}"')

    async def has(self, priv: str) -> bool:
        return await self.conn.fetchval(
            "SELECT has_table_privilege($1, $2, $3)", RUNTIME_ROLE, TABLE, priv
        )

    async def seq_usage(self) -> bool:
        return await self.conn.fetchval(
            "SELECT has_sequence_privilege($1, $2, 'USAGE')", RUNTIME_ROLE, SEQUENCE
        )

    async def acls(self) -> tuple[str | None, str | None]:
        return (
            await self.conn.fetchval(
                "SELECT relacl::text FROM pg_class WHERE oid = $1::regclass", TABLE
            ),
            await self.conn.fetchval(
                "SELECT relacl::text FROM pg_class WHERE oid = $1::regclass", SEQUENCE
            ),
        )

    async def apply(self, as_role: str | None) -> asyncpg.PostgresError | None:
        """Run the forward section under `as_role`; a failure rolls back to a savepoint."""
        error: asyncpg.PostgresError | None = None
        try:
            async with self.conn.transaction():
                if as_role:
                    await self.conn.execute(f'SET ROLE "{as_role}"')
                await self.conn.execute(_forward())
        except asyncpg.PostgresError as exc:
            error = exc
        finally:
            await self.conn.execute("RESET ROLE")
        return error


@pytest_asyncio.fixture
async def world() -> AsyncIterator[World]:
    try:
        admin = await asyncpg.connect(ADMIN_DSN)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no admin Postgres: {exc}")
    scratch = f"t351_{uuid.uuid4().hex[:12]}"
    conn: asyncpg.Connection | None = None
    try:
        if not await admin.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
            pytest.skip("needs a superuser session to build a scratch database")
        if await admin.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = $1)", RUNTIME_ROLE):
            pytest.skip(f"a real {RUNTIME_ROLE} exists in this cluster; roles are cluster-global")
        await admin.execute(f'CREATE DATABASE "{scratch}" TEMPLATE template0')
        conn = await asyncpg.connect(ADMIN_DSN, database=scratch)
        tx = conn.transaction()
        await tx.start()
        owner = f"t351_owner_{uuid.uuid4().hex[:8]}"
        await conn.execute(f'CREATE ROLE "{owner}" NOSUPERUSER NOLOGIN')
        try:
            yield World(conn, owner)
        finally:
            await tx.rollback()  # every role created above vanishes with it
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        leftovers = await admin.fetchval(
            "SELECT count(*) FROM pg_roles WHERE rolname = $1 OR rolname LIKE 't351_owner_%'",
            RUNTIME_ROLE,
        )
        await admin.close()
        assert leftovers == 0, "scratch roles leaked into the cluster"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_privileges_present_non_owner_passes_and_changes_nothing(world: World) -> None:
    await world.build()
    assert await world.conn.fetchval(
        "SELECT NOT rolsuper FROM pg_roles WHERE rolname = $1", RUNTIME_ROLE
    )
    before = await world.acls()

    assert await world.apply(RUNTIME_ROLE) is None

    assert await world.acls() == before
    assert [await world.has(p) for p in ("SELECT", "INSERT", "UPDATE")] == [True, True, True]
    assert await world.has("DELETE") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_insert_non_owner_raises_and_stays_missing(world: World) -> None:
    await world.build(privs=("SELECT", "UPDATE"))

    error = await world.apply(RUNTIME_ROLE)

    assert error is not None and error.sqlstate == "P0001"
    assert "missing INSERT" in error.message
    assert "GRANT INSERT ON TABLE public.conversations TO backend_rag_v2" in error.message
    assert await world.has("INSERT") is False
    assert await world.has("DELETE") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_sequence_usage_non_owner_raises(world: World) -> None:
    await world.build(seq_usage=False)

    error = await world.apply(RUNTIME_ROLE)

    assert error is not None and error.sqlstate == "P0001"
    assert "missing USAGE on sequence" in error.message
    assert "GRANT USAGE ON SEQUENCE public.conversations_id_seq TO backend_rag_v2" in error.message
    assert await world.seq_usage() is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superuser_runtime_role_raises(world: World) -> None:
    await world.build()
    await world.conn.execute(f'ALTER ROLE "{RUNTIME_ROLE}" SUPERUSER')

    error = await world.apply(None)

    assert error is not None and error.sqlstate == "P0001"
    assert "superuser" in error.message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_insert_run_by_the_table_owner_is_granted(world: World) -> None:
    await world.build(privs=("SELECT", "UPDATE"), seq_usage=False)
    assert await world.has("INSERT") is False

    assert await world.apply(world.owner) is None

    assert await world.has("INSERT") is True
    assert await world.seq_usage() is True
    assert await world.has("DELETE") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_role_is_a_noop(world: World) -> None:
    await world.build(role=False)
    before = await world.acls()

    assert await world.apply(None) is None
    assert await world.acls() == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_table_is_a_noop(world: World) -> None:
    await world.build(table=False)

    assert await world.apply(RUNTIME_ROLE) is None


# ----------------------------------------------- schema_audit (every release)


class _Acquire:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self.conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Pool:
    """Minimal pool whose acquire() yields the uncommitted scratch connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


async def _audit(world: World) -> list[schema_audit.Finding]:
    return await schema_audit._check_conversations_runtime_privileges(_Pool(world.conn))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_all_privileges_present_has_no_finding(world: World) -> None:
    await world.build()
    assert await _audit(world) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_missing_insert_is_an_error_naming_insert(world: World) -> None:
    await world.build(privs=("SELECT", "UPDATE"))

    [finding] = await _audit(world)

    assert finding.severity == "error"
    assert finding.code == "conversations_runtime_privilege_missing"
    assert finding.details["missing"] == ["INSERT on public.conversations"]
    assert finding.details["manual_grants"] == [
        "GRANT INSERT ON TABLE public.conversations TO backend_rag_v2"
    ]
    assert "INSERT" in finding.message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_missing_sequence_usage_is_an_error(world: World) -> None:
    await world.build(seq_usage=False)

    [finding] = await _audit(world)

    assert finding.code == "conversations_runtime_privilege_missing"
    assert finding.details["manual_grants"] == [
        "GRANT USAGE ON SEQUENCE public.conversations_id_seq TO backend_rag_v2"
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_superuser_role_is_an_error(world: World) -> None:
    await world.build()
    await world.conn.execute(f'ALTER ROLE "{RUNTIME_ROLE}" SUPERUSER')

    [finding] = await _audit(world)

    assert finding.code == "runtime_role_superuser"
    assert finding.severity == "error"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_absent_role_and_absent_table_have_no_finding(world: World) -> None:
    await world.build(role=False)
    assert await _audit(world) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_absent_table_has_no_finding(world: World) -> None:
    await world.build(table=False)
    assert await _audit(world) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_delete_is_neither_required_nor_forbidden(world: World) -> None:
    await world.build(privs=("SELECT", "INSERT", "UPDATE", "DELETE"))
    assert await _audit(world) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_error_finding_makes_the_release_command_exit_1(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real finding from the real check, fed through the real `_amain` (run_audit
    is the only stub: it needs a live MigrationManager)."""
    await world.build(privs=("SELECT", "UPDATE"))
    findings = await _audit(world)

    async def fake_run_audit(**_kw: object) -> schema_audit.AuditReport:
        return schema_audit.AuditReport(
            checks_run=["conversations_runtime_privileges"], findings=findings
        )

    monkeypatch.setattr(schema_audit, "run_audit", fake_run_audit)

    assert await schema_audit._amain(["--database-url", "postgres://fake"]) == 1
