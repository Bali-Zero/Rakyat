"""C1/K2 under concurrency: the completed-practice gate decides on the LOCKED row.

Real router, real PostgreSQL, two connections. A delivery completion (what /delivery/complete does:
lock the practice row, flip it to completed with its date) commits between the PATCH's early read and
its write; the PATCH must then refuse instead of overwriting the completed practice's date.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.app.routers import crm_practices
from backend.app.routers.crm_practices import PracticeUpdate
from backend.db.migration_base import split_migration_sql
from backend.tests.db.test_invoice_company_production_shape_pg import _SCHEMA_SQL

MIGRATION_356 = (
    Path(__file__).resolve().parents[2] / "db/migrations_v2/356_practices_invoice_company.sql"
)
ADMIN = {"email": "synthetic.admin@example.invalid", "role": "admin"}
STAFF = {"email": "synthetic.consultant@example.invalid", "role": "consultant"}
COMPLETED_AT = datetime(2026, 10, 20, 4, tzinfo=UTC)
NEW_DATE = datetime(2026, 11, 5, 4, tzinfo=UTC)


@pytest_asyncio.fixture
async def pool(monkeypatch: pytest.MonkeyPatch):
    """A per-test schema on TEST_DATABASE_URL (the one the CI shard provides)."""
    dsn = os.environ.get("TEST_DATABASE_URL", "")
    url = urlsplit(dsn)
    if not dsn or url.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in url.path:
        pytest.skip("Requires a local test database, provided by the backend CI shard")
    schema = "completed_anchor_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f"CREATE SCHEMA {schema}")
    db = None
    try:
        db = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        await db.execute(_SCHEMA_SQL)
        up, _ = split_migration_sql(MIGRATION_356.read_text(encoding="utf-8"))
        await db.execute(up.replace("public", schema))
        await db.execute(
            "INSERT INTO practice_types (code, name, category, base_price)"
            " VALUES ('ext_b1_voa','Extension','visa_extension',1000000),"
            " ('ext_c1_tourism','Extension','visa_extension',2000000)"
        )
        for who in (STAFF, ADMIN):
            await db.execute(
                "INSERT INTO team_members (email, active, role) VALUES ($1, true, $2)",
                who["email"],
                who["role"],
            )
        monkeypatch.setenv("PROCESS_VIEW_GRANTS_JSON", json.dumps({ADMIN["email"]: "all"}))
        monkeypatch.setattr(crm_practices, "invalidate_cache", AsyncMock(return_value=0))
        yield db
    finally:
        if db is not None:
            await db.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


@pytest.fixture(autouse=True)
def quiet_background(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crm_practices, "spawn", MagicMock())


async def owned_open_practice(pool: asyncpg.Pool) -> int:
    person = await pool.fetchval(
        "INSERT INTO clients (full_name, email, assigned_to)"
        " VALUES ('Synthetic Person','p@example.invalid',$1) RETURNING id",
        STAFF["email"],
    )
    return await pool.fetchval(
        """INSERT INTO practices (client_id, practice_type_id, practice_type_code, status,
               assigned_to, created_by, quoted_price)
           SELECT $1, id, code, 'on_process', $2, $2, 1000000 FROM practice_types
           WHERE code = 'ext_b1_voa'
           RETURNING id""",
        person,
        STAFF["email"],
    )


async def patch(pool: asyncpg.Pool, practice_id: int, *, user: dict = STAFF, **fields):
    return await crm_practices.update_practice(
        request=None,
        practice_id=practice_id,
        updates=PracticeUpdate(**fields),
        db_pool=pool,
        current_user=user,
    )


async def blocked_on(pool: asyncpg.Pool, holder_pid: int, mover: asyncio.Task) -> int:
    """The pid of a backend blocked by `holder_pid`: it is inside its transaction, waiting on a lock."""
    for _ in range(100):
        pid = await pool.fetchval(
            "SELECT pid FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid)) LIMIT 1",
            holder_pid,
        )
        if pid:
            return pid
        if mover.done():
            mover.result()  # surface why it finished without waiting
        await asyncio.sleep(0.05)
    pytest.fail("the PATCH never blocked on the row lock")


async def advisory_locks(pool: asyncpg.Pool, pid: int) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted AND pid=$1", pid
    )


async def linked_company(pool: asyncpg.Pool, practice_id: int) -> int:
    return await pool.fetchval(
        """WITH c AS (INSERT INTO companies (company_name) VALUES ('PT Synthetic Lock') RETURNING id)
           INSERT INTO client_company_links (client_id, company_id)
           SELECT p.client_id, c.id FROM practices p, c WHERE p.id=$1 RETURNING company_id""",
        practice_id,
    )


async def activity_rows(pool: asyncpg.Pool, practice_id: int) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM activity_log WHERE entity_type='practice' AND entity_id=$1",
        practice_id,
    )


async def complete_in_open_tx(other: asyncpg.Connection, practice_id: int):
    txn = other.transaction()
    await txn.start()
    await other.fetchval("SELECT id FROM practices WHERE id=$1 FOR UPDATE", practice_id)
    await other.execute(
        "UPDATE practices SET status='completed', completion_date=$2 WHERE id=$1",
        practice_id,
        COMPLETED_AT,
    )
    return txn


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["completion_date", "assigned_to", "practice_type_code"])
async def test_a_completion_committed_after_the_early_read_refuses_the_patch(
    pool: asyncpg.Pool, field: str
) -> None:
    practice_id = await owned_open_practice(pool)
    before = await activity_rows(pool, practice_id)
    value = {
        "completion_date": NEW_DATE,
        "assigned_to": "someone.else@example.invalid",
        # a later proof upload prices the bonus by the current service
        "practice_type_code": "ext_c1_tourism",
    }[field]
    async with pool.acquire() as other:
        txn = await complete_in_open_tx(other, practice_id)
        mover = asyncio.create_task(patch(pool, practice_id, **{field: value}))
        # Blocked on the row lock = inside its transaction, after the early unlocked read.
        await blocked_on(pool, await other.fetchval("SELECT pg_backend_pid()"), mover)
        assert not mover.done()
        await txn.commit()
    with pytest.raises(HTTPException) as caught:
        await mover
    assert caught.value.status_code == 409
    row = await pool.fetchrow(
        "SELECT p.status, p.completion_date, p.assigned_to, pt.code AS service FROM practices p"
        " JOIN practice_types pt ON pt.id = p.practice_type_id WHERE p.id=$1",
        practice_id,
    )
    assert (row["status"], row["completion_date"], row["assigned_to"], row["service"]) == (
        "completed",
        COMPLETED_AT,
        STAFF["email"],
        "ext_b1_voa",
    )
    assert await activity_rows(pool, practice_id) == before


@pytest.mark.asyncio
async def test_a_patch_that_commits_first_on_an_open_practice_is_allowed(
    pool: asyncpg.Pool,
) -> None:
    practice_id = await owned_open_practice(pool)
    await patch(pool, practice_id, completion_date=NEW_DATE)
    assert (
        await pool.fetchval("SELECT completion_date FROM practices WHERE id=$1", practice_id)
        == NEW_DATE
    )
    # then the completion happens, and the date is the one the completion writes
    async with pool.acquire() as other, other.transaction():
        await other.fetchval("SELECT id FROM practices WHERE id=$1 FOR UPDATE", practice_id)
        await other.execute(
            "UPDATE practices SET status='completed', completion_date=$2 WHERE id=$1",
            practice_id,
            COMPLETED_AT,
        )
    with pytest.raises(HTTPException) as caught:
        await patch(pool, practice_id, completion_date=NEW_DATE)
    assert caught.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "with_date", "completes"),
    [
        (ADMIN, False, False),  # company only, admin
        (STAFF, False, False),  # company only, non-admin
        (STAFF, True, False),  # mixed: date + the same company, practice still open
        (STAFF, True, True),  # mixed, and a completion commits while it waits
    ],
)
async def test_every_patch_locks_the_practice_row_before_the_company_link(
    pool: asyncpg.Pool, user: dict, with_date: bool, completes: bool
) -> None:
    practice_id = await owned_open_practice(pool)
    company_id = await linked_company(pool, practice_id)
    fields = {"invoice_company_id": company_id}
    if with_date:
        fields["completion_date"] = NEW_DATE
    async with pool.acquire() as holder:
        holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
        txn = await complete_in_open_tx(holder, practice_id) if completes else holder.transaction()
        if not completes:
            await txn.start()
            await holder.fetchval("SELECT id FROM practices WHERE id=$1 FOR UPDATE", practice_id)
        mover = asyncio.create_task(patch(pool, practice_id, user=user, **fields))
        waiter = await blocked_on(pool, holder_pid, mover)
        # The row lock comes first: while it waits for the row it holds no company-link lock.
        assert await advisory_locks(pool, waiter) == 0
        await txn.commit()
    if completes:
        with pytest.raises(HTTPException) as caught:
            await mover
        assert caught.value.status_code == 409
    else:
        await mover
        assert (
            await pool.fetchval("SELECT invoice_company_id FROM practices WHERE id=$1", practice_id)
            == company_id
        )


@pytest.mark.asyncio
async def test_the_patch_history_row_is_stamped_when_it_holds_the_row_lock(
    pool: asyncpg.Pool,
) -> None:
    # now() is the transaction's start; a PATCH that waited for the row lock must date its row after
    # the wait, or a first-proof reader would order it before the writer that had just committed.
    practice_id = await owned_open_practice(pool)
    async with pool.acquire() as other:
        txn = other.transaction()
        await txn.start()
        await other.fetchval("SELECT id FROM practices WHERE id=$1 FOR UPDATE", practice_id)
        mover = asyncio.create_task(patch(pool, practice_id, notes="a note"))
        await blocked_on(pool, await other.fetchval("SELECT pg_backend_pid()"), mover)
        await asyncio.sleep(0.3)
        released = await pool.fetchval("SELECT clock_timestamp()")
        await txn.commit()
    await mover
    stamped = await pool.fetchval(
        "SELECT performed_at FROM activity_log WHERE entity_type='practice' AND entity_id=$1",
        practice_id,
    )
    assert stamped >= released
