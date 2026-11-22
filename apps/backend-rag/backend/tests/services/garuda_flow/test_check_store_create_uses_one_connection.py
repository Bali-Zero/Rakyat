"""Storing a new eligibility check uses the one connection it holds.

Real PostgreSQL, the same tables and policy seeding as the check-to-order
journey tests. A pool of ONE connection makes the rule deterministic: a create
that asks the pool for a second connection while holding the first can never
finish, with no race and no load needed.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import asyncpg
import pytest

from backend.services.garuda_flow.check_store import PostgresCheckStore
from backend.services.garuda_flow.civil_clock import garuda_today
from backend.services.garuda_flow.public_api import (
    EligibilityCheckOutcome,
    PersistencePolicyUnavailable,
)
from backend.tests.fixtures.prod_shaped_pool import create_prod_shaped_pool

_DSN = (
    os.environ.get("GARUDA_L3_TEST_DSN")
    or os.environ.get("INTAKE_TEST_DSN")
    or "postgresql://localhost:5432/nuzantara_test"
)
_TODAY = garuda_today()
_ENTRY_DATE = _TODAY + timedelta(days=7)
_DEADLINE_SECONDS = 15


def _request() -> dict[str, object]:
    return {
        "case_type": "issuance",
        "nationality": "USA",
        "entry_date": _ENTRY_DATE.isoformat(),
        "passport_expiry_date": (_ENTRY_DATE + timedelta(days=200)).isoformat(),
        "voa_expiry_date": None,
        "purpose": "tourism",
        "travellers": 1,
        "self_pay": True,
        "extension_already_used": False,
        "retention_notice_acknowledged": True,
    }


def _outcome() -> EligibilityCheckOutcome:
    return EligibilityCheckOutcome(
        accepted=True,
        reason_codes=[],
        published_filing_deadline=_TODAY + timedelta(days=1),
        price_idr=790_000,
        price_source="B1 Visa on Arrival (VOA)",
    )


def _key() -> str:
    return f"one-connection-{uuid.uuid4().hex}"


async def _close_open_policies(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        UPDATE public.visa_decision_retention_policies
           SET effective_period = tstzrange(lower(effective_period), clock_timestamp(), '[)')
         WHERE environment = 'TEST' AND policy_scope = 'GARUDA_CHECK'
           AND upper(effective_period) IS NULL
        """
    )


async def _pool(max_size: int) -> asyncpg.Pool:
    try:
        pool = await create_prod_shaped_pool(_DSN, min_size=1, max_size=max_size)
    except (OSError, asyncpg.PostgresError) as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI has no reachable Postgres at the test DSN: {exc}")
        pytest.skip(f"no local Postgres reachable at the test DSN: {exc}")
        raise
    async with pool.acquire() as conn:
        await _close_open_policies(conn)
        await conn.execute(
            """
            INSERT INTO public.visa_decision_retention_policies (
                environment, policy_scope, policy_version, retention_interval,
                idempotency_retention_interval, legal_hold_review_interval,
                retention_anchor, effective_period, approved_by, approval_reference
            ) VALUES (
                'TEST', 'GARUDA_CHECK', $1, INTERVAL '90 days',
                INTERVAL '1 hour', INTERVAL '30 days',
                'CREATED_AT', tstzrange(clock_timestamp(), NULL, '[)'),
                'zero-test-approver', 'ZERO-ONE-CONNECTION-TEST-APPROVAL'
            )
            """,
            f"one-connection-test-{uuid.uuid4().hex[:16]}",
        )
    return pool


async def _dispose(pool: asyncpg.Pool) -> None:
    """Close the policy and the pool; a pool whose connections are stuck is torn down."""
    try:
        async with asyncio.timeout(5):
            async with pool.acquire() as conn:
                await _close_open_policies(conn)
            await pool.close()
    except TimeoutError:
        pool.terminate()


async def _finished_in_time(tasks: list[asyncio.Task]) -> int:
    """How many of the creates finished before the deadline; the others are cancelled."""
    done, pending = await asyncio.wait(tasks, timeout=_DEADLINE_SECONDS)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return len(done)


@pytest.mark.asyncio
async def test_a_new_check_is_stored_on_a_pool_of_one_connection() -> None:
    pool = await _pool(max_size=1)
    try:
        store = PostgresCheckStore(pool, environment="TEST")
        create = asyncio.ensure_future(
            store.create(idempotency_key=_key(), canonical_request=_request(), outcome=_outcome())
        )

        finished = await _finished_in_time([create])

        assert finished == 1, "the create is still waiting for a second connection"
        stored = create.result()
        assert stored.idempotency_replayed is False
        assert stored.session_secret
    finally:
        await _dispose(pool)


@pytest.mark.asyncio
async def test_as_many_new_checks_as_the_pool_has_connections_all_finish() -> None:
    pool = await _pool(max_size=3)
    try:
        store = PostgresCheckStore(pool, environment="TEST")
        creates = [
            asyncio.ensure_future(
                store.create(
                    idempotency_key=_key(), canonical_request=_request(), outcome=_outcome()
                )
            )
            for _ in range(9)
        ]

        finished = await _finished_in_time(creates)

        assert finished == 9
        assert len({task.result().result_id for task in creates}) == 9
    finally:
        await _dispose(pool)


@pytest.mark.asyncio
async def test_a_replay_and_a_missing_policy_answer_as_before() -> None:
    pool = await _pool(max_size=2)
    try:
        store = PostgresCheckStore(pool, environment="TEST")
        key = _key()
        first = await asyncio.wait_for(
            store.create(idempotency_key=key, canonical_request=_request(), outcome=_outcome()),
            timeout=_DEADLINE_SECONDS,
        )
        again = await asyncio.wait_for(
            store.create(idempotency_key=key, canonical_request=_request(), outcome=_outcome()),
            timeout=_DEADLINE_SECONDS,
        )
        assert again.idempotency_replayed is True
        assert again.result_id == first.result_id

        async with pool.acquire() as conn:
            await _close_open_policies(conn)
        with pytest.raises(PersistencePolicyUnavailable):
            await asyncio.wait_for(
                store.create(
                    idempotency_key=_key(), canonical_request=_request(), outcome=_outcome()
                ),
                timeout=_DEADLINE_SECONDS,
            )
        async with pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT count(*) FROM garuda_voa_check_results WHERE result_id = $1",
                first.result_id,
            )
        assert stored == 1
    finally:
        await _dispose(pool)
