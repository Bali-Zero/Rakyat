"""Expired lead intents are purged on a schedule (real PostgreSQL + the api task).

`/api/lead/capture` is a public writer into `lead_intents` and the repository's purge had
no caller, so the table grew without bound. Rows are synthetic. The repository part runs against a throwaway schema
cloned from the migrated test database; the task part drives the real `lifespan_light`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

from backend.app import main_api
from backend.services.lead_capture.repository import LeadIntentRepository

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://test:test@localhost:5433/test")


@pytest_asyncio.fixture
async def lead_pool() -> AsyncIterator[asyncpg.Pool]:
    try:
        admin = await asyncpg.connect(_DSN)
    except Exception:
        pytest.skip("isolated test Postgres is not reachable")
    schema = f"lead_t_{uuid.uuid4().hex[:12]}"
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(
            f"SET search_path = {schema};"
            " CREATE TABLE lead_intents (LIKE public.lead_intents INCLUDING ALL);"
        )
        pool = await asyncpg.create_pool(
            _DSN, min_size=1, max_size=2, server_settings={"search_path": schema}
        )
        try:
            yield pool
        finally:
            await pool.close()
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


async def _add(pool: asyncpg.Pool, intent_id: str, *, expired: bool, matched: str | None = None):
    await pool.execute(
        "INSERT INTO lead_intents (id, source, context, whatsapp_url, matched_client_id,"
        " expires_at) VALUES ($1, 'visa_clock', '{}'::jsonb, 'https://wa.example.test/x', $2,"
        " CASE WHEN $3 THEN NOW() - INTERVAL '1 day' ELSE NOW() + INTERVAL '1 day' END)",
        intent_id,
        matched,
        expired,
    )


async def _ids(pool: asyncpg.Pool) -> set[str]:
    return {r["id"] for r in await pool.fetch("SELECT id FROM lead_intents")}


@pytest.mark.asyncio
async def test_an_expired_unmatched_intent_is_deleted_and_the_rest_are_kept(lead_pool) -> None:
    await _add(lead_pool, "li_expired", expired=True)
    await _add(lead_pool, "li_fresh", expired=False)
    await _add(lead_pool, "li_matched", expired=True, matched="c1")

    deleted = await LeadIntentRepository(lead_pool).purge_expired()

    assert deleted == 1
    assert await _ids(lead_pool) == {"li_fresh", "li_matched"}


@pytest.mark.asyncio
async def test_one_call_deletes_at_most_limit_rows_and_the_tick_loop_drains_all(
    lead_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(5):
        await _add(lead_pool, f"li_old{i}", expired=True)
    repo = LeadIntentRepository(lead_pool)

    assert await repo.purge_expired(limit=2) == 2
    assert len(await _ids(lead_pool)) == 3

    # The scheduler tick drains the rest in batches of 2 (small batch patched in).
    monkeypatch.setattr(main_api, "_LEAD_INTENTS_PURGE_LIMIT", 2)
    monkeypatch.setattr(main_api, "_LEAD_INTENTS_PURGE_GRACE", timedelta(0))
    calls = await _run_one_tick(lead_pool, monkeypatch)
    assert await _ids(lead_pool) == set()
    assert calls == [2, 1]  # a short batch ends the tick


async def _run_one_tick(pool, monkeypatch) -> list[int]:
    calls: list[int] = []
    real = LeadIntentRepository.purge_expired

    async def spy(self, limit: int = 500, grace: timedelta = timedelta(0)) -> int:
        n = await real(self, limit=limit, grace=grace)
        calls.append(n)
        return n

    monkeypatch.setattr(LeadIntentRepository, "purge_expired", spy)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:  # initial delay, then the post-tick sleep
            raise asyncio.CancelledError

    monkeypatch.setattr(main_api.asyncio, "sleep", fake_sleep)
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))
    with pytest.raises(asyncio.CancelledError):
        await main_api._run_lead_intents_purge_scheduler(app)
    return calls


@pytest.mark.asyncio
async def test_a_tick_stops_at_the_loop_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_api, "_LEAD_INTENTS_PURGE_LIMIT", 3)
    monkeypatch.setattr(main_api, "_LEAD_INTENTS_PURGE_MAX_LOOPS", 4)
    calls = await _run_one_tick_fake(monkeypatch, always=3)
    assert calls == 4


async def _run_one_tick_fake(monkeypatch, *, always: int) -> int:
    calls = 0

    async def fake_purge(self, limit: int = 500, grace: timedelta = timedelta(0)) -> int:
        nonlocal calls
        calls += 1
        return always

    monkeypatch.setattr(LeadIntentRepository, "purge_expired", fake_purge)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(main_api.asyncio, "sleep", fake_sleep)
    app = SimpleNamespace(state=SimpleNamespace(db_pool=object()))
    with pytest.raises(asyncio.CancelledError):
        await main_api._run_lead_intents_purge_scheduler(app)
    return calls


@pytest.mark.asyncio
async def test_a_purge_that_raises_is_logged_and_the_loop_reaches_the_next_tick(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = 0

    async def boom(self, limit: int = 500, grace: timedelta = timedelta(0)) -> int:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("db down")

    monkeypatch.setattr(LeadIntentRepository, "purge_expired", boom)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:  # initial, tick 1, tick 2
            raise asyncio.CancelledError

    monkeypatch.setattr(main_api.asyncio, "sleep", fake_sleep)
    app = SimpleNamespace(state=SimpleNamespace(db_pool=object()))
    with caplog.at_level("WARNING"), pytest.raises(asyncio.CancelledError):
        await main_api._run_lead_intents_purge_scheduler(app)

    assert attempts == 2
    assert "lead-intents purge tick failed: RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_the_api_lifespan_spawns_the_purge_task_and_cancels_it_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEAD_INTENTS_PURGE_ENABLED", raising=False)
    monkeypatch.delenv("DISABLE_BACKGROUND_WORKERS", raising=False)
    monkeypatch.setenv("WA_OUTBOX_SCHEDULER_ENABLED", "false")
    monkeypatch.delenv("GARUDA_OUTBOX_CONSUMER_ENABLED", raising=False)

    async def fake_init(app: object) -> None:
        app.state.db_pool = AsyncMock()
        app.state.cache = None

    monkeypatch.setattr(
        "backend.app.setup.service_initializer.initialize_services_light", fake_init
    )
    started = asyncio.Event()

    async def fake_loop(app: object) -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(main_api, "_run_lead_intents_purge_scheduler", fake_loop)

    app = SimpleNamespace(state=SimpleNamespace())
    async with main_api.lifespan_light(app):
        for _ in range(500):
            if getattr(app.state, "_lead_intents_purge_task", None) is not None:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("the lifespan never spawned the lead-intents purge task")
        task = app.state._lead_intents_purge_task
        await asyncio.wait_for(started.wait(), 5)
        assert not task.done()

    assert task.cancelled()


@pytest.mark.asyncio
async def test_the_kill_switch_and_missing_pool_spawn_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEAD_INTENTS_PURGE_ENABLED", "false")
    assert (
        main_api._spawn_lead_intents_purge_task(
            SimpleNamespace(state=SimpleNamespace(db_pool=object()))
        )
        is None
    )
    monkeypatch.delenv("LEAD_INTENTS_PURGE_ENABLED")
    assert (
        main_api._spawn_lead_intents_purge_task(
            SimpleNamespace(state=SimpleNamespace(db_pool=None))
        )
        is None
    )


@pytest.mark.asyncio
async def test_grace_keeps_recently_expired_rows_and_deletes_old_ones(lead_pool) -> None:
    await lead_pool.execute(
        "INSERT INTO lead_intents (id, source, context, whatsapp_url, expires_at) VALUES"
        " ('li_exp10', 'visa_clock', '{}'::jsonb, 'https://wa.example.test/x',"
        "  NOW() - INTERVAL '10 days'),"
        " ('li_exp70', 'visa_clock', '{}'::jsonb, 'https://wa.example.test/x',"
        "  NOW() - INTERVAL '70 days')"
    )

    deleted = await LeadIntentRepository(lead_pool).purge_expired(grace=timedelta(days=62))

    assert deleted == 1
    assert await _ids(lead_pool) == {"li_exp10"}


@pytest.mark.asyncio
async def test_the_scheduler_grace_keeps_the_dashboard_month_to_date_count(
    lead_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marketing widget counts every lead_intents row created this calendar month
    (dashboard_summary._get_new_leads_count); a row created on the 1st at 00:00 expires
    7 days later and must survive the scheduler's purge, or the widget's count shrinks."""
    first = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await lead_pool.execute(
        "INSERT INTO lead_intents (id, source, context, whatsapp_url, created_at, expires_at)"
        " VALUES ('li_first', 'visa_clock', '{}'::jsonb, 'https://wa.example.test/x', $1, $2)",
        first,
        first + timedelta(days=7),
    )
    count_sql = (
        "SELECT COUNT(*) FROM lead_intents"
        " WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)"
    )
    before = await lead_pool.fetchval(count_sql)
    assert before == 1

    # Worst case on the month's last day: created 31 days ago, expired 24 days ago. This one
    # is date-independent, so it fails deterministically if the grace is shortened.
    await lead_pool.execute(
        "INSERT INTO lead_intents (id, source, context, whatsapp_url, created_at, expires_at)"
        " VALUES ('li_worst', 'visa_clock', '{}'::jsonb, 'https://wa.example.test/x',"
        " NOW() - INTERVAL '31 days', NOW() - INTERVAL '24 days')"
    )

    await _run_one_tick(lead_pool, monkeypatch)

    assert await lead_pool.fetchval(count_sql) == before
    assert await _ids(lead_pool) == {"li_first", "li_worst"}
    assert main_api._LEAD_INTENTS_PURGE_GRACE >= timedelta(days=31 + 7), (
        "grace must cover a month-old row plus its 7-day TTL or the widget count shrinks"
    )
