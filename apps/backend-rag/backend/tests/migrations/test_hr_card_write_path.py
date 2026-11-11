"""The staff card WRITE path against a real Postgres: the merge, the lock, the import.

Collected under backend/tests/ and run by the `Backend Shard N` jobs against their
postgres service, like the other tests in this directory (no skip, no xfail). The
tests work in their own schema (`hrcardwrite`) so they never touch shared tables.
The import is exercised twice: its logic in-process (`run`), and the whole tool through
the real launcher in a subprocess (the process boundary is C10 in STAFF_CARD.md).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql
from backend.tests.fixtures.import_root import build_root
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

pytestmark = pytest.mark.integration

SCHEMA = "hrcardwrite"
MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "354_hr_employees_profile.sql"
)

_HR_DDL = """
CREATE TABLE team_members (
    id VARCHAR(36) PRIMARY KEY, full_name TEXT, email TEXT UNIQUE, role TEXT, department TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE);
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


async def _init(conn: asyncpg.Connection) -> None:
    """The production connection init (jsonb codec registration)."""
    await init_prod_shaped_connection(conn)


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    admin = await asyncpg.connect(_dsn())
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('r2-hr-card-write-path-test'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        p = await asyncpg.create_pool(
            _dsn(),
            init=_init,
            server_settings={"search_path": SCHEMA},  # survives the pool's RESET on release
            statement_cache_size=0,
            min_size=1,
            max_size=4,
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
                    "('tm-t2','Fake Twin Upper','Fake.Twin@balizero.com','client','ops'),"
                    "('tm-i','Fake Gone','fake.gone@balizero.com','staff','ops'),"
                    "('tm-p1','Pad One','fake.pad@balizero.com','staff','ops'),"
                    "('tm-p2','Pad Two',' fake.pad@balizero.com ','staff','ops'),"
                    "('tm-p3','Pad Three','Fake.Pad@balizero.com','staff','ops'),"
                    "('tm-s1','Solo Padded',' fake.solo@balizero.com ','staff','ops'),"
                    "('tm-q1','Pair One','fake.q@balizero.com','staff','ops'),"
                    "('tm-q2','Pair Two',' fake.q@balizero.com ','staff','ops')"
                )
                await conn.execute(
                    "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, npwp) "
                    "VALUES ('tm-a', '2025-01-01', 5, '10.01')"
                )
                await conn.execute(
                    "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, profile) "
                    "VALUES ('tm-i', '2025-01-01', 3, '{\"unit\": \"gone\"}')"
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
        await admin.execute("SELECT pg_advisory_unlock(hashtext('r2-hr-card-write-path-test'))")
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
    from backend.scripts import _import_staff_cards_worker as imp

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
    from backend.scripts import _import_staff_cards_worker as imp

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
    from backend.scripts import _import_staff_cards_worker as imp

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
        assert await conn.fetchval("SELECT count(*) FROM hr_employees") == 4  # tm-a, t1, t2, i
    assert await _profile(pool, "tm-a") == {}


@pytest.mark.asyncio
async def test_the_import_rejects_a_case_twin_email_as_ambiguous_and_writes_nothing(
    pool: asyncpg.Pool,
) -> None:
    from backend.scripts import _import_staff_cards_worker as imp

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
async def test_a_card_write_failure_carries_no_driver_text(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRCardError

    with pytest.raises(HRCardError) as exc:
        await _merge(pool, "tm-a", {"npwp": "Z" * 80 + "ZQXLEAK"}, {})
    assert "ZQXLEAK" not in str(exc.value) and "ZQXLEAK" not in repr(exc.value.__cause__)
    assert exc.value.__suppress_context__


# ─── import: the member changes between resolve and merge; lock timeout; lock order ───


@pytest.mark.asyncio
async def test_a_member_renamed_between_resolve_and_merge_is_rejected_and_nothing_is_written(
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_service import HRService
    from backend.scripts import _import_staff_cards_worker as imp

    original = HRService.lock_staff_member

    async def renamed_first(self: Any, conn: Any, member_id: str, email: str) -> bool:
        async with pool.acquire() as other:  # a second connection commits the change
            await other.execute(
                "UPDATE team_members SET email = 'fake.renamed@balizero.com' WHERE id = $1",
                member_id,
            )
        return await original(self, conn, member_id, email)

    card = {"email": "fake.a@balizero.com", "fields": {"bank_name": "ShouldNotLand"}}
    with patch.object(HRService, "lock_staff_member", renamed_first):
        async with pool.acquire() as conn:
            counts, ok = await imp.run(conn, {"cards": [card]}, apply=True)
    assert ok is False and counts["rejected:member_changed"] == 1 and counts["applied"] == 0
    assert counts["committed:no"] == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None


@pytest.mark.asyncio
async def test_a_member_turned_client_between_resolve_and_merge_is_rejected(
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_service import HRService
    from backend.scripts import _import_staff_cards_worker as imp

    original = HRService.lock_staff_member

    async def demoted_first(self: Any, conn: Any, member_id: str, email: str) -> bool:
        async with pool.acquire() as other:
            await other.execute("UPDATE team_members SET role = 'client' WHERE id = $1", member_id)
        return await original(self, conn, member_id, email)

    card = {"email": "fake.a@balizero.com", "fields": {"bank_name": "ShouldNotLand"}}
    with patch.object(HRService, "lock_staff_member", demoted_first):
        async with pool.acquire() as conn:
            counts, ok = await imp.run(conn, {"cards": [card]}, apply=True)
    assert ok is False and counts["rejected:member_changed"] == 1


@pytest.mark.asyncio
async def test_a_blocked_row_lock_is_rejected_as_lock_timeout_and_rolls_back(
    pool: asyncpg.Pool,
) -> None:
    from backend.scripts import _import_staff_cards_worker as imp

    card = {"email": "fake.a@balizero.com", "fields": {"bank_name": "ShouldNotLand"}}
    async with pool.acquire() as holder:
        holder_tx = holder.transaction()
        await holder_tx.start()
        try:  # another writer holds the member row (an uncommitted change to it)
            await holder.execute("UPDATE team_members SET department = 'x' WHERE id = 'tm-a'")
            with patch.object(imp, "LOCK_TIMEOUT_MS", 300):
                async with pool.acquire() as conn:
                    counts, ok = await asyncio.wait_for(
                        imp.run(conn, {"cards": [card]}, apply=True), 20
                    )
        finally:
            await holder_tx.rollback()
    assert ok is False and counts["rejected:lock_timeout"] == 1 and counts["committed:no"] == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None
        assert await conn.fetchval("SHOW lock_timeout") != "300ms"  # SET LOCAL did not leak


@pytest.mark.asyncio
async def test_real_apply_locks_members_in_sorted_id_order(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRService
    from backend.scripts import _import_staff_cards_worker as imp

    seen: list[str] = []
    original = HRService.lock_staff_member

    async def spy(self: Any, conn: Any, member_id: str, email: str) -> bool:
        seen.append(member_id)
        return await original(self, conn, member_id, email)

    doc = {
        "cards": [
            {"email": "fake.twin@balizero.com", "fields": {"bank_name": "T"}},  # tm-t1
            {"email": "fake.b@balizero.com", "fields": {"hire_date": "2025-02-01"}},  # tm-b
            {"email": "fake.a@balizero.com", "fields": {"bank_name": "A"}},  # tm-a
        ]
    }
    with patch.object(HRService, "lock_staff_member", spy):
        async with pool.acquire() as conn:
            counts, ok = await imp.run(conn, doc, apply=True)
    assert ok is True and counts["applied"] == 3
    assert seen == ["tm-a", "tm-b", "tm-t1"]


@pytest.mark.asyncio
async def test_two_concurrent_batches_over_the_same_members_in_opposite_order(
    pool: asyncpg.Pool,
) -> None:
    """Both batches list the same two members, in opposite document order. Locks are taken in
    sorted member-id order, so they serialise instead of deadlocking: both complete (or one is
    rejected:lock_timeout and rolled back whole) and no deadlock error escapes."""
    from backend.scripts import _import_staff_cards_worker as imp

    a, t1 = "fake.a@balizero.com", "fake.twin@balizero.com"

    def batch(first: str, second: str, tag: str) -> dict[str, Any]:
        return {
            "cards": [
                {"email": first, "fields": {"bank_name": f"{tag}1"}, "profile": {"unit": tag}},
                {"email": second, "fields": {"bank_name": f"{tag}2"}, "profile": {"unit": tag}},
            ]
        }

    async def one(document: dict[str, Any]) -> Any:
        async with pool.acquire() as conn:
            return await imp.run(conn, document, apply=True)

    results = await asyncio.wait_for(
        asyncio.gather(one(batch(a, t1, "X")), one(batch(t1, a, "Y"))), 30
    )
    for counts, ok in results:
        assert not [k for k in counts if "deadlock" in k or k == "rejected:db_error"]
        assert ok or counts["rejected:lock_timeout"] >= 1
        assert (counts["committed:yes"], counts["committed:no"]) in ((1, 0), (0, 1))
    async with pool.acquire() as conn:
        rows = {
            r["team_member_id"]: r["bank_name"]
            for r in await conn.fetch(
                "SELECT team_member_id, bank_name FROM hr_employees WHERE team_member_id IN ('tm-a','tm-t1')"
            )
        }
    committed = [tag for (counts, ok), tag in zip(results, ("X", "Y"), strict=True) if ok]
    assert committed, "at least one batch must have committed"
    # Whole batches only: a member never carries one batch's value while the other member
    # carries the other batch's (X wrote a1/t2, Y wrote t1/a2; the later committer wins both).
    expected = {"X": ("X1", "X2"), "Y": ("Y2", "Y1")}  # (tm-a, tm-t1)
    assert (rows["tm-a"], rows["tm-t1"]) in [expected[t] for t in committed]


# ─── the whole tool through the real launcher: a subprocess, a real database ───


def _scoped_dsn() -> str:
    dsn = _dsn()
    return f"{dsn}{'&' if '?' in dsn else '?'}search_path={SCHEMA}"


async def _launch(
    tmp: Path, document: Any, *args: str, rename: bool = False
) -> subprocess.CompletedProcess[bytes]:
    """The real launcher, from a test-owned root (a fake driver only in the rename case)."""
    launcher_file = build_root(tmp, fake_asyncpg=rename)
    cwd = tmp / "cwd"
    cwd.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": _scoped_dsn(),
        "JWT_SECRET_KEY": "j" * 40,  # settings need a 32+ char value to import
        "API_KEYS": "k" * 12,
    }
    if rename:
        env["HRIMPORT_REAL_ASYNCPG"] = str(Path(asyncpg.__file__).parent)
        env["HRIMPORT_FAKE_MODE"] = "rename"
        env["HRIMPORT_RENAME_DSN"] = _scoped_dsn()

    def go() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-I", str(launcher_file), *args],
            input=json.dumps(document).encode(),
            capture_output=True,
            cwd=cwd,
            env=env,
            timeout=180,
        )

    return await asyncio.to_thread(go)


def _assert_quiet(run: subprocess.CompletedProcess[bytes]) -> None:
    for stream in (run.stdout, run.stderr):
        assert b"Traceback" not in stream and b"@" not in stream and b"fake" not in stream
        assert b"ShouldNotLand" not in stream


@pytest.mark.asyncio
async def test_launcher_dry_run_then_apply_writes_the_card_and_says_so(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    document = {
        "cards": [
            {
                "email": "fake.b@balizero.com",
                "fields": {"hire_date": "2025-02-01", "bank_name": "ShouldNotLand"},
                "profile": {"unit": "ub"},
            }
        ]
    }
    dry = await _launch(tmp_path, document)
    assert (
        dry.returncode == 0 and b"would_insert: 1" in dry.stdout and b"committed: no" in dry.stdout
    )
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_employees WHERE team_member_id = 'tm-b'")
            == 0
        )
    run = await _launch(tmp_path, document, "--apply")
    assert run.returncode == 0 and run.stderr == b""
    assert run.stdout.splitlines() == [
        b"mode: apply", b"resolved: 1", b"not_found: 0", b"applied: 1", b"committed: yes",
    ]  # fmt: skip
    _assert_quiet(run)
    assert await _profile(pool, "tm-b") == {"unit": "ub"}


@pytest.mark.asyncio
async def test_launcher_a_rejected_batch_writes_nothing_and_says_committed_no(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    document = {
        "cards": [
            {"email": "fake.a@balizero.com", "fields": {"bank_name": "ShouldNotLand"}},
            {"email": "fake.b@balizero.com", "fields": {"hire_date": "2025-02-01", "npwp": "1"}},
        ]
    }
    run = await _launch(tmp_path, document, "--apply")
    assert run.returncode == 1 and run.stderr == b""
    assert b"rejected:invalid_fields: 1" in run.stdout and b"committed: no" in run.stdout
    _assert_quiet(run)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_employees WHERE team_member_id = 'tm-b'")
            == 0
        )


@pytest.mark.asyncio
async def test_launcher_an_ambiguous_email_is_rejected_and_nothing_is_written(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE team_members SET role = 'staff' WHERE id = 'tm-t2'")
    card = {"email": "fake.twin@balizero.com", "fields": {"bank_name": "ShouldNotLand"}}
    run = await _launch(tmp_path, {"cards": [card]}, "--apply")
    assert run.returncode == 1 and b"rejected:ambiguous_email: 1" in run.stdout
    assert b"committed: no" in run.stdout and b"resolved: 0" in run.stdout
    _assert_quiet(run)
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_employees WHERE bank_name IS NOT NULL")
            == 0
        )


@pytest.mark.asyncio
async def test_launcher_a_member_renamed_between_resolve_and_merge_is_rejected(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    card = {"email": "fake.a@balizero.com", "fields": {"bank_name": "ShouldNotLand"}}
    run = await _launch(tmp_path, {"cards": [card]}, "--apply", rename=True)
    assert run.returncode == 1 and b"rejected:member_changed: 1" in run.stdout
    assert b"committed: no" in run.stdout and b"applied: 0" in run.stdout
    _assert_quiet(run)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["fake.pad@balizero.com", "Fake.Pad@balizero.com"])
async def test_a_padded_twin_and_a_case_twin_make_either_canonical_address_ambiguous(
    pool: asyncpg.Pool, address: str
) -> None:
    """Rows 'x@y', ' x@y ' and 'X@y' all exist. The card for either canonical address is
    rejected as ambiguous (it must never resolve to one of them) and nothing is written."""
    from backend.scripts import _import_staff_cards_worker as imp

    card = {"email": address, "fields": {"hire_date": "2025-02-01", "bank_name": "ShouldNotLand"}}
    async with pool.acquire() as conn:
        counts, ok = await imp.run(conn, {"cards": [card]}, apply=True)
        assert ok is False and counts["rejected:ambiguous_email"] == 1
        assert counts["resolved"] == 0 and counts["applied"] == 0 and counts["committed:no"] == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_employees WHERE bank_name IS NOT NULL")
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_employees WHERE team_member_id IN ('tm-p1','tm-p2','tm-p3')"
            )
            == 0
        )


@pytest.mark.asyncio
async def test_a_card_naming_the_padded_address_is_refused_not_resolved_to_its_trimmed_twin(
    pool: asyncpg.Pool,
) -> None:
    from backend.scripts import _import_staff_cards_worker as imp

    card = {"email": " fake.pad@balizero.com ", "fields": {"hire_date": "2025-02-01"}}
    async with pool.acquire() as conn:
        counts, ok = await imp.run(conn, {"cards": [card]}, apply=True)
        assert ok is False and counts["rejected:email_noncanonical"] == 1
        assert counts["resolved"] == 0 and counts["applied"] == 0
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_employees WHERE team_member_id LIKE 'tm-p%'"
            )
            == 0
        )


