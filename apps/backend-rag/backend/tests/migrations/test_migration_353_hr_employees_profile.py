"""Migration 353 and the card write paths, against a real Postgres.

Collected under backend/tests/ and run by the `Backend Shard N` jobs against their
postgres service, like the other tests in this directory (no skip, no xfail). The
test works in its own schema (`hrcard353`) so it never touches shared tables.
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
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

pytestmark = pytest.mark.integration

SCHEMA = "hrcard353"
MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "353_hr_employees_profile.sql"
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
    assert rollback, "353 must carry a rollback section"
    return rollback


async def _init(conn: asyncpg.Connection) -> None:
    """The production connection init (jsonb codec registration, S3)."""
    await init_prod_shaped_connection(conn)


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    admin = await asyncpg.connect(_dsn())
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('r2-migration-353-hr-card-test'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        p = await asyncpg.create_pool(
            _dsn(),
            init=_init,
            server_settings={"search_path": SCHEMA},  # survives the pool's RESET on release
            statement_cache_size=0,
            min_size=1,
            max_size=3,
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
        await admin.execute("SELECT pg_advisory_unlock(hashtext('r2-migration-353-hr-card-test'))")
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
            "UPDATE hr_employees SET profile = jsonb_build_object('docs', $1::text::jsonb, 'contract', $1::text::jsonb, 'unit', 'u0') "
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
        await conn.execute("UPDATE hr_employees SET profile = $1::text::jsonb", stored)
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
    from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService
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
            raise HRCardError(HRCardCode.DB_ERROR)
        return await original(self, conn, *args, **kwargs)

    with patch.object(HRService, "merge_card_on", flaky):
        async with pool.acquire() as conn:
            counts, ok = await imp.run(conn, document, apply=True)
    assert ok is False and counts["applied"] == 1 and counts["rejected:db_error"] == 1
    assert counts["committed:no"] == 1
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
        assert ok is True and counts["applied"] == 1 and counts["committed:yes"] == 1
        assert await conn.fetchval(
            "SELECT hire_date FROM hr_employees WHERE team_member_id = 'tm-b'"
        ) == date(2025, 2, 1)


# ─── S3 / S4 / S5 / S6 / S8, on real Postgres through the production codec ───


async def _typeof(pool: asyncpg.Pool, member: str) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT jsonb_typeof(profile) FROM hr_employees WHERE team_member_id = $1", member
        )


@pytest.mark.asyncio
async def test_two_patches_keep_profile_an_object_under_the_production_codec(
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_card_models import admin_card
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    async with pool.acquire() as conn:  # the pool really has the codec production has
        assert await conn.fetchval("SELECT $1::jsonb::text", {"a": 1}) == '{"a": 1}'
    # (iii) scalars only: profile must stay an object
    await service.merge_card("tm-a", {"bank_name": "Fake Bank"}, {})
    assert await _typeof(pool, "tm-a") == "object" and await _profile(pool, "tm-a") == {}
    await service.merge_card(
        "tm-a", {}, {"unit": "u1", "docs": {"ktp": "f1"}, "contract": {"number": "C1"}}
    )
    await service.merge_card(
        "tm-a", {"npwp": "9.9"}, {"docs": {"kk": "f2"}, "contract": {"file_id": "cf"}}
    )
    assert await _typeof(pool, "tm-a") == "object"
    assert await _profile(pool, "tm-a") == {
        "unit": "u1",
        "docs": {"ktp": "f1", "kk": "f2"},
        "contract": {"number": "C1", "file_id": "cf"},
    }
    card = await service.patch_card(1, {"bank_name": "B2"}, {"unit": "u2"}, admin_card)
    assert card.unit == "u2" and card.bank_name == "B2" and card.docs == {"ktp": "f1", "kk": "f2"}
    assert await _typeof(pool, "tm-a") == "object"


@pytest.mark.asyncio
async def test_a_non_object_incoming_part_is_rejected_and_nothing_changes(
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService

    service = HRService(pool)
    await service.merge_card("tm-a", {}, {"unit": "keep"})
    for bad in ({"docs": ["x"]}, {"contract": "x"}, {"docs": None}):
        with pytest.raises(HRCardError) as exc:
            await service.merge_card("tm-a", {"bank_name": "ShouldNotLand"}, bad)
        assert exc.value.code is HRCardCode.PROFILE_NOT_OBJECT
    with pytest.raises(HRCardError):
        await service.merge_card("tm-a", {}, ["x"])  # type: ignore[arg-type]
    assert await _profile(pool, "tm-a") == {"unit": "keep"}
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None


@pytest.mark.asyncio
async def test_a_profile_that_would_not_be_an_object_after_the_merge_rolls_back(
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService

    service = HRService(pool)
    broken = HRService._CARD_UPDATE_SQL.replace(
        "updated_at = NOW()", "updated_at = NOW(), notes = 'ShouldRollBack'"
    ).replace("|| $11::jsonb\n", "|| '[1]'::jsonb\n", 1)
    assert broken != HRService._CARD_UPDATE_SQL
    with patch.object(HRService, "_CARD_UPDATE_SQL", broken):
        with pytest.raises(HRCardError) as exc:
            await service.merge_card("tm-a", {}, {"unit": "u"})
    assert exc.value.code is HRCardCode.PROFILE_NOT_OBJECT
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT notes FROM hr_employees WHERE id = 1") is None
    assert await _typeof(pool, "tm-a") == "object"


@pytest.mark.asyncio
async def test_patch_card_rolls_back_when_the_view_fails(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService

    def boom(card: dict[str, Any]) -> Any:
        raise RuntimeError("secret-looking " + str(card.get("npwp")))

    with pytest.raises(HRCardError) as exc:
        await HRService(pool).patch_card(1, {"bank_name": "ShouldRollBack"}, {"unit": "x"}, boom)
    assert exc.value.code is HRCardCode.DB_ERROR and "secret" not in str(exc.value)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None
    assert await _profile(pool, "tm-a") == {}
    assert await HRService(pool).patch_card(999, {}, {}, lambda c: c) is None


@pytest.mark.asyncio
async def test_a_concurrent_insert_falls_through_to_the_update(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRService

    racing_insert = """
        WITH ins AS (
            INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr)
            VALUES ($1, '2020-01-01', 0) RETURNING id)
        SELECT NULL::int AS id, NULL::text AS profile_type
        WHERE (SELECT count(*) FROM ins) < 0 AND $2::date IS NOT NULL AND $3::text IS NULL
          AND $4::text IS NULL AND $5::text IS NULL AND $6::text IS NULL AND $7::text IS NULL
          AND $8::text IS NULL AND $9::text IS NULL AND $10::bool IS NULL AND $11::jsonb IS NOT NULL
    """
    with patch.object(HRService, "_CARD_INSERT_SQL", racing_insert):
        row_id = await HRService(pool).merge_card(
            "tm-b", {"hire_date": date(2025, 2, 1)}, {"unit": "ub"}
        )
    assert row_id is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hire_date FROM hr_employees WHERE team_member_id = 'tm-b'"
        )
    assert row["hire_date"] == date(2025, 2, 1)
    assert await _profile(pool, "tm-b") == {"unit": "ub"}


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_not_found", [False, True])
async def test_import_second_merge_returning_none_rolls_the_batch_back(
    pool: asyncpg.Pool, skip_not_found: bool
) -> None:
    from backend.app.services.hr.hr_service import HRService
    from backend.scripts import import_staff_cards as imp

    document = {
        "cards": [
            {
                "email": "fake.a@balizero.com",
                "fields": {"bank_name": "Changed"},
                "profile": {"unit": "u9"},
            },
            {"email": "fake.b@balizero.com", "fields": {"hire_date": "2025-02-01"}, "profile": {}},
            {"email": "fake.nobody@balizero.com", "fields": {}, "profile": {}},
        ]
    }
    original = HRService.merge_card_on
    calls = {"n": 0}

    async def second_returns_none(self: Any, conn: Any, *args: Any) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 2 else await original(self, conn, *args)

    with patch.object(HRService, "merge_card_on", second_returns_none):
        async with pool.acquire() as conn:
            counts, ok = await imp.run(conn, document, apply=True, skip_not_found=skip_not_found)
    assert ok is False
    assert counts["applied"] == 1 and counts["rejected:not_applied"] == 1
    assert counts["not_found"] == 1 and counts["committed:no"] == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None
        assert await conn.fetchval("SELECT count(*) FROM hr_employees") == 1
    assert await _profile(pool, "tm-a") == {}
    out = imp.format_counts(counts, True)
    assert "committed: " not in out or "committed:no: 1" in out
    assert "fake" not in out


@pytest.mark.asyncio
async def test_self_lookup_is_case_insensitive_and_own_only(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    card = await service.get_card_by_email("FAKE.A@Balizero.com")
    assert card is not None and card["team_member_id"] == "tm-a"
    assert await service.get_card_by_email("fake.b@balizero.com") is None  # no hr row
    assert await service.get_card_by_email("fake.a@balizero.com.evil") is None
    assert await service.get_card_by_email("fake.%@balizero.com") is None  # no LIKE semantics


@pytest.mark.asyncio
async def test_a_stored_string_profile_and_odd_contract_still_read(
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    from backend.app.services.hr.hr_card_models import admin_card, member_card
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE hr_employees SET profile = '\"x\"'::jsonb WHERE id = 1")
    with caplog.at_level("DEBUG"):
        card = await service.get_card_by_id(1)
        assert card is not None and member_card(card).legal_name is None  # (v)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hr_employees SET profile = $1::text::jsonb WHERE id = 1",
                json.dumps({"contract": {"number": "ZQXLEAK\nX"}}),
            )
        card = await service.get_card_by_id(1)
        assert card is not None and admin_card(card).contract.number == "ZQXLEAK\nX"  # (iv)
    assert all("ZQXLEAK" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_card_write_failure_carries_no_driver_text(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRCardError, HRService

    with pytest.raises(HRCardError) as exc:
        await HRService(pool).merge_card("tm-a", {"npwp": "Z" * 80 + "ZQXLEAK"}, {})
    assert "ZQXLEAK" not in str(exc.value) and "ZQXLEAK" not in repr(exc.value.__cause__)
    assert exc.value.__suppress_context__
