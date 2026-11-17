"""CRM KG maintenance compare-and-swap claim against REAL PostgreSQL.

Runs the production claim statements and ``run_due_jobs`` on a unique throwaway
schema holding the real migration-133 ``system_settings`` DDL. Skipped unless
TEST_DATABASE_URL points at a local ``*_test`` database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from backend.db.migration_base import split_migration_sql
from backend.workers import crm_kg_maintenance as kg

_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://nuzantara@localhost:5432/nuzantara_test",
)


def _is_safe_test_database(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in {"localhost", "127.0.0.1"} and parsed.path.lstrip("/").endswith(
        "_test"
    )


pytestmark = pytest.mark.skipif(
    not _is_safe_test_database(_DB_URL),
    reason="TEST_DATABASE_URL must be a localhost database whose name ends with _test",
)

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "133_legacy_system_settings.sql"
)
_KEY = f"{kg.BUILD_MEDIATED_JOB}_last_run_at"
_GC_KEY = f"{kg.GARBAGE_COLLECT_JOB}_last_run_at"
_OLD = (datetime.now(UTC) - timedelta(days=2)).isoformat()


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    schema = f"crm_kg_claim_it_{uuid.uuid4().hex[:8]}"
    setup = await asyncpg.connect(_DB_URL)
    created = False
    try:
        await setup.execute(f"CREATE SCHEMA {schema}")
        created = True
        await setup.execute(f"SET search_path TO {schema}")
        forward, _rollback = split_migration_sql(_MIGRATION.read_text(encoding="utf-8"))
        await setup.execute(forward)
        p = await asyncpg.create_pool(
            _DB_URL, min_size=2, max_size=4, server_settings={"search_path": schema}
        )
        try:
            yield p
        finally:
            await p.close()
    finally:
        if created:
            await setup.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await setup.close()


def _job(name: str, interval: int = 3600) -> kg._Job:
    return kg._Job(name, interval, AsyncMock(return_value={"ok": True}))


async def _seed(pool: asyncpg.Pool, key: str, value: str) -> None:
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2)", key, value)


async def _stored(pool: asyncpg.Pool, key: str) -> str | None:
    return await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", key)


@pytest.mark.asyncio
async def test_concurrent_first_claims_exactly_one_wins(pool: asyncpg.Pool) -> None:
    now = datetime.now(UTC).isoformat()
    results = await asyncio.gather(
        *[pool.fetchval(kg._CLAIM_INSERT_SQL, _KEY, now) for _ in range(4)]
    )
    assert sum(r is not None for r in results) == 1


@pytest.mark.asyncio
async def test_concurrent_cas_updates_from_same_old_value_exactly_one_wins(
    pool: asyncpg.Pool,
) -> None:
    await _seed(pool, _KEY, _OLD)
    stamps = [(datetime.now(UTC) + timedelta(seconds=i)).isoformat() for i in range(4)]
    results = await asyncio.gather(
        *[pool.fetchval(kg._CLAIM_UPDATE_SQL, _KEY, s, _OLD) for s in stamps]
    )
    assert sum(r is not None for r in results) == 1
    assert await _stored(pool, _KEY) in stamps


@pytest.mark.asyncio
async def test_concurrent_claim_run_exactly_one_machine_claims(pool: asyncpg.Pool) -> None:
    await _seed(pool, _KEY, _OLD)
    job = _job(kg.BUILD_MEDIATED_JOB)
    results = await asyncio.gather(*[kg._claim_run(pool, job) for _ in range(4)])
    assert sum(results) == 1


@pytest.mark.asyncio
async def test_recent_stamp_is_not_claimable(pool: asyncpg.Pool) -> None:
    recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    await _seed(pool, _KEY, recent)
    assert await kg._claim_run(pool, _job(kg.BUILD_MEDIATED_JOB)) is False
    assert await _stored(pool, _KEY) == recent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value",
    ["2026-99-99T99:99:99+00:00", "garbage", "2026-11-18T05:00:00", ""],
)
async def test_invalid_value_is_claimed_replaced_and_then_not_due(
    pool: asyncpg.Pool, bad_value: str
) -> None:
    await _seed(pool, _KEY, bad_value)
    job = _job(kg.BUILD_MEDIATED_JOB)
    assert await kg._claim_run(pool, job) is True
    stamp = await _stored(pool, _KEY)
    assert stamp != bad_value
    assert datetime.fromisoformat(stamp).tzinfo is not None
    assert await kg._claim_run(pool, job) is False


@pytest.mark.asyncio
async def test_run_due_jobs_end_to_end_with_fake_jobs(pool: asyncpg.Pool) -> None:
    build = AsyncMock(return_value={"ok": True, "edges": 1})
    gc = AsyncMock(return_value={"ok": True})
    config = kg.KgMaintenanceConfig(True, 3600, 3600, 60, 60)
    with (
        patch(
            "backend.services.knowledge_graph.mediated_edges_builder.build_mediated_edges", build
        ),
        patch("backend.services.knowledge_graph.garbage_collector.garbage_collect", gc),
    ):
        first = await kg.run_due_jobs(pool, config)
        second = await kg.run_due_jobs(pool, config)
    assert first[kg.BUILD_MEDIATED_JOB]["status"] == "ok"
    assert second == {
        kg.BUILD_MEDIATED_JOB: {"status": "not_due"},
        kg.GARBAGE_COLLECT_JOB: {"status": "not_due"},
    }
    build.assert_called_once()
    gc.assert_called_once()
    assert await _stored(pool, f"{kg.BUILD_MEDIATED_JOB}_last_result") is not None
    assert await _stored(pool, _GC_KEY) is not None
