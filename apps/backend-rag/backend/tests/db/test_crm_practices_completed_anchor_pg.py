"""C1/K2 under concurrency: the completed-practice gate decides on the LOCKED row.

Real router, real PostgreSQL, two connections. A delivery completion (what /delivery/complete does:
lock the practice row, flip it to completed with its date) commits between the PATCH's early read and
its write; the PATCH must then refuse instead of overwriting the completed practice's date.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.app.routers import crm_practices
from backend.app.routers.crm_practices import PracticeUpdate
from backend.tests.db.test_invoice_company_production_shape_pg import _SCHEMA_SQL

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
        await db.execute(
            "INSERT INTO practice_types (code, name, category, base_price)"
            " VALUES ('ext_b1_voa','Extension','visa_extension',1000000)"
        )
        await db.execute(
            "INSERT INTO team_members (email, active, role) VALUES ($1, true, $2)",
            STAFF["email"],
            STAFF["role"],
        )
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
        "INSERT INTO clients (full_name, email) VALUES ('Synthetic Person','p@example.invalid') RETURNING id"
    )
    return await pool.fetchval(
        """INSERT INTO practices (client_id, practice_type_id, practice_type_code, status,
               assigned_to, created_by, quoted_price)
           SELECT $1, id, code, 'on_process', $2, $2, 1000000 FROM practice_types LIMIT 1
           RETURNING id""",
        person,
        STAFF["email"],
    )


async def patch(pool: asyncpg.Pool, practice_id: int, **fields):
    return await crm_practices.update_practice(
        request=None,
        practice_id=practice_id,
        updates=PracticeUpdate(**fields),
        db_pool=pool,
        current_user=STAFF,
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
@pytest.mark.parametrize("field", ["completion_date", "assigned_to"])
async def test_a_completion_committed_after_the_early_read_refuses_the_patch(
    pool: asyncpg.Pool, field: str
) -> None:
    practice_id = await owned_open_practice(pool)
    before = await activity_rows(pool, practice_id)
    value = NEW_DATE if field == "completion_date" else "someone.else@example.invalid"
    async with pool.acquire() as other:
        txn = await complete_in_open_tx(other, practice_id)
        mover = asyncio.create_task(patch(pool, practice_id, **{field: value}))
        await asyncio.sleep(0.5)  # the PATCH read the open practice and waits on the row lock
        assert not mover.done()
        await txn.commit()
    with pytest.raises(HTTPException) as caught:
        await mover
    assert caught.value.status_code == 409
    row = await pool.fetchrow(
        "SELECT status, completion_date, assigned_to FROM practices WHERE id=$1", practice_id
    )
    assert (row["status"], row["completion_date"], row["assigned_to"]) == (
        "completed",
        COMPLETED_AT,
        STAFF["email"],
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
