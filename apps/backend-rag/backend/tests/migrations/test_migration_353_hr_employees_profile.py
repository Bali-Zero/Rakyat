"""Migration 353, the card read and the merge, against a real Postgres.

Collected under backend/tests/ and run by the `Backend Shard N` jobs against their
postgres service, like the other tests in this directory (no skip, no xfail). The
test works in its own schema (`hrcard353`) so it never touches shared tables.
"""

from __future__ import annotations

import asyncio
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
    id VARCHAR(36) PRIMARY KEY, full_name TEXT, email TEXT UNIQUE, role TEXT, department TEXT);
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
    """The production connection init (jsonb codec registration)."""
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
                    "('tm-c','Fake C','fake.c@balizero.com','client','ops'),"
                    "('tm-t1','Fake Twin','fake.twin@balizero.com','staff','ops'),"
                    "('tm-t2','Fake Twin Upper','Fake.Twin@balizero.com','client','ops')"
                )
                await conn.execute(
                    "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, npwp) "
                    "VALUES ('tm-a', '2025-01-01', 5, '10.01')"
                )
                await conn.execute(
                    "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, notes, profile) "
                    "VALUES ('tm-t1', '2025-01-01', 1, 'n1', '{\"unit\": \"mine\"}'), "
                    "('tm-t2', '2025-01-01', 2, 'n2', '{\"unit\": \"theirs\"}')"
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


async def _merge(pool: asyncpg.Pool, member: str, fields: dict[str, Any], patch: Any) -> Any:
    from backend.app.services.hr.hr_service import HRService

    async with pool.acquire() as conn:
        return await HRService(pool).merge_card_on(conn, member, fields, patch)


async def _typeof(pool: asyncpg.Pool, member: str) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT jsonb_typeof(profile) FROM hr_employees WHERE team_member_id = $1", member
        )


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
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = jsonb_build_object("
            "'docs', $1::text::jsonb, 'contract', $1::text::jsonb, 'unit', 'u0') "
            "WHERE team_member_id = 'tm-a'",
            stored,
        )
    await _merge(pool, "tm-a", {}, {"docs": {"ktp": "f1"}, "contract": {"number": "C1"}, "x": 1})
    assert await _profile(pool, "tm-a") == {
        "unit": "u0",
        "docs": {"ktp": "f1"},
        "contract": {"number": "C1"},
        "x": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["null", '"x"', "[]"])
async def test_a_non_object_profile_is_normalised_before_the_merge(
    pool: asyncpg.Pool, stored: str
) -> None:
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE hr_employees ALTER COLUMN profile DROP NOT NULL")
        await conn.execute("UPDATE hr_employees SET profile = $1::text::jsonb", stored)
    await _merge(pool, "tm-a", {}, {"unit": "u1", "docs": {"a": "f"}})
    assert await _profile(pool, "tm-a") == {"unit": "u1", "docs": {"a": "f"}}


