"""The client-visible milestone of a GARUDA handoff is cosmetic; the paid practice is not.

The milestone insert runs in a savepoint after the practice. Any failure of that insert is
logged and the practice is kept, as the canonical writer (crm_practices.py) does; a cancelled
task still propagates and leaves nothing behind. Real Postgres: each connection of the pool
below carries a TEMP `timeline_events` that refuses milestones, so the insert fails the way a
CHECK or a column change in production would.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from backend.services.garuda_ops.adapters_pg import PostgresCrmWriter
from backend.tests.services.garuda_ops.test_adapters_pg import (
    _DSN,
    PRACTICE_TYPE,
    _reset_suite_rows,
    _snapshot,
)

pytestmark = pytest.mark.asyncio

_EMAIL = "milestone-refused@example.invalid"


async def _milestones_are_refused(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS timeline_events (
            id SERIAL PRIMARY KEY,
            client_id INTEGER NOT NULL,
            practice_id INTEGER,
            event_type VARCHAR(100) NOT NULL CHECK (event_type <> 'milestone'),
            title VARCHAR(255) NOT NULL,
            description TEXT,
            event_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            client_visible BOOLEAN NOT NULL DEFAULT TRUE,
            color VARCHAR(50) DEFAULT 'info',
            created_by VARCHAR(255)
        )
        """
    )


@pytest.fixture
async def pool():
    try:
        p = await asyncpg.create_pool(_DSN, min_size=1, max_size=2, init=_milestones_are_refused)
    except (OSError, asyncpg.PostgresError) as exc:
        if os.environ.get("CI"):
            pytest.fail(f"no reachable Postgres in CI at {_DSN}: {exc}")
        pytest.skip(f"no reachable Postgres at {_DSN}: {exc}")
    try:
        await _reset_suite_rows(p)
        yield p
        await _reset_suite_rows(p)
    finally:
        await p.close()


async def _practice_count(key: str) -> int:
    conn = await asyncpg.connect(_DSN)
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM practices WHERE source_idempotency_key = $1", key
        )
    finally:
        await conn.close()


async def test_a_refused_milestone_keeps_the_paid_practice(pool, caplog):
    writer = PostgresCrmWriter(pool)
    key = f"evt_{uuid.uuid4().hex}"
    caplog.set_level(logging.WARNING, logger="garuda.ops.adapters_pg")

    practice_id = await writer.create_client_and_practice(
        _snapshot(customer_email=_EMAIL),
        source_idempotency_key=key,
        practice_type_code=PRACTICE_TYPE,
    )
    again = await writer.create_client_and_practice(
        _snapshot(customer_email=_EMAIL),
        source_idempotency_key=key,
        practice_type_code=PRACTICE_TYPE,
    )

    assert again == practice_id
    assert await _practice_count(key) == 1
    assert await writer.find_practice_by_source_idempotency_key(key) == practice_id
    warnings = [
        r.getMessage() for r in caplog.records if "milestone insert failed" in r.getMessage()
    ]
    assert warnings == ["milestone insert failed (CheckViolationError); the practice is kept"]
    assert all(str(practice_id) not in m and _EMAIL not in m for m in warnings)


async def test_a_cancelled_milestone_still_cancels_the_handoff(pool, monkeypatch):
    writer = PostgresCrmWriter(pool)
    key = f"evt_{uuid.uuid4().hex}"
    execute = asyncpg.connection.Connection.execute

    async def cancelled_at_the_milestone(self, query, *args, **kwargs):
        if "INSERT INTO timeline_events" in query:
            raise asyncio.CancelledError
        return await execute(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.connection.Connection, "execute", cancelled_at_the_milestone)

    with pytest.raises(asyncio.CancelledError):
        await writer.create_client_and_practice(
            _snapshot(customer_email=_EMAIL),
            source_idempotency_key=key,
            practice_type_code=PRACTICE_TYPE,
        )
    monkeypatch.undo()

    assert await _practice_count(key) == 0
