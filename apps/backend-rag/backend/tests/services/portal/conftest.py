"""Real-Postgres fixtures for the portal account services.

Each test gets a throwaway schema cloned from the migrated test database, so the
unique e-mail index, the foreign keys and the case-sensitivity of the real
`team_members` table are the ones under test, and nothing leaks between tests.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import asyncpg
import pytest
import pytest_asyncio

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://test:test@localhost:5433/test")


@pytest_asyncio.fixture
async def portal_pg_pool() -> AsyncIterator[asyncpg.Pool]:
    try:
        admin = await asyncpg.connect(_DSN)
    except Exception:
        pytest.skip("isolated test Postgres is not reachable")
    schema = f"portal_t_{uuid.uuid4().hex[:12]}"
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(
            f"""
            SET search_path = {schema};
            CREATE TABLE clients (LIKE public.clients INCLUDING ALL);
            CREATE TABLE team_members (LIKE public.team_members INCLUDING ALL);
            ALTER TABLE team_members ALTER COLUMN id SET DEFAULT gen_random_uuid()::text;
            ALTER TABLE team_members
                ADD COLUMN linked_client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL;
            ALTER TABLE team_members ADD COLUMN portal_access BOOLEAN DEFAULT false;
            CREATE TABLE client_invitations (
                id SERIAL PRIMARY KEY,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                email VARCHAR(255) NOT NULL,
                token VARCHAR(255) UNIQUE NOT NULL,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                used_at TIMESTAMP WITH TIME ZONE,
                created_by VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE TABLE magic_link_tokens (LIKE public.magic_link_tokens INCLUDING ALL);
            """
        )
        pool = await asyncpg.create_pool(
            _DSN, min_size=1, max_size=3, server_settings={"search_path": schema}
        )
        try:
            yield pool
        finally:
            await pool.close()
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


@pytest.fixture
def make_client(portal_pg_pool) -> Callable[..., Awaitable[int]]:
    async def _make(name: str, email: str | None = None, *, deleted: bool = False) -> int:
        return await portal_pg_pool.fetchval(
            "INSERT INTO clients (full_name, email, deleted_at)"
            " VALUES ($1, $2, CASE WHEN $3 THEN NOW() END) RETURNING id",
            name,
            email,
            deleted,
        )

    return _make
