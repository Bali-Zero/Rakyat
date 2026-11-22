import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.knowledge_graph.incremental_builder import (
    _get_last_kg_run_ts,
    _kg_incremental_tick,
)


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, _sql):
        return self.pool.row


class _Pool:
    def __init__(self):
        self.row = None
        self.connection = _Connection(self)

    def acquire(self):
        return _Acquire(self.connection)

    async def execute(self, _sql, value):
        self.row = {"updated_at": datetime.now(timezone.utc), "value": value}


async def _run_two_ticks(stats):
    pool = _Pool()
    build = AsyncMock(return_value=stats)

    with (
        patch(
            "backend.services.misc.autonomous_scheduler._acquire_task_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "backend.services.misc.autonomous_scheduler._release_task_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "backend.services.knowledge_graph.incremental_builder."
            "run_knowledge_graph_incremental_build",
            build,
        ),
    ):
        await _kg_incremental_tick(pool, 86400, 14400)
        await _kg_incremental_tick(pool, 86400, 14400)

    return build, json.loads(pool.row["value"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["skipped", "error"])
async def test_returned_non_success_status_does_not_consume_daily_cadence(status):
    build, verdict = await _run_two_ticks({"status": status})

    assert build.await_count == 2
    assert verdict["status"] == status


@pytest.mark.asyncio
async def test_all_failed_collections_do_not_consume_daily_cadence_or_look_ok():
    build, verdict = await _run_two_ticks(
        {"collections_processed": 0, "collections_failed": 7, "errors": ["synthetic failure"]}
    )

    assert build.await_count == 2
    assert verdict["status"] == "failed"


@pytest.mark.asyncio
async def test_successful_run_still_consumes_daily_cadence():
    build, verdict = await _run_two_ticks({"status": "ok", "collections_processed": 1})

    assert build.await_count == 1
    assert verdict["status"] == "ok"


async def _last_run_for(value):
    pool = _Pool()
    pool.row = {"updated_at": datetime.now(timezone.utc), "value": value}
    return await _get_last_kg_run_ts(pool), pool.row["updated_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {"status": "skipped"},
        json.dumps({"status": "error"}),
        json.dumps(json.dumps({"status": "failed"})),
    ],
    ids=["dict-from-a-json-codec", "text", "text-encoded-twice"],
)
async def test_a_run_that_did_no_work_is_not_a_run_in_any_stored_shape(value):
    last_run, _ = await _last_run_for(value)

    assert last_run is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["not json", json.dumps({"ran_at": "2026-01-01T00:00:00+00:00"}), None, 7, {"status": 3}],
    ids=["unparsable", "no-status", "null", "a-number", "status-not-text"],
)
async def test_a_verdict_that_cannot_be_read_still_counts_as_a_run(value):
    last_run, stored_at = await _last_run_for(value)

    assert last_run == stored_at