@pytest.mark.asyncio
async def test_merge_keeps_omitted_columns_and_inserts_a_missing_row(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRService

    await _merge(pool, "tm-a", {"hire_date": date(2024, 6, 1), "bank_name": "Fake Bank"}, {})
    assert await _merge(pool, "tm-b", {"hire_date": date(2025, 2, 1)}, {"unit": "ub"}) is not None
    async with pool.acquire() as conn:
        a = await conn.fetchrow("SELECT * FROM hr_employees WHERE team_member_id = 'tm-a'")
        b = await conn.fetchrow("SELECT * FROM hr_employees WHERE team_member_id = 'tm-b'")
    assert a["hire_date"] == date(2024, 6, 1) and a["npwp"] == "10.01" and a["base_salary_idr"] == 5
    assert b["hire_date"] == date(2025, 2, 1) and b["base_salary_idr"] == 0
    card = await HRService(pool).get_card_for_member("tm-b")
    assert card is not None and card["profile"] == {"unit": "ub"}


@pytest.mark.asyncio
async def test_two_merges_keep_profile_an_object_under_the_production_codec(
    pool: asyncpg.Pool,
) -> None:
    async with pool.acquire() as conn:  # the pool really has the codec production has
        assert await conn.fetchval("SELECT $1::jsonb::text", {"a": 1}) == '{"a": 1}'
    await _merge(pool, "tm-a", {"bank_name": "Fake Bank"}, {})  # scalars only
    assert await _typeof(pool, "tm-a") == "object" and await _profile(pool, "tm-a") == {}
    await _merge(
        pool, "tm-a", {}, {"unit": "u1", "docs": {"ktp": "f1"}, "contract": {"number": "C1"}}
    )
    await _merge(
        pool, "tm-a", {"npwp": "9.9"}, {"docs": {"kk": "f2"}, "contract": {"file_id": "cf"}}
    )
    assert await _typeof(pool, "tm-a") == "object"
    assert await _profile(pool, "tm-a") == {
        "unit": "u1",
        "docs": {"ktp": "f1", "kk": "f2"},
        "contract": {"number": "C1", "file_id": "cf"},
    }


@pytest.mark.asyncio
async def test_a_non_object_incoming_part_is_rejected_and_nothing_changes(
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_service import HRCardCode, HRCardError

    await _merge(pool, "tm-a", {}, {"unit": "keep"})
    for bad in ({"docs": ["x"]}, {"contract": "x"}, {"docs": None}):
        with pytest.raises(HRCardError) as exc:
            await _merge(pool, "tm-a", {"bank_name": "ShouldNotLand"}, bad)
        assert exc.value.code is HRCardCode.PROFILE_NOT_OBJECT
    with pytest.raises(HRCardError):
        await _merge(pool, "tm-a", {}, ["x"])
    assert await _profile(pool, "tm-a") == {"unit": "keep"}
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None


@pytest.mark.asyncio
async def test_a_profile_that_would_not_be_an_object_after_the_merge_rolls_back(
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService

    broken = HRService._CARD_UPDATE_SQL.replace(
        "updated_at = NOW()", "updated_at = NOW(), notes = 'ShouldRollBack'"
    ).replace("|| $11::jsonb\n", "|| '[1]'::jsonb\n", 1)
    assert broken != HRService._CARD_UPDATE_SQL
    with patch.object(HRService, "_CARD_UPDATE_SQL", broken):
        with pytest.raises(HRCardError) as exc:
            await _merge(pool, "tm-a", {}, {"unit": "u"})
    assert exc.value.code is HRCardCode.PROFILE_NOT_OBJECT
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT notes FROM hr_employees WHERE id = 1") is None
    assert await _typeof(pool, "tm-a") == "object"


async def _lock_waiters(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )


@pytest.mark.asyncio
async def test_a_concurrent_insert_falls_through_to_the_update_on_two_connections(
    pool: asyncpg.Pool,
) -> None:
    """B inserts the row and holds its transaction open; A's merge finds no row to update,
    its INSERT blocks on B's uncommitted row, B commits, A's INSERT conflicts and A falls
    through to the UPDATE. Two real connections, a real lock wait, no simulation."""
    async with pool.acquire() as b_conn:
        b_tx = b_conn.transaction()
        await b_tx.start()
        await b_conn.execute(
            "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, profile) "
            "VALUES ('tm-b', '2020-01-01', 0, '{\"from_b\": 1}'::jsonb)"
        )
        a_task = asyncio.create_task(
            _merge(pool, "tm-b", {"hire_date": date(2025, 2, 1)}, {"from_a": 1})
        )
        try:
            for _ in range(100):  # A must really be waiting on B's row, not merely scheduled
                if await _lock_waiters(pool):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("the first writer never blocked on the second one's insert")
            assert not a_task.done()
            await b_tx.commit()
        except BaseException:
            await b_tx.rollback()
            a_task.cancel()
            raise
    assert await asyncio.wait_for(a_task, 10) is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT hire_date FROM hr_employees WHERE team_member_id = 'tm-b'")
    assert [r["hire_date"] for r in rows] == [date(2025, 2, 1)]  # one row: A updated B's
    assert await _profile(pool, "tm-b") == {"from_b": 1, "from_a": 1}


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
            {"email": "fake.b@balizero.com", "fields": {"hire_date": "2025-02-01"}},
        ]
    }
    original = HRService.merge_card_on
    calls = {"n": 0}

    async def flaky(self: Any, conn: Any, *args: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise HRCardError(HRCardCode.DB_ERROR)
        return await original(self, conn, *args)

    with patch.object(HRService, "merge_card_on", flaky):
        async with pool.acquire() as conn:
            counts, ok = await imp.run(conn, document, apply=True)
    assert ok is False and counts["applied"] == 1 and counts["rejected:db_error"] == 1
    assert counts["committed:no"] == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None
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
            {"email": "fake.c@balizero.com", "fields": {"hire_date": "2025-02-01"}},
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
            {"email": "fake.b@balizero.com", "fields": {"hire_date": "2025-02-01"}},
            {"email": "fake.nobody@balizero.com"},
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
        assert await conn.fetchval("SELECT count(*) FROM hr_employees") == 3  # tm-a, t1, t2
    assert await _profile(pool, "tm-a") == {}
    assert "fake" not in imp.format_counts(counts, True)


# ─── the case-twin lesson: two team_members rows whose emails differ only by case ───


@pytest.mark.asyncio
async def test_me_card_returns_the_authenticated_rows_card_never_the_case_twins(
    pool: asyncpg.Pool,
) -> None:
    import httpx
    from fastapi import FastAPI

    from backend.app.dependencies import get_current_user, get_database_pool
    from backend.app.routers import hr as hr_router

    def client_for(session: dict[str, Any]) -> httpx.AsyncClient:
        app = FastAPI()
        app.include_router(hr_router.router)
        app.dependency_overrides[get_current_user] = lambda: session
        app.dependency_overrides[get_database_pool] = lambda: pool
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    staff = {"id": "tm-t1", "email": "fake.twin@balizero.com", "role": "staff"}
    async with client_for(staff) as c:
        res = await c.get("/api/hr/me/card")
    assert res.status_code == 200
    body = res.json()
    assert (
        body["unit"] == "mine" and body["base_salary_idr"] == 1 and body["full_name"] == "Fake Twin"
    )
    assert "notes" not in body and "theirs" not in res.text and "n1" not in res.text

    # The twin session (a client row) is refused outright, and a session whose email is
    # the OTHER case still gets only its own row's card, never the lower-case one.
    twin = {"id": "tm-t2", "email": "Fake.Twin@balizero.com", "role": "client"}
    async with client_for(twin) as c:
        assert (await c.get("/api/hr/me/card")).status_code == 403
    twin_staff = {**twin, "role": "staff"}  # a forged role in the session changes nothing
    async with client_for(twin_staff) as c:
        res = await c.get("/api/hr/me/card")
    assert (
        res.status_code == 404
    )  # the DB row is a client: no staff card, whatever the session says
    assert "mine" not in res.text


@pytest.mark.asyncio
async def test_get_card_for_member_is_primary_key_only_and_staff_only(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    card = await service.get_card_for_member("tm-t1")
    assert card is not None and card["profile"] == {"unit": "mine"} and "notes" not in card
    assert await service.get_card_for_member("fake.twin@balizero.com") is None  # an email is no key
    assert await service.get_card_for_member("tm-t2") is None  # a client row has no staff card
    assert await service.get_card_for_member("tm-b") is None  # no hr row


@pytest.mark.asyncio
async def test_the_import_rejects_a_case_twin_email_as_ambiguous_and_writes_nothing(
    pool: asyncpg.Pool,
) -> None:
    from backend.scripts import import_staff_cards as imp

    async with pool.acquire() as conn:
        await conn.execute("UPDATE team_members SET role = 'staff' WHERE id = 'tm-t2'")
        card = {"email": "fake.twin@balizero.com", "fields": {"bank_name": "ShouldNotLand"}}
        counts, ok = await imp.run(conn, {"cards": [card]}, apply=True)
        assert ok is False and counts["rejected:ambiguous_email"] == 1 and counts["resolved"] == 0
        assert counts["committed:no"] == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_employees WHERE bank_name IS NOT NULL")
            == 0
        )
        # While the twin is a client row, only one STAFF row matches: resolved, exact.
        await conn.execute("UPDATE team_members SET role = 'client' WHERE id = 'tm-t2'")
        counts, ok = await imp.run(conn, {"cards": [card]}, apply=True)
        assert ok is True and counts["applied"] == 1
        assert (
            await conn.fetchval("SELECT bank_name FROM hr_employees WHERE team_member_id = 'tm-t1'")
            == "ShouldNotLand"
        )
        assert (
            await conn.fetchval("SELECT bank_name FROM hr_employees WHERE team_member_id = 'tm-t2'")
            is None
        )


@pytest.mark.asyncio
async def test_a_stored_string_profile_and_odd_contract_still_read(
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    from backend.app.services.hr.hr_card_models import member_card
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE hr_employees SET profile = '\"x\"'::jsonb WHERE id = 1")
    with caplog.at_level("DEBUG"):
        card = await service.get_card_for_member("tm-a")
        assert card is not None and member_card(card).legal_name is None
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hr_employees SET profile = $1::text::jsonb WHERE id = 1",
                json.dumps({"contract": {"number": "ZQXLEAK\nX"}}),
            )
        card = await service.get_card_for_member("tm-a")
        assert card is not None and member_card(card).contract.number == "ZQXLEAK\nX"
    assert all("ZQXLEAK" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_card_write_failure_carries_no_driver_text(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRCardError

    with pytest.raises(HRCardError) as exc:
        await _merge(pool, "tm-a", {"npwp": "Z" * 80 + "ZQXLEAK"}, {})
    assert "ZQXLEAK" not in str(exc.value) and "ZQXLEAK" not in repr(exc.value.__cause__)
    assert exc.value.__suppress_context__
