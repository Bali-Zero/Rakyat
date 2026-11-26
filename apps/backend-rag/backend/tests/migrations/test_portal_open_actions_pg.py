"""Portal dashboard open actions against a real Postgres, own schema `portalopen`.

The practice table is the production shape for the columns the query reads
(`missing_documents JSONB DEFAULT '[]'`). The pool carries the production jsonb
codec. All names are placeholders.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers.portal_dashboard import _fetch_open_actions
from backend.services.portal.recap_service import build_recap
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

pytestmark = pytest.mark.integration

SCHEMA = "portalopen"

_DDL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
CREATE TABLE {SCHEMA}.practice_types (id SERIAL PRIMARY KEY, name TEXT, code TEXT);
CREATE TABLE {SCHEMA}.practices (
    id SERIAL PRIMARY KEY, client_id INTEGER, practice_type_id INTEGER,
    status VARCHAR DEFAULT 'inquiry',
    missing_documents JSONB DEFAULT '[]'::jsonb,
    client_visible BOOLEAN DEFAULT true,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO {SCHEMA}.practice_types (name, code) VALUES ('Contoh Permit', 'contoh_permit');
"""

CLIENT = 7


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    admin = await asyncpg.connect(_dsn())
    created: asyncpg.Pool | None = None
    try:
        await admin.execute(_DDL)
        created = await asyncpg.create_pool(
            _dsn(),
            min_size=1,
            max_size=2,
            init=init_prod_shaped_connection,
            server_settings={"search_path": SCHEMA},
        )
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.close()


async def _practice(pool: asyncpg.Pool, status: str, missing: str | None) -> int:
    """`missing=None` leaves the column default ('[]'); else a JSON literal."""
    if missing is None:
        return await pool.fetchval(
            "INSERT INTO practices (client_id, practice_type_id, status)"
            " VALUES ($1, 1, $2) RETURNING id",
            CLIENT,
            status,
        )
    return await pool.fetchval(
        "INSERT INTO practices (client_id, practice_type_id, status, missing_documents)"
        " VALUES ($1, 1, $2, $3::text::jsonb) RETURNING id",
        CLIENT,
        status,
        missing,
    )


async def _actions(pool: asyncpg.Pool) -> dict[int, list[str]]:
    async with pool.acquire() as conn:
        rows = await _fetch_open_actions(conn, CLIENT)
    return {r["id"]: r["pending_from_client"] for r in rows}


@pytest.mark.asyncio
async def test_default_empty_list_in_inquiry_is_not_an_action(pool: asyncpg.Pool) -> None:
    pid = await _practice(pool, "inquiry", None)
    assert pid not in await _actions(pool)


@pytest.mark.asyncio
async def test_explicit_empty_list_in_progress_is_not_an_action(pool: asyncpg.Pool) -> None:
    pid = await _practice(pool, "in_progress", "[]")
    assert pid not in await _actions(pool)


@pytest.mark.asyncio
async def test_json_null_in_progress_is_not_an_action(pool: asyncpg.Pool) -> None:
    pid = await _practice(pool, "in_progress", "null")
    assert pid not in await _actions(pool)


@pytest.mark.asyncio
async def test_non_array_value_is_not_an_action(pool: asyncpg.Pool) -> None:
    pid = await _practice(pool, "in_progress", '{"a": 1}')
    assert pid not in await _actions(pool)


@pytest.mark.asyncio
async def test_listed_documents_are_an_action_with_the_list(pool: asyncpg.Pool) -> None:
    pid = await _practice(pool, "in_progress", '["passport"]')
    assert (await _actions(pool))[pid] == ["passport"]


@pytest.mark.asyncio
async def test_waiting_documents_with_empty_list_is_still_listed(pool: asyncpg.Pool) -> None:
    pid = await _practice(pool, "waiting_documents", "[]")
    assert (await _actions(pool))[pid] == []


@pytest.mark.asyncio
async def test_waiting_documents_with_json_null_is_listed_with_an_empty_list(
    pool: asyncpg.Pool,
) -> None:
    pid = await _practice(pool, "waiting_documents", "null")
    assert (await _actions(pool))[pid] == []


@pytest.mark.asyncio
async def test_recap_says_nothing_to_do_when_only_empty_lists_exist(pool: asyncpg.Pool) -> None:
    await _practice(pool, "inquiry", None)
    await _practice(pool, "in_progress", "null")
    async with pool.acquire() as conn:
        actions = await _fetch_open_actions(conn, CLIENT)
    recap = await build_recap(
        open_actions=actions, upcoming_deadlines=[], unread_messages=0, polish=False
    )
    assert "Nothing needs your attention" in str(recap)


@pytest.mark.asyncio
async def test_recap_still_counts_a_real_action(pool: asyncpg.Pool) -> None:
    await _practice(pool, "inquiry", None)
    await _practice(pool, "in_progress", '["passport"]')
    async with pool.acquire() as conn:
        actions = await _fetch_open_actions(conn, CLIENT)
    assert len(actions) == 1
    recap = await build_recap(
        open_actions=actions, upcoming_deadlines=[], unread_messages=0, polish=False
    )
    assert "There is 1 thing that needs you: Contoh Permit." in str(recap)
