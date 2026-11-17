"""CRM KG maintenance lease claim against REAL PostgreSQL.

Runs the production claim statement on a throwaway schema holding the real
migration-133 ``system_settings`` DDL. Connects to TEST_DATABASE_URL (same
convention as the other *_pg integration suites).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from backend.db.migration_base import split_migration_sql
from backend.workers.crm_kg_maintenance import _CLAIM_SQL

_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://nuzantara@localhost:5432/nuzantara_test",
)
_MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "133_legacy_system_settings.sql"
)
_SCHEMA = "crm_kg_claim_it"
_KEY = "crm_kg_build_mediated_last_run_at"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=6)


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    setup = await asyncpg.connect(_DB_URL)
    try:
        await setup.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await setup.execute(f"CREATE SCHEMA {_SCHEMA}")
        await setup.execute(f"SET search_path TO {_SCHEMA}")
        forward, _rollback = split_migration_sql(_MIGRATION.read_text(encoding="utf-8"))
        await setup.execute(forward)
        p = await asyncpg.create_pool(
            _DB_URL, min_size=2, max_size=4, server_settings={"search_path": _SCHEMA}
        )
        try:
            yield p
        finally:
            await p.close()
    finally:
        await setup.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await setup.close()


async def _claim(pool: asyncpg.Pool, now: datetime) -> str | None:
    async with pool.acquire() as conn:
        return await conn.fetchval(_CLAIM_SQL, _KEY, now.isoformat(), now - timedelta(hours=6))


async def _seed(pool: asyncpg.Pool, value: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2)", _KEY, value)


@pytest.mark.asyncio
async def test_first_claim_inserts_and_returns_row(pool: asyncpg.Pool) -> None:
    assert await _claim(pool, NOW) == _KEY


@pytest.mark.asyncio
async def test_concurrent_claims_exactly_one_wins(pool: asyncpg.Pool) -> None:
    await _seed(pool, (NOW - timedelta(days=2)).isoformat())
    results = await asyncio.gather(*[_claim(pool, NOW) for _ in range(4)])
    assert sum(r is not None for r in results) == 1


@pytest.mark.asyncio
async def test_concurrent_first_claims_exactly_one_wins(pool: asyncpg.Pool) -> None:
    results = await asyncio.gather(*[_claim(pool, NOW) for _ in range(4)])
    assert sum(r is not None for r in results) == 1


@pytest.mark.asyncio
async def test_recent_stamp_is_not_claimable(pool: asyncpg.Pool) -> None:
    await _seed(pool, (NOW - timedelta(minutes=5)).isoformat())
    assert await _claim(pool, NOW) is None


@pytest.mark.asyncio
async def test_unparseable_value_is_claimable(pool: asyncpg.Pool) -> None:
    await _seed(pool, "not-a-date")
    assert await _claim(pool, NOW) == _KEY
