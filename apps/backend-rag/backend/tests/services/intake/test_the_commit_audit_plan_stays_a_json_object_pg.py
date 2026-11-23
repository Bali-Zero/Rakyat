"""The commit audit plan stays a JSON object through commit, delivery and rollback, on any pool.

The production pools register a jsonb codec whose encoder serialises (backend/core/pg_json_codec.py);
the intake worker's pool registers none. A plan already serialised to JSON and bound to a jsonb
parameter is encoded a second time by the codec and stored as a JSON string, and the delivery
result merged into it then makes an array that the rollback cannot read. The approve route,
the writer, the delivery and the rollback run for real on the isolated test database, on both
kinds of pool. Invented rows only (the writer suite's seed).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from backend.services.intake import writer as intake_writer
from backend.tests.fixtures.prod_shaped_pool import create_prod_shaped_pool
from backend.tests.services.intake.test_intake_writer import (  # noqa: F401  (seed is a fixture)
    ADMIN,
    _client,
    _make_app,
    seed,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(params=["production-shaped", "no-codec"])
async def pool(request: pytest.FixtureRequest) -> AsyncIterator[asyncpg.Pool]:
    dsn = os.environ.get("INTAKE_TEST_DSN")
    if not dsn:
        pytest.skip("INTAKE_TEST_DSN is not set")
    if request.param == "production-shaped":
        p = await create_prod_shaped_pool(dsn, min_size=1, max_size=4)
    else:
        p = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    yield p
    await p.close()


async def _audit(pool: asyncpg.Pool, idem_key: str, outcome: str) -> asyncpg.Record:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT jsonb_typeof(plan) AS kind,
                   plan->>'idempotency_key' AS idem,
                   plan->'crm_push'->>'status' AS push_status,
                   plan->>'rolled_back_doc_id' AS rolled_back_doc_id,
                   plan
              FROM intake_commit_audit
             WHERE idempotency_key = $1 AND outcome = $2
             ORDER BY id DESC
             LIMIT 1
            """,
            idem_key,
            outcome,
        )


async def test_the_plan_is_an_object_after_commit_delivery_and_rollback(pool, seed, monkeypatch):
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "1")
    monkeypatch.setenv("INTAKE_CRM_PUSH_ENABLED", "1")

    async with _client(_make_app(pool, ADMIN)) as cl:
        response = await cl.post(f"/api/intake/review/{seed['p_a']}/approve", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    push_status = body["crm_push"]["status"]
    assert push_status not in (None, "disabled"), "the delivery must run and merge its result"
    doc_id = body["result"]["doc_id"]
    idem_key = body["would_commit"]["idempotency_key"]

    committed = await _audit(pool, idem_key, "committed")
    assert committed["kind"] == "object"
    assert committed["idem"] == idem_key
    assert committed["push_status"] == push_status
    read_back = intake_writer._as_dict(committed["plan"])
    assert read_back["idempotency_key"] == idem_key
    assert read_back["crm_push"]["status"] == push_status

    async with pool.acquire() as conn:
        async with conn.transaction():
            rolled = await intake_writer.rollback_commit(
                conn,
                client_id=seed["cid_a"],
                idempotency_key=idem_key,
                committed_by=ADMIN["email"],
            )
    assert rolled.doc_id == doc_id

    rollback_row = await _audit(pool, idem_key, "rolled_back")
    assert rollback_row["kind"] == "object"
    assert rollback_row["rolled_back_doc_id"] == str(doc_id)
