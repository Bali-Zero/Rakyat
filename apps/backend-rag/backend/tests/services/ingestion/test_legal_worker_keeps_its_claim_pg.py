"""A legal ingest job that is still being worked on is not claimed a second time.

The worker hides a job from other claims for a fixed time, and a law takes far longer than that
to ingest. These run the worker's own claim and its own job function on the real table, with the
times made short: the first step of the job is held open, the hidden time passes on the
database's own clock, and a second claim is tried. Invented rows.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.migrations.migration_070b_legal_ingest_jobs import UPGRADE_SQL
from backend.services.ingestion import legal_full_ingestion_worker as worker

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_HIDDEN_FOR = timedelta(seconds=2)
_RENEW_EVERY = timedelta(milliseconds=200)


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = pytest.mark.skipif(
    not _is_safe_test_database(_DB_URL), reason="needs a local test database"
)


@pytest_asyncio.fixture
async def pool(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[asyncpg.Pool, None]:
    monkeypatch.setattr(worker, "VISIBILITY_TIMEOUT", _HIDDEN_FOR)
    # raising=False: where the worker has no renewal the tests still run, and fail on
    # what the job does, not on a missing name.
    monkeypatch.setattr(worker, "CLAIM_RENEW_INTERVAL", _RENEW_EVERY, raising=False)
    admin = await asyncpg.connect(_DB_URL)
    schema = "legal_claim_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as c:
            await c.execute(UPGRADE_SQL)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


class _HeldJob:
    """Stands in for the download, the first step of a job: holds it open, then ends it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def download(self, *args: Any, **kwargs: Any) -> None:
        self.started.set()
        await self.release.wait()
        raise RuntimeError("the test ends the job here")


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> _HeldJob:
    job = _HeldJob()
    monkeypatch.setattr(worker, "_download_pdf", job.download)
    return job


