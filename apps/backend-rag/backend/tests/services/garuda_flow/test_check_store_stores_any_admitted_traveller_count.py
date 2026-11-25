"""A traveller count the public contract admits is stored, never a 500 (real PostgreSQL).

The contract admits any integer from 1; garuda_voa_check_results.travellers is int4.
Same tables, policy seeding and DSN as the one-connection create test, whose helpers
this reuses. Synthetic request only.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.garuda_flow.check_store import PostgresCheckStore
from backend.services.garuda_flow.public_api import IdempotencyConflict
from backend.tests.services.garuda_flow.test_check_store_create_uses_one_connection import (
    _DEADLINE_SECONDS,
    _dispose,
    _key,
    _outcome,
    _pool,
    _request,
)

_INT4_MAX = 2**31 - 1


async def _create(store: PostgresCheckStore, key: str, travellers: int):
    request = {**_request(), "travellers": travellers}
    return await asyncio.wait_for(
        store.create(idempotency_key=key, canonical_request=request, outcome=_outcome()),
        timeout=_DEADLINE_SECONDS,
    )


async def _stored_travellers(pool, result_id: str) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT travellers FROM garuda_voa_check_results WHERE result_id = $1", result_id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("travellers", "stored"),
    [(1, 1), (2, 2), (_INT4_MAX, _INT4_MAX), (_INT4_MAX + 1, _INT4_MAX), (10**12, _INT4_MAX)],
    ids=["one", "two", "int4-max", "int4-max-plus-one", "a-trillion"],
)
async def test_an_admitted_traveller_count_is_stored(travellers: int, stored: int) -> None:
    pool = await _pool(max_size=1)
    try:
        store = PostgresCheckStore(pool, environment="TEST")
        created = await _create(store, _key(), travellers)
        assert await _stored_travellers(pool, created.result_id) == stored
    finally:
        await _dispose(pool)


@pytest.mark.asyncio
async def test_a_replay_of_a_huge_count_matches_and_a_different_count_conflicts() -> None:
    pool = await _pool(max_size=1)
    try:
        store = PostgresCheckStore(pool, environment="TEST")
        key = _key()
        first = await _create(store, key, 10**12)
        again = await _create(store, key, 10**12)
        assert again.idempotency_replayed is True
        assert again.result_id == first.result_id
        # The digest is taken on the count as sent, not the stored one.
        with pytest.raises(IdempotencyConflict):
            await _create(store, key, _INT4_MAX)
    finally:
        await _dispose(pool)