@pytest.mark.asyncio
async def test_a_lone_padded_row_is_not_found_for_the_canonical_card_and_not_written(
    pool: asyncpg.Pool,
) -> None:
    from backend.scripts import _import_staff_cards_worker as imp

    card = {"email": "fake.solo@balizero.com", "fields": {"hire_date": "2025-02-01"}}
    async with pool.acquire() as conn:
        counts, ok = await imp.run(conn, {"cards": [card]}, apply=True, skip_not_found=True)
        assert counts["not_found"] == 1 and counts["resolved"] == 0 and ok is True
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_employees WHERE team_member_id = 'tm-s1'")
            == 0
        )


@pytest.mark.asyncio
async def test_launcher_a_padded_twin_is_ambiguous_through_the_whole_tool(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    card = {"email": "fake.pad@balizero.com", "fields": {"hire_date": "2025-02-01"}}
    run = await _launch(tmp_path, {"cards": [card]}, "--apply")
    assert run.returncode == 1 and b"rejected:ambiguous_email: 1" in run.stdout
    assert b"committed: no" in run.stdout


@pytest.mark.asyncio
async def test_a_padded_twin_alone_makes_the_canonical_address_ambiguous_and_nothing_is_written(
    pool: asyncpg.Pool,
) -> None:
    """Rows 'x@y' and ' x@y ' only (no case twin): the old trim-then-lower lookup saw one row
    and wrote to it. The normalised ambiguity check sees two people behind the address."""
    from backend.scripts import _import_staff_cards_worker as imp

    card = {"email": "fake.q@balizero.com", "fields": {"hire_date": "2025-02-01"}}
    async with pool.acquire() as conn:
        counts, ok = await imp.run(conn, {"cards": [card]}, apply=True)
        assert ok is False and counts["rejected:ambiguous_email"] == 1 and counts["applied"] == 0
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_employees WHERE team_member_id LIKE 'tm-q%'"
            )
            == 0
        )
    padded = {"email": " fake.q@balizero.com ", "fields": {"hire_date": "2025-02-01"}}
    async with pool.acquire() as conn:
        counts, ok = await imp.run(conn, {"cards": [padded]}, apply=True)
        assert counts["rejected:email_noncanonical"] == 1 and counts["applied"] == 0
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_employees WHERE team_member_id LIKE 'tm-q%'"
            )
            == 0
        )