async def _new_job(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as c:
        job_id = await c.fetchval(
            """
            INSERT INTO legal_ingest_jobs (idempotency_key, tipo, nomor, anno, source_url, nb_target)
            VALUES ($1, 'UU', '1', '2099', 'https://example.test/law.pdf', 'none')
            RETURNING id
            """,
            uuid4().hex,
        )
    return str(job_id)


async def _row(pool: asyncpg.Pool, job_id: str) -> asyncpg.Record:
    async with pool.acquire() as c:
        return await c.fetchrow(
            "SELECT status, visibility_at FROM legal_ingest_jobs WHERE id = $1", job_id
        )


async def _claim(pool: asyncpg.Pool) -> asyncpg.Record | None:
    """What a second worker gets when it asks for a job."""
    async with pool.acquire() as c, c.transaction():
        return await worker._claim_job(c)


async def _until_the_database_clock_passes(pool: asyncpg.Pool, moment: datetime) -> None:
    async with asyncio.timeout(15):
        while True:
            async with pool.acquire() as c:
                if await c.fetchval("SELECT clock_timestamp() > $1", moment):
                    return
            await asyncio.sleep(0.05)


def _claim_keepers() -> list[asyncio.Task[Any]]:
    return [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__name__", "") == "_keep_claim" and not task.done()
    ]


async def _start(pool: asyncpg.Pool, held: _HeldJob) -> asyncio.Task[None]:
    running = asyncio.create_task(worker._process_one_job(pool, MagicMock()))
    await asyncio.wait_for(held.started.wait(), 5)
    return running


async def _end(running: asyncio.Task[None], held: _HeldJob) -> None:
    held.release.set()
    await asyncio.wait_for(running, 5)


async def test_a_job_still_being_worked_on_is_not_claimed_a_second_time(
    pool: asyncpg.Pool, held: _HeldJob
) -> None:
    job_id = await _new_job(pool)
    running = await _start(pool, held)
    try:
        first_claim = (await _row(pool, job_id))["visibility_at"]
        await _until_the_database_clock_passes(pool, first_claim)

        second = await _claim(pool)

        assert second is None, "a second worker was handed the job the first is still on"
        assert len(_claim_keepers()) == 1
    finally:
        await _end(running, held)

    assert (await _row(pool, job_id))["status"] == "failed"


async def test_when_the_job_ends_nothing_keeps_renewing_its_claim(
    pool: asyncpg.Pool, held: _HeldJob
) -> None:
    await _new_job(pool)
    running = await _start(pool, held)

    await _end(running, held)
    await asyncio.sleep(0)

    assert _claim_keepers() == []


async def test_a_worker_that_stops_gives_the_job_back(pool: asyncpg.Pool, held: _HeldJob) -> None:
    job_id = await _new_job(pool)
    running = await _start(pool, held)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.sleep(0)

    assert _claim_keepers() == []
    left = await _row(pool, job_id)
    assert left["status"] == "pending"
    await _until_the_database_clock_passes(pool, left["visibility_at"])
    again = await _claim(pool)
    assert again is not None and str(again["id"]) == job_id


async def test_a_job_held_past_the_limit_is_let_go(
    pool: asyncpg.Pool, held: _HeldJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "CLAIM_MAX_HOLD", _RENEW_EVERY * 3, raising=False)
    real_renewal = getattr(worker, "_renew_claim", None)
    renewals = 0

    async def counted_renewal(db_pool: asyncpg.Pool, job_id: str) -> None:
        nonlocal renewals
        renewals += 1
        await real_renewal(db_pool, job_id)

    monkeypatch.setattr(worker, "_renew_claim", counted_renewal, raising=False)
    job_id = await _new_job(pool)
    running = await _start(pool, held)
    try:
        first_claim = (await _row(pool, job_id))["visibility_at"]
        async with asyncio.timeout(15):
            while (again := await _claim(pool)) is None:
                await asyncio.sleep(0.1)

        assert str(again["id"]) == job_id
        # It was renewed while inside the limit, and let go only after that.
        assert 1 <= renewals <= 2
        assert again["visibility_at"] > first_claim + _HIDDEN_FOR
        assert _claim_keepers() == [], "the claim is still being renewed past the limit"
        seen = renewals
        await asyncio.sleep(_RENEW_EVERY.total_seconds() * 3)
        assert renewals == seen
    finally:
        await _end(running, held)


async def test_a_renewal_that_fails_is_tried_again(
    pool: asyncpg.Pool, held: _HeldJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_renewal = worker._renew_claim
    calls = 0

    async def renewal_that_fails_twice(db_pool: asyncpg.Pool, job_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise asyncpg.PostgresConnectionError("the database was away")
        await real_renewal(db_pool, job_id)

    monkeypatch.setattr(worker, "_renew_claim", renewal_that_fails_twice)
    job_id = await _new_job(pool)
    running = await _start(pool, held)
    try:
        first_claim = (await _row(pool, job_id))["visibility_at"]
        await _until_the_database_clock_passes(pool, first_claim)

        assert await _claim(pool) is None
        assert calls > 2
    finally:
        await _end(running, held)


@pytest.mark.parametrize("status", ["complete", "failed"])
async def test_a_job_that_is_over_is_not_hidden_again(pool: asyncpg.Pool, status: str) -> None:
    job_id = await _new_job(pool)
    async with pool.acquire() as c:
        await c.execute("UPDATE legal_ingest_jobs SET status = $2 WHERE id = $1", job_id, status)
    before = (await _row(pool, job_id))["visibility_at"]

    await worker._renew_claim(pool, job_id)

    assert (await _row(pool, job_id))["visibility_at"] == before


async def test_a_renewal_moves_the_hidden_time_of_its_own_job_only(pool: asyncpg.Pool) -> None:
    mine = await _new_job(pool)
    other = await _new_job(pool)
    before = {job: (await _row(pool, job))["visibility_at"] for job in (mine, other)}

    await worker._renew_claim(pool, mine)

    assert (await _row(pool, mine))["visibility_at"] > before[mine]
    assert (await _row(pool, other))["visibility_at"] == before[other]
