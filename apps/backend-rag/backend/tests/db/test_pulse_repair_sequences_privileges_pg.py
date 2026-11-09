"""Pulse.repair_sequences only walks tables the running role may read (real PostgreSQL).

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it).
Synthetic names only. The pool runs as a NON-superuser role, because a superuser
passes every privilege check and would make each case vacuous.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import MagicMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.olympus.pulse import Pulse

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Fixture:
    pool: asyncpg.Pool
    hidden: str
    drifted: str
    blind_seq: str


@pytest_asyncio.fixture
async def fx() -> AsyncIterator[Fixture]:
    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    pool = None
    tag = uuid4().hex[:8]
    runtime, other = f"pulse_rt_{tag}", f"pulse_other_{tag}"
    hidden, drifted, blind = f"pulse_hidden_{tag}", f"pulse_drifted_{tag}", f"pulse_blind_{tag}"
    try:
        identity = await admin.fetchrow(
            "SELECT current_database() AS db, system_identifier::text AS system_id "
            "FROM pg_control_system()"
        )
        if dict(identity) != {"db": "universal_db_test", "system_id": expected}:
            pytest.fail("Refusing a database outside this disposable test cluster")
        await admin.execute(f"CREATE ROLE {runtime} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        await admin.execute(f"CREATE ROLE {other} NOLOGIN")
        await admin.execute(f"GRANT USAGE ON SCHEMA public TO {runtime}")
        await admin.execute(f"""
            CREATE TABLE public.{hidden} (id int GENERATED ALWAYS AS IDENTITY, v int);
            INSERT INTO public.{hidden} (v) VALUES (1), (2), (3);
            ALTER TABLE public.{hidden} OWNER TO {other};

            CREATE TABLE public.{drifted} (id serial PRIMARY KEY);
            INSERT INTO public.{drifted} (id) VALUES (10), (20), (50);
            GRANT SELECT ON public.{drifted} TO {runtime};
            GRANT SELECT, UPDATE ON SEQUENCE public.{drifted}_id_seq TO {runtime};

            CREATE TABLE public.{blind} (id serial PRIMARY KEY);
            INSERT INTO public.{blind} (id) VALUES (7);
            GRANT SELECT ON public.{blind} TO {runtime};
        """)
        host_dsn = dsn
        pool = await asyncpg.create_pool(host_dsn, user=runtime, min_size=1, max_size=2)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT current_user") == runtime
            assert (
                await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                is False
            )
        yield Fixture(pool=pool, hidden=hidden, drifted=drifted, blind_seq=f"{blind}_id_seq")
    finally:
        if pool is not None:
            await pool.close()
        for table in (hidden, drifted, blind):
            await admin.execute(f"DROP TABLE IF EXISTS public.{table}")
        await admin.execute(f"DROP OWNED BY {runtime}")
        await admin.execute(f"DROP ROLE IF EXISTS {runtime}")
        await admin.execute(f"DROP ROLE IF EXISTS {other}")
        await admin.close()


def pulse_of(pool: asyncpg.Pool) -> Pulse:
    return Pulse(pool, MagicMock())


@pytest.mark.asyncio
async def test_unreadable_table_is_never_a_candidate(fx: Fixture, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="olympus.pulse"):
        actions = await pulse_of(fx.pool).repair_sequences()

    assert [a for a in actions if fx.hidden in a.target] == []
    assert [a for a in actions if a.detail.get("table") == fx.hidden] == []
    assert not [r for r in caplog.records if fx.hidden in r.getMessage()]


@pytest.mark.asyncio
async def test_readable_drifted_table_is_still_repaired(fx: Fixture) -> None:
    actions = await pulse_of(fx.pool).repair_sequences()

    repaired = [a for a in actions if a.detail.get("table") == fx.drifted]
    assert len(repaired) == 1
    assert repaired[0].outcome == "success"
    assert repaired[0].detail["new"] == 50
    async with fx.pool.acquire() as conn:
        assert await conn.fetchval(f"SELECT last_value FROM public.{fx.drifted}_id_seq") == 50


@pytest.mark.asyncio
async def test_readable_table_with_unreadable_sequence_is_still_skipped(
    fx: Fixture, caplog
) -> None:
    with caplog.at_level(logging.WARNING, logger="olympus.pulse"):
        actions = await pulse_of(fx.pool).repair_sequences()

    skipped = [a for a in actions if fx.blind_seq in a.target]
    assert len(skipped) == 1
    assert skipped[0].outcome == "skipped"
    assert fx.blind_seq in skipped[0].detail["reason"]
    assert [r for r in caplog.records if fx.blind_seq in r.getMessage()]
