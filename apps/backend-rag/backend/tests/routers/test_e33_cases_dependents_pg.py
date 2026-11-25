"""E33 principal case lists its dependents: the read-time query on a real PostgreSQL.

A throwaway schema in the TEST database holds migration 259 and invented clients; cases are
created through the real ``POST /api/e33/cases`` and read back through ``GET``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import backend.app.routers.e33_cases as e33_cases_module
from backend.app.dependencies import get_current_user, get_database_pool
from backend.db.migration_base import split_migration_sql
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

MIGRATION = (
    Path(e33_cases_module.__file__).resolve().parents[2]
    / "db"
    / "migrations_v2"
    / "259_e33_case_lifecycle.sql"
)

ADMIN = {"id": "1", "email": "zz-admin@example.test", "role": "admin"}
STAFF = {"id": "2", "email": "zz-staff@example.test", "role": "team"}

PREREQ = """
CREATE TABLE clients (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name text NOT NULL,
    assigned_to text,
    deleted_at timestamptz
);
CREATE TABLE practices (id integer PRIMARY KEY);
"""


@pytest_asyncio.fixture
async def pool():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL required for the real PostgreSQL dependents proof")
    admin = await asyncpg.connect(dsn)
    name = await admin.fetchval("SELECT current_database()")
    if not re.search(r"(?:^|_)test(?:_run_[0-9]+_[0-9]+)?(?:_gw[0-9]+)?$", name):
        await admin.close()
        pytest.fail("A test database or isolated test worker is required")
    schema = "e33_dependents_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
        init=init_prod_shaped_connection,
        statement_cache_size=0,
    )
    try:
        async with created.acquire() as c:
            await c.execute(PREREQ)
            forward, _ = split_migration_sql(MIGRATION.read_text())
            await c.execute(forward)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _client(pool, name: str, assigned_to: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            "INSERT INTO clients (full_name, assigned_to) VALUES ($1, $2) RETURNING id",
            name,
            assigned_to,
        )


def _api(pool, user: dict[str, str]) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(e33_cases_module.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_database_pool] = lambda: pool
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://test",
    )


async def _create(api, **body) -> str:
    resp = await api.post("/api/e33/cases", json={"basis": "deposit", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()["case_id"]


@pytest_asyncio.fixture
async def family(pool):
    """A principal (assigned to staff) and three dependents; one belongs to someone else."""
    principal_client = await _client(pool, "ZzPrincipal Person", STAFF["email"])
    spouse = await _client(pool, "ZzSpouse Person", STAFF["email"])
    child = await _client(pool, "ZzChild Person", STAFF["email"])
    hidden = await _client(pool, "ZzHidden Person", "zz-other@example.test")
    async with _api(pool, ADMIN) as api:
        principal = await _create(api, client_id=principal_client)
        lone = await _create(api, client_id=await _client(pool, "ZzLone Person", ADMIN["email"]))
        d_spouse = await _create(
            api, client_id=spouse, dependent_code="E31B", principal_case_id=principal
        )
        d_child = await _create(
            api, client_id=child, dependent_code="E31E", principal_case_id=principal
        )
        d_hidden = await _create(
            api, client_id=hidden, dependent_code="E31H", principal_case_id=principal
        )
    return {
        "principal": principal,
        "lone": lone,
        "spouse": d_spouse,
        "child": d_child,
        "hidden": d_hidden,
        "spouse_client": spouse,
    }


@pytest.mark.asyncio
@pytest.mark.integration
async def test_principal_detail_lists_its_dependents_in_creation_order(pool, family) -> None:
    async with _api(pool, ADMIN) as api:
        resp = await api.get(f"/api/e33/cases/{family['principal']}")
    assert resp.status_code == 200
    deps = resp.json()["dependents"]
    assert [d["case_id"] for d in deps] == [family["spouse"], family["child"], family["hidden"]]
    assert deps[0] == {
        "case_id": family["spouse"],
        "code": "E31B",
        "client_id": family["spouse_client"],
        "client_name": "ZzSpouse Person",
        "stage": "fit_memo",
        "relationship": None,
    }
    assert deps[1]["client_name"] == "ZzChild Person"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_principal_without_dependents_gets_an_empty_list(pool, family) -> None:
    async with _api(pool, ADMIN) as api:
        resp = await api.get(f"/api/e33/cases/{family['lone']}")
    assert resp.status_code == 200
    assert resp.json()["dependents"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dependent_the_caller_may_not_see_is_not_listed(pool, family) -> None:
    async with _api(pool, STAFF) as api:
        resp = await api.get(f"/api/e33/cases/{family['principal']}")
    assert resp.status_code == 200
    ids = [d["case_id"] for d in resp.json()["dependents"]]
    assert ids == [family["spouse"], family["child"]]
    assert family["hidden"] not in ids


@pytest.mark.asyncio
@pytest.mark.integration
async def test_advance_response_keeps_the_dependents(pool, family) -> None:
    async with _api(pool, ADMIN) as api:
        resp = await api.post(
            f"/api/e33/cases/{family['principal']}/advance", json={"to_stage": "bank_precheck"}
        )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["dependents"]) == 3
