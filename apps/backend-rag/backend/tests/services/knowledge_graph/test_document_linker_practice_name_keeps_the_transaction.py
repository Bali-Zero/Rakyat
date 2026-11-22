"""Real-PostgreSQL coverage for practice-name lookups in an open transaction."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from backend.services.knowledge_graph.document_linker import _practice_name

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    # The suite gives every xdist worker its own database, `<base>_gw<N>`.
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = pytest.mark.skipif(
    not _is_safe_test_database(_DB_URL),
    reason="TEST_DATABASE_URL must be a localhost database named test or ending in _test",
)


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    assert _DB_URL is not None
    schema = f"document_linker_practice_{uuid.uuid4().hex[:8]}"
    connection = await asyncpg.connect(_DB_URL)
    created = False
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        await connection.execute(f'SET search_path TO "{schema}"')
        yield connection
    finally:
        if created:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.asyncio
async def test_practice_name_skips_missing_column_without_aborting_transaction(
    conn: asyncpg.Connection,
) -> None:
    await conn.execute("CREATE TABLE practices (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    await conn.execute("INSERT INTO practices (id, title) VALUES (900001, 'Person One')")

    async with conn.transaction():
        assert await _practice_name(conn, 900001) == "Person One"
        assert await conn.fetchval("SELECT 1") == 1


@pytest.mark.asyncio
async def test_practice_name_returns_name_or_none_without_aborting_transaction(
    conn: asyncpg.Connection,
) -> None:
    await conn.execute("CREATE TABLE practices (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    await conn.execute("INSERT INTO practices (id, name) VALUES (900001, 'Person One')")

    async with conn.transaction():
        assert await _practice_name(conn, 900001) == "Person One"
        assert await _practice_name(conn, 900002) is None
        assert await conn.fetchval("SELECT 1") == 1
