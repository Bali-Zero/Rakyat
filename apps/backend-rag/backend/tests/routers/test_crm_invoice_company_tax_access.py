"""Who may read the invoice-company lookups, on real PostgreSQL (synthetic data only).

The two lookups follow the rule of creating a process (DAI-156, DAI-178): a CRM admin
or a ``COMPANY_CREATE_ROLES`` member passes for any client that exists. Every other
role keeps the ownership filter.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import crm_practices
from backend.app.utils import crm_utils
from backend.app.utils.crm_utils import COMPANY_CREATE_ROLES

_SCHEMA_SQL = """
CREATE TABLE clients (
    id SERIAL PRIMARY KEY, full_name VARCHAR NOT NULL, assigned_to VARCHAR,
    deleted_at TIMESTAMPTZ
);
CREATE TABLE practices (
    id SERIAL PRIMARY KEY, client_id INTEGER, created_by VARCHAR, assigned_to VARCHAR
);
CREATE TABLE practice_types (code VARCHAR PRIMARY KEY, category VARCHAR);
CREATE TABLE companies (
    id SERIAL PRIMARY KEY, company_name VARCHAR NOT NULL, npwp_company VARCHAR
);
CREATE TABLE client_company_links (
    id SERIAL PRIMARY KEY, client_id INTEGER NOT NULL, company_id INTEGER NOT NULL,
    role VARCHAR NOT NULL DEFAULT 'shareholder', status VARCHAR DEFAULT 'active', end_date DATE
);
"""

OWNER = "synthetic.owner@example.invalid"
CALLER = "synthetic.caller@example.invalid"
FULL_VIEW = "synthetic.fullview@example.invalid"
UNKNOWN_CLIENT = 987_654
ENDPOINTS = ("search", "suggestion")
PRIVILEGED_ROLES = sorted(COMPANY_CREATE_ROLES)
TAX_ROLES = sorted(role for role in COMPANY_CREATE_ROLES if role.startswith("tax "))


def _user(role: str, email: str = CALLER) -> dict[str, str]:
    return {"email": email, "role": role}


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    dsn = os.environ["TEST_DATABASE_URL"]
    admin = await asyncpg.connect(dsn)
    name = await admin.fetchval("SELECT current_database()")
    if not re.search(r"_test(?:_run_[0-9]+_[0-9]+)?(?:_gw[0-9]+)?$", name):
        await admin.close()
        pytest.fail("A test database or isolated test worker is required")
    schema = "invoice_access_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        await created.execute(_SCHEMA_SQL)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _client(pool: asyncpg.Pool, *, assigned_to: str = OWNER, deleted: bool = False) -> int:
    client_id = await pool.fetchval(
        "INSERT INTO clients (full_name, assigned_to, deleted_at)"
        " VALUES ('Synthetic Person', $1, CASE WHEN $2 THEN now() END) RETURNING id",
        assigned_to,
        deleted,
    )
    company_id = await pool.fetchval(
        "INSERT INTO companies (company_name) VALUES ('PT Synthetic A') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO client_company_links (client_id, company_id) VALUES ($1, $2)",
        client_id,
        company_id,
    )
    return client_id


async def _get(pool: asyncpg.Pool, user: dict, endpoint: str, client_id: int) -> httpx.Response:
    app = FastAPI()
    app.include_router(crm_practices.router)
    app.dependency_overrides[get_database_pool] = lambda: pool
    app.dependency_overrides[get_current_user] = lambda: user
    params: dict[str, str | int] = {"client_id": client_id}
    if endpoint == "search":
        params["q"] = "synthetic"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/api/crm/practices/invoice-company/{endpoint}", params=params)


def _names(response: httpx.Response) -> list[str]:
    return [row["company_name"] for row in response.json()["companies"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", TAX_ROLES)
async def test_tax_team_searches_for_a_client_it_does_not_own(
    pool: asyncpg.Pool, role: str
) -> None:
    client_id = await _client(pool)
    response = await _get(pool, _user(role), "search", client_id)
    assert response.status_code == 200
    assert response.json()["companies"][0]["company_name"] == "PT Synthetic A"
    assert response.json()["companies"][0]["linked"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("role", TAX_ROLES)
async def test_tax_team_gets_the_suggestion_for_a_client_it_does_not_own(
    pool: asyncpg.Pool, role: str
) -> None:
    client_id = await _client(pool)
    response = await _get(pool, _user(role), "suggestion", client_id)
    assert response.status_code == 200
    assert _names(response) == ["PT Synthetic A"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_tax_role_is_read_like_the_create_rule_reads_it(
    pool: asyncpg.Pool, endpoint: str
) -> None:
    client_id = await _client(pool)
    response = await _get(pool, _user("  Tax Lead "), endpoint, client_id)
    assert response.status_code == 200
    assert _names(response) == ["PT Synthetic A"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("role", ["staff", "consultant", "tax assistant", ""])
async def test_other_roles_stay_out_of_a_client_they_do_not_own(
    pool: asyncpg.Pool, endpoint: str, role: str
) -> None:
    client_id = await _client(pool)
    response = await _get(pool, _user(role), endpoint, client_id)
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_owner_with_an_ordinary_role_keeps_access(pool: asyncpg.Pool, endpoint: str) -> None:
    client_id = await _client(pool, assigned_to=CALLER)
    response = await _get(pool, _user("staff"), endpoint, client_id)
    assert response.status_code == 200
    assert _names(response) == ["PT Synthetic A"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_practice_owner_with_an_ordinary_role_keeps_access(
    pool: asyncpg.Pool, endpoint: str
) -> None:
    client_id = await _client(pool)
    await pool.execute(
        "INSERT INTO practices (client_id, created_by) VALUES ($1, $2)", client_id, CALLER
    )
    response = await _get(pool, _user("staff"), endpoint, client_id)
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("role", [*PRIVILEGED_ROLES, "staff"])
async def test_unknown_client_is_404_for_every_role(
    pool: asyncpg.Pool, endpoint: str, role: str
) -> None:
    response = await _get(pool, _user(role), endpoint, UNKNOWN_CLIENT)
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("role", PRIVILEGED_ROLES)
async def test_deleted_client_is_404_for_the_roles_that_skip_the_filter(
    pool: asyncpg.Pool, endpoint: str, role: str
) -> None:
    client_id = await _client(pool, deleted=True)
    response = await _get(pool, _user(role), endpoint, client_id)
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_full_view_account_keeps_access_and_gets_404_on_an_unknown_client(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setattr(crm_utils, "_practices_full_view_emails", lambda: {FULL_VIEW})
    user = _user("staff", FULL_VIEW)
    client_id = await _client(pool)
    assert (await _get(pool, user, endpoint, client_id)).status_code == 200
    assert (await _get(pool, user, endpoint, UNKNOWN_CLIENT)).status_code == 404
