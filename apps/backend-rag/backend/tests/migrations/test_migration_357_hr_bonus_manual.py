"""Migration 357 (manual bonuses): SQL contract as text, and the real constraints on Postgres.

The text tests run everywhere. The integration tests work in their own schema
(`hrbonus357`) against TEST_DATABASE_URL like the other tests in this directory.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql

MIGRATION = Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "357_hr_bonus_manual.sql"
SCHEMA = "hrbonus357"


def _parts() -> tuple[str, str]:
    forward, rollback = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))[:2]
    return forward, rollback


def test_forward_sql_contract():
    forward, rollback = _parts()
    assert "ALTER COLUMN practice_id DROP NOT NULL" in forward
    for col in ("source", "reason", "created_by"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in forward
    assert "DEFAULT 'practice'" in forward
    assert "CHECK (source IN ('practice', 'manual'))" in forward
    assert "char_length(reason) <= 300" in forward
    assert "source = 'practice' AND practice_id IS NOT NULL" in forward
    assert "source = 'manual' AND practice_id IS NULL" in forward
    assert "length(btrim(reason)) > 0" in forward
    # The trigger function only validates a practice when there is one.
    assert "IF NEW.practice_id IS NULL THEN" in forward
    assert "CREATE OR REPLACE FUNCTION hr_validate_completed_practice" in forward
    assert "DROP TRIGGER" not in forward
    # Idempotent re-apply: every constraint is dropped before it is added.
    assert forward.count("DROP CONSTRAINT IF EXISTS") == 3
    assert forward.count("NOT VALID;") == forward.count("VALIDATE CONSTRAINT")
    # Rollback restores the old function and refuses to lose manual rows.
    assert rollback.index("SET NOT NULL") < rollback.index("DROP COLUMN IF EXISTS source")
    assert "IF NEW.practice_id IS NULL" not in rollback


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


_DDL = """
CREATE TABLE team_members (id VARCHAR(36) PRIMARY KEY, avatar TEXT);
CREATE TABLE practices (id SERIAL PRIMARY KEY, status TEXT);
CREATE TABLE hr_employees (id SERIAL PRIMARY KEY,
    team_member_id VARCHAR(36) REFERENCES team_members(id));
CREATE TABLE hr_bonus_ledger (
    id BIGSERIAL PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id) ON DELETE RESTRICT,
    employee_id INTEGER NOT NULL REFERENCES hr_employees(id) ON DELETE RESTRICT,
    practice_type_code VARCHAR(50) NOT NULL,
    amount_idr BIGINT NOT NULL CHECK (amount_idr >= 0),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    awarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_by VARCHAR(36), approved_at TIMESTAMPTZ, notes TEXT,
    CONSTRAINT uq_hr_bonus_practice_emp UNIQUE (practice_id, employee_id));
CREATE FUNCTION hr_validate_completed_practice() RETURNS TRIGGER AS $$
DECLARE v_status TEXT;
BEGIN
    SELECT status INTO v_status FROM practices WHERE id = NEW.practice_id;
    IF v_status IS NULL OR v_status <> 'completed' THEN
        RAISE EXCEPTION 'Practice % must be completed', NEW.practice_id;
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_hr_bonus_validate BEFORE INSERT OR UPDATE OF practice_id
    ON hr_bonus_ledger FOR EACH ROW EXECUTE FUNCTION hr_validate_completed_practice();
INSERT INTO team_members VALUES ('tm-1');
INSERT INTO hr_employees (team_member_id) VALUES ('tm-1');
INSERT INTO practices (status) VALUES ('completed'), ('open');
INSERT INTO hr_bonus_ledger (practice_id, employee_id, practice_type_code, amount_idr)
    VALUES (1, 1, 'visa_fake', 100);
