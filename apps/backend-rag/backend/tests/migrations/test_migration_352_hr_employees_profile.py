"""Migration 352 and the card write paths, against a real Postgres.

Collected under backend/tests/ and run by the `Backend Shard N` jobs against their
postgres service, like the other tests in this directory (no skip, no xfail). The
test works in its own schema (`hrcard352`) so it never touches shared tables.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql

pytestmark = pytest.mark.integration

SCHEMA = "hrcard352"
MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "352_hr_employees_profile.sql"
)

_HR_DDL = """
CREATE TABLE team_members (
    id VARCHAR(36) PRIMARY KEY, full_name TEXT, email TEXT, role TEXT, department TEXT);
CREATE TABLE hr_employees (
    id SERIAL PRIMARY KEY,
    team_member_id VARCHAR(36) NOT NULL UNIQUE REFERENCES team_members(id) ON DELETE RESTRICT,
    hire_date DATE NOT NULL,
    base_salary_idr BIGINT NOT NULL CHECK (base_salary_idr >= 0),
    bank_name VARCHAR(100), bank_account VARCHAR(100), bank_account_holder VARCHAR(255),
    npwp VARCHAR(32), ptkp_status VARCHAR(10) NOT NULL DEFAULT 'TK/0',
    bpjs_kesehatan_number VARCHAR(32), bpjs_ketenagakerjaan_number VARCHAR(32),
    is_active BOOLEAN NOT NULL DEFAULT TRUE, notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
"""


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


def _forward() -> str:
    return split_migration_sql(MIGRATION.read_text(encoding="utf-8"))[0]


def _rollback() -> str:
    rollback = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))[1]
    assert rollback, "352 must carry a rollback section"
    return rollback


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    admin = await asyncpg.connect(_dsn())
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('r2-migration-352-hr-card-test'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        p = await asyncpg.create_pool(
            _dsn(), server_settings={"search_path": SCHEMA}, min_size=1, max_size=3
        )
        try:
            async with p.acquire() as conn:
                await conn.execute(_HR_DDL)
                await conn.execute(_forward())
                await conn.execute(
                    "INSERT INTO team_members VALUES "
                    "('tm-a','Fake A','fake.a@balizero.com','staff','ops'),"
                    "('tm-b','Fake B','fake.b@balizero.com','staff','ops'),"
                    "('tm-c','Fake C','fake.c@balizero.com','client','ops')"
                )
                await conn.execute(
                    "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, npwp) "
                    "VALUES ('tm-a', '2025-01-01', 5, '10.01')"
                )
            yield p
        finally:
            await p.close()
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.execute("SELECT pg_advisory_unlock(hashtext('r2-migration-352-hr-card-test'))")
        await admin.close()


async def _profile(pool: asyncpg.Pool, member: str) -> Any:
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT profile::text FROM hr_employees WHERE team_member_id = $1", member
        )
    return json.loads(raw)


@pytest.mark.asyncio
async def test_migration_adds_the_column_and_rolls_back(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_forward())  # idempotent re-apply
        assert await conn.fetchval("SELECT profile::text FROM hr_employees LIMIT 1") == "{}"
        assert await conn.fetchval(
            "SELECT col_description('hr_employees'::regclass, attnum) IS NOT NULL "
            "FROM pg_attribute WHERE attrelid = 'hr_employees'::regclass AND attname = 'profile'"
        )
        await conn.execute(_rollback())
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'hr_employees'::regclass "
            "AND attname = 'profile' AND NOT attisdropped)"
        )


@pytest.mark.asyncio
async def test_migration_fails_loudly_when_hr_employees_is_absent(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE hr_employees")
        with pytest.raises(asyncpg.UndefinedTableError):
            await conn.execute(_forward())


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["null", '"x"', "[]", "[1,2]", "7"])
async def test_nested_merge_survives_non_object_stored_values(
    pool: asyncpg.Pool, stored: str
) -> None:
    from backend.app.services.hr.hr_service import HRService

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = jsonb_build_object('docs', $1::jsonb, 'contract', $1::jsonb, 'unit', 'u0') "
            "WHERE team_member_id = 'tm-a'",
            stored,
        )
    service = HRService(pool)
    await service.merge_card(
        "tm-a", {}, {"docs": {"ktp": "f1"}, "contract": {"number": "C1"}, "nik": "1.2"}
    )
    profile = await _profile(pool, "tm-a")
    assert profile == {
        "unit": "u0",
        "docs": {"ktp": "f1"},
        "contract": {"number": "C1"},
        "nik": "1.2",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["null", '"x"', "[]"])
async def test_a_non_object_profile_is_normalised_before_the_merge(
    pool: asyncpg.Pool, stored: str
) -> None:
    from backend.app.services.hr.hr_service import HRService

    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE hr_employees ALTER COLUMN profile DROP NOT NULL")
        await conn.execute("UPDATE hr_employees SET profile = $1::jsonb", stored)
    await HRService(pool).merge_card("tm-a", {}, {"unit": "u1", "docs": {"a": "f"}})
    assert await _profile(pool, "tm-a") == {"unit": "u1", "docs": {"a": "f"}}


@pytest.mark.asyncio
async def test_merge_with_real_dates_keeps_omitted_columns_and_inserts_a_missing_row(
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    await service.merge_card("tm-a", {"hire_date": date(2024, 6, 1), "bank_name": "Fake Bank"}, {})
    inserted = await service.merge_card("tm-b", {"hire_date": date(2025, 2, 1)}, {"unit": "ub"})
    assert inserted is not None
    async with pool.acquire() as conn:
        a = await conn.fetchrow("SELECT * FROM hr_employees WHERE team_member_id = 'tm-a'")
        b = await conn.fetchrow("SELECT * FROM hr_employees WHERE team_member_id = 'tm-b'")
    assert a["hire_date"] == date(2024, 6, 1) and a["npwp"] == "10.01" and a["base_salary_idr"] == 5
    assert b["hire_date"] == date(2025, 2, 1) and b["base_salary_idr"] == 0
    card = await service.get_card_by_email("fake.b@balizero.com")
    assert card is not None and card["profile"] == {"unit": "ub"}


@pytest.mark.asyncio
async def test_import_apply_is_atomic_when_a_later_card_fails(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRCardError, HRService
    from backend.scripts import import_staff_cards as imp

    document = {
        "cards": [
            {
                "email": "fake.a@balizero.com",
                "fields": {"bank_name": "Changed"},
                "profile": {"unit": "u9"},
            },
            {"email": "fake.b@balizero.com", "fields": {"hire_date": "2025-02-01"}, "profile": {}},
        ]
    }
    original = HRService.merge_card_on
    calls = {"n": 0}

    async def flaky(self: Any, conn: Any, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise HRCardError("db_Boom")
        return await original(self, conn, *args, **kwargs)

    with patch.object(HRService, "merge_card_on", flaky):
        async with pool.acquire() as conn:
            with pytest.raises(HRCardError):
                await imp.run(conn, document, apply=True)
    assert calls["n"] == 2
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT bank_name FROM hr_employees WHERE team_member_id = 'tm-a'")
            is None
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_employees WHERE team_member_id = 'tm-b'")
            == 0
        )
    assert await _profile(pool, "tm-a") == {}


@pytest.mark.asyncio
async def test_import_apply_creates_a_card_with_a_hire_date(pool: asyncpg.Pool) -> None:
    from backend.scripts import import_staff_cards as imp

    document = {
        "cards": [
            {
                "email": "fake.b@balizero.com",
                "fields": {"hire_date": "2025-02-01"},
                "profile": {"unit": "ub"},
            },
            {"email": "fake.c@balizero.com", "fields": {"hire_date": "2025-02-01"}, "profile": {}},
        ]
    }
    async with pool.acquire() as conn:  # fake.c is a client: not a staff member -> not found
        counts, ok = await imp.run(conn, document, apply=True)
        assert ok is False and counts["not_found"] == 1 and counts["blocked:not_found"] == 1
        counts, ok = await imp.run(conn, document, apply=True, skip_not_found=True)
        assert ok is True and counts["would_insert"] == 1
        assert await conn.fetchval(
            "SELECT hire_date FROM hr_employees WHERE team_member_id = 'tm-b'"
        ) == date(2025, 2, 1)
