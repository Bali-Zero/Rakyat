"""Migration 354 and the card read path, against a real Postgres.

Collected under backend/tests/ and run by the `Backend Shard N` jobs against their
postgres service, like the other tests in this directory (no skip, no xfail). The
test works in its own schema (`hrcard354`) so it never touches shared tables.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

pytestmark = pytest.mark.integration

SCHEMA = "hrcard354"
MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "354_hr_employees_profile.sql"
)

_HR_DDL = """
CREATE TABLE team_members (
    id VARCHAR(36) PRIMARY KEY, full_name TEXT, email TEXT UNIQUE, role TEXT, department TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE, avatar TEXT);
CREATE TABLE hr_employees (
    id SERIAL PRIMARY KEY,
    team_member_id VARCHAR(36) NOT NULL UNIQUE REFERENCES team_members(id) ON DELETE RESTRICT,
    hire_date DATE NOT NULL,
    base_salary_idr BIGINT NOT NULL CHECK (base_salary_idr >= 0),
    bank_name VARCHAR(100), bank_account VARCHAR(100), bank_account_holder VARCHAR(255),
    npwp VARCHAR(32), ptkp_status VARCHAR(10) NOT NULL DEFAULT 'TK/0',
    bpjs_kesehatan_number VARCHAR(32), bpjs_ketenagakerjaan_number VARCHAR(32),
    is_active BOOLEAN NOT NULL DEFAULT TRUE, notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
"""


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


def _forward() -> str:
    return split_migration_sql(MIGRATION.read_text(encoding="utf-8"))[0]


def _rollback() -> str:
    rollback = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))[1]
    assert rollback, "354 must carry a rollback section"
    return rollback


async def _init(conn: asyncpg.Connection) -> None:
    """The production connection init (jsonb codec registration)."""
    await init_prod_shaped_connection(conn)


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    admin = await asyncpg.connect(_dsn())
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('r2-migration-354-hr-card-test'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        p = await asyncpg.create_pool(
            _dsn(),
            init=_init,
            server_settings={"search_path": SCHEMA},  # survives the pool's RESET on release
            statement_cache_size=0,
            min_size=1,
            max_size=4,
        )
        try:
            async with p.acquire() as conn:
                await conn.execute(_HR_DDL)
                await conn.execute(_forward())
                await conn.execute(
                    "INSERT INTO team_members VALUES "
                    "('tm-a','Fake A','fake.a@balizero.com','staff','ops'),"
                    "('tm-b','Fake B','fake.b@balizero.com','staff','ops'),"
                    "('tm-c','Fake C','fake.c@balizero.com','client','ops'),"
                    "('tm-t1','Fake Twin','fake.twin@balizero.com','staff','ops'),"
                    "('tm-t2','Fake Twin Upper','Fake.Twin@balizero.com','client','ops'),"
                    "('tm-i','Fake Gone','fake.gone@balizero.com','staff','ops')"
                )
                await conn.execute(
                    "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, npwp) "
                    "VALUES ('tm-a', '2025-01-01', 5, '10.01')"
                )
                await conn.execute(
                    "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, profile) "
                    "VALUES ('tm-i', '2025-01-01', 3, '{\"unit\": \"gone\"}')"
                )
                await conn.execute(
                    "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, notes, profile) "
                    "VALUES ('tm-t1', '2025-01-01', 1, 'n1', '{\"unit\": \"mine\"}'), "
                    "('tm-t2', '2025-01-01', 2, 'n2', '{\"unit\": \"theirs\"}')"
                )
            yield p
        finally:
            await p.close()
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.execute("SELECT pg_advisory_unlock(hashtext('r2-migration-354-hr-card-test'))")
        await admin.close()


@pytest.mark.asyncio
async def test_migration_adds_the_column_and_rolls_back(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_forward())  # idempotent re-apply
        assert await conn.fetchval("SELECT profile::text FROM hr_employees LIMIT 1") == "{}"
        assert await conn.fetchval(
            "SELECT col_description('hr_employees'::regclass, attnum) IS NOT NULL "
            "FROM pg_attribute WHERE attrelid = 'hr_employees'::regclass AND attname = 'profile'"
        )
        await conn.execute(_rollback())
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'hr_employees'::regclass "
            "AND attname = 'profile' AND NOT attisdropped)"
        )


@pytest.mark.asyncio
async def test_migration_fails_loudly_when_hr_employees_is_absent(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE hr_employees")
        with pytest.raises(asyncpg.UndefinedTableError):
            await conn.execute(_forward())


@pytest.mark.asyncio
async def test_me_card_returns_the_authenticated_rows_card_never_the_case_twins(
    pool: asyncpg.Pool,
) -> None:
    import httpx
    from fastapi import FastAPI

    from backend.app.dependencies import get_current_user, get_database_pool
    from backend.app.routers import hr as hr_router

    def client_for(session: dict[str, Any]) -> httpx.AsyncClient:
        app = FastAPI()
        app.include_router(hr_router.router)
        app.dependency_overrides[get_current_user] = lambda: session
        app.dependency_overrides[get_database_pool] = lambda: pool
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    staff = {"id": "tm-t1", "email": "fake.twin@balizero.com", "role": "staff"}
    async with client_for(staff) as c:
        res = await c.get("/api/hr/me/card")
    assert res.status_code == 200
    body = res.json()
    assert (
        body["unit"] == "mine" and body["base_salary_idr"] == 1 and body["full_name"] == "Fake Twin"
    )
    assert "notes" not in body and "theirs" not in res.text and "n1" not in res.text

    # The twin session (a client row) is refused outright, and a session whose email is
    # the OTHER case still gets only its own row's card, never the lower-case one.
    twin = {"id": "tm-t2", "email": "Fake.Twin@balizero.com", "role": "client"}
    async with client_for(twin) as c:
        assert (await c.get("/api/hr/me/card")).status_code == 403
    twin_staff = {**twin, "role": "staff"}  # a forged role in the session changes nothing
    async with client_for(twin_staff) as c:
        res = await c.get("/api/hr/me/card")
    assert (
        res.status_code == 404
    )  # the DB row is a client: no staff card, whatever the session says
    assert "mine" not in res.text


@pytest.mark.asyncio
async def test_get_card_for_member_is_primary_key_only_and_staff_only(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    card = await service.get_card_for_member("tm-t1")
    assert card is not None and card["profile"] == {"unit": "mine"} and "notes" not in card
    assert await service.get_card_for_member("fake.twin@balizero.com") is None  # an email is no key
    assert await service.get_card_for_member("tm-t2") is None  # a client row has no staff card
    assert await service.get_card_for_member("tm-b") is None  # no hr row


@pytest.mark.asyncio
async def test_a_stored_string_profile_and_odd_contract_still_read(
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    from backend.app.services.hr.hr_card_models import member_card
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE hr_employees SET profile = '\"x\"'::jsonb WHERE id = 1")
    with caplog.at_level("DEBUG"):
        card = await service.get_card_for_member("tm-a")
        assert card is not None and member_card(card).legal_name is None
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hr_employees SET profile = $1::text::jsonb WHERE id = 1",
                json.dumps({"contract": {"number": "ZQXLEAK\nX"}}),
            )
        card = await service.get_card_for_member("tm-a")
        assert card is not None and member_card(card).contract.number == "ZQXLEAK\nX"
    assert all("ZQXLEAK" not in r.getMessage() for r in caplog.records)


# ─── the REAL authentication path: signed token -> HybridAuthMiddleware -> deps.auth ───


@pytest.fixture
def real_auth_client(pool: asyncpg.Pool):
    """The hr router behind the production middleware and dependency, nothing overridden
    except the database pool. The revocation store answers 'not revoked' (as the middleware
    unit tests do); tokens are signed with the app's own function."""
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock

    import httpx
    from fastapi import FastAPI
    from middleware.hybrid_auth import HybridAuthMiddleware

    from backend.app.dependencies import get_database_pool
    from backend.app.routers import hr as hr_router
    from backend.app.routers.auth import create_access_token

    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)
    redis.get = AsyncMock(return_value=None)
    manager = MagicMock()
    manager.get_async_client.return_value = redis
    app = FastAPI()
    app.include_router(hr_router.router)
    app.add_middleware(HybridAuthMiddleware)
    app.dependency_overrides[get_database_pool] = lambda: pool

    def get(sub: str, email: str, role: str):
        token = create_access_token(
            {"sub": sub, "email": email, "role": role}, expires_delta=timedelta(minutes=5)
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        return client, {"Authorization": f"Bearer {token}"}

    with patch("backend.core.redis_manager.RedisManager.get_instance", return_value=manager):
        yield get


@pytest.mark.asyncio
async def test_real_auth_path_active_staff_gets_own_card(real_auth_client) -> None:
    client, headers = real_auth_client("tm-t1", "fake.twin@balizero.com", "member")
    async with client:
        res = await client.get("/api/hr/me/card", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["unit"] == "mine" and "theirs" not in res.text


@pytest.mark.asyncio
async def test_real_auth_path_a_deactivated_member_with_a_valid_token_gets_404(
    pool: asyncpg.Pool, real_auth_client
) -> None:
    client, headers = real_auth_client("tm-i", "fake.gone@balizero.com", "member")
    async with client:
        assert (await client.get("/api/hr/me/card", headers=headers)).status_code == 200
        async with pool.acquire() as conn:
            await conn.execute("UPDATE team_members SET active = false WHERE id = 'tm-i'")
        res = await client.get("/api/hr/me/card", headers=headers)  # the token is still valid
    assert res.status_code == 404 and "gone" not in res.text


@pytest.mark.asyncio
async def test_real_auth_path_a_staff_claim_on_a_client_row_gets_no_card(
    real_auth_client,
) -> None:
    client, headers = real_auth_client("tm-t2", "Fake.Twin@balizero.com", "member")
    async with client:
        res = await client.get("/api/hr/me/card", headers=headers)
    assert res.status_code in (403, 404) and "theirs" not in res.text


@pytest.mark.asyncio
async def test_a_client_claim_is_refused_and_a_forged_id_matches_no_row(real_auth_client) -> None:
    client, headers = real_auth_client("tm-t1", "fake.twin@balizero.com", "client")
    async with client:
        assert (await client.get("/api/hr/me/card", headers=headers)).status_code == 403
    client, headers = real_auth_client("fake.twin@balizero.com", "fake.twin@balizero.com", "member")
    async with client:  # an email in the id slot is no primary key
        assert (await client.get("/api/hr/me/card", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_get_card_for_member_requires_an_active_member(pool: asyncpg.Pool) -> None:
    from backend.app.services.hr.hr_service import HRService

    service = HRService(pool)
    assert await service.get_card_for_member("tm-i") is not None
    async with pool.acquire() as conn:
        await conn.execute("UPDATE team_members SET active = false WHERE id = 'tm-i'")
    assert await service.get_card_for_member("tm-i") is None