"""


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(_dsn())
    try:
        await c.execute("SELECT pg_advisory_lock(hashtext('r2-migration-357-test'))")
        await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        await c.execute(f"SET search_path TO {SCHEMA}")
        await c.execute(_DDL)
        yield c
    finally:
        await c.execute("RESET search_path")
        await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await c.execute("SELECT pg_advisory_unlock(hashtext('r2-migration-357-test'))")
        await c.close()


_INSERT_MANUAL = (
    "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, source, reason) "
    "VALUES (1, 'manual', 50, 'manual', $1)"
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_constraints_trigger_and_idempotence(conn: asyncpg.Connection) -> None:
    forward, rollback = _parts()
    await conn.execute(forward)
    await conn.execute(forward)  # idempotent re-apply
    assert await conn.fetchval("SELECT source FROM hr_bonus_ledger WHERE id = 1") == "practice"

    await conn.execute(_INSERT_MANUAL, "Extra weekend filing")
    await conn.execute(_INSERT_MANUAL, "A second one for the same person")  # NULL never collides
    for bad in ("", "   "):
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(_INSERT_MANUAL, bad)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(_INSERT_MANUAL, "x" * 301)
    # A manual row cannot carry a practice, a practice row cannot omit it.
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO hr_bonus_ledger (practice_id, employee_id, practice_type_code, "
            "amount_idr, source, reason) VALUES (1, 1, 'm', 1, 'manual', 'why')"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr) "
            "VALUES (1, 'visa_fake', 1)"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, source) "
            "VALUES (1, 'x', 1, 'import')"
        )
    # The completed-practice rule still bites practice rows.
    with pytest.raises(asyncpg.RaiseError):
        await conn.execute(
            "INSERT INTO hr_bonus_ledger (practice_id, employee_id, practice_type_code, amount_idr) "
            "VALUES (2, 1, 'visa_fake', 1)"
        )

    # Rolling back with manual rows present is refused and loses nothing.
    with pytest.raises(asyncpg.NotNullViolationError):
        await conn.execute(rollback)
    assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE source = 'manual'") == 2
    await conn.execute("DELETE FROM hr_bonus_ledger WHERE source = 'manual'")
    await conn.execute(rollback)
    assert not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = 'hr_bonus_ledger' AND column_name = 'source')",
        SCHEMA,
    )


class _OneConnectionPool:
    """Just enough pool for HRService: acquire() yields the fixture's connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> _OneConnectionPool:
        return self

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_and_reject_split_404_from_409_in_one_statement(
    conn: asyncpg.Connection,
) -> None:
    from backend.app.services.hr.hr_service import HRService

    forward, _ = _parts()
    await conn.execute(forward)
    svc = HRService(_OneConnectionPool(conn))  # type: ignore[arg-type]
    ids = {}
    for name, status in (
        ("m_pending", "pending"),
        ("m_approved", "approved"),
        ("m_reject", "pending"),
    ):
        ids[name] = await conn.fetchval(
            "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, "
            "status, source, reason) VALUES (1, 'manual', 5, $1, 'manual', 'why') RETURNING id",
            status,
        )

    # DELETE: unknown -> missing; exists but approved, or a practice row -> conflict; else deleted.
    assert await svc.delete_manual_bonus(987654) == "missing"
    assert await svc.delete_manual_bonus(ids["m_approved"]) == "conflict"
    assert await svc.delete_manual_bonus(1) == "conflict"  # the practice bonus
    assert await svc.delete_manual_bonus(ids["m_pending"]) == "deleted"
    assert await svc.delete_manual_bonus(ids["m_pending"]) == "missing"  # now gone
    assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE id = 1") == 1

    # REJECT: unknown -> LookupError; not pending -> ValueError; pending -> rejected row.
    with pytest.raises(LookupError):
        await svc.reject_bonus(987654, "tm-1")
    with pytest.raises(ValueError):
        await svc.reject_bonus(ids["m_approved"], "tm-1")
    row = await svc.reject_bonus(ids["m_reject"], "tm-1", "dup")
    assert row["status"] == "rejected" and row["id"] == ids["m_reject"] and "_existed" not in row
    with pytest.raises(ValueError):
        await svc.reject_bonus(ids["m_reject"], "tm-1")
