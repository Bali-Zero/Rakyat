"""The portal draft lookup reads reports through the caller's linked companies.

Real PostgreSQL in a per-test schema (skips without TEST_DATABASE_URL). Synthetic values only.
``lkpm_reports.client_id`` holds a company id for imported rows and a client id for rows created
one by one, so a portal caller is scoped by ``company_id`` through active ``client_company_links``.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request

from backend.app.routers import lkpm as lkpm_router
from backend.db.migration_base import split_migration_sql
from backend.services.compliance.lkpm_service import LKPMService

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations_v2/132_legacy_lkpm_reports.sql"
YEAR = 2026
PORTAL_CLIENT = 900005
OWN_COMPANY = 900010
OTHER_COMPANY = 900777
PORTAL_USER = "11111111-2222-3333-4444-555555555555"


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL required for real PostgreSQL proof")
    admin = await asyncpg.connect(dsn)
    name = await admin.fetchval("SELECT current_database()")
    if not re.fullmatch(r"(?:\w+_)?test(?:_run_[0-9]+_[0-9]+)?(?:_gw[0-9]+)?", name):
        await admin.close()
        pytest.fail("A test database or isolated test worker is required")
    schema = "lkpm_draft_scope_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        forward, _ = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
        async with created.acquire() as conn:
            await conn.execute(forward)
            await conn.execute(
                """
                CREATE TABLE companies (id integer PRIMARY KEY, company_name text);
                CREATE TABLE client_company_links (
                    client_id integer, company_id integer, status text
                );
                CREATE TABLE team_members (
                    id uuid PRIMARY KEY, role text, active boolean,
                    portal_access boolean, linked_client_id integer
                );
                """
            )
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _report(pool: asyncpg.Pool, client_id: int, company_id: int | None, revenue: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO lkpm_reports (client_id, company_id, quarter, year, quarterly_revenue) "
            "VALUES ($1, $2, 'Q1', $3, $4)",
            client_id,
            company_id,
            YEAR,
            revenue,
        )


async def _link(pool: asyncpg.Pool, client_id: int, company_id: int, status: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO client_company_links (client_id, company_id, status) VALUES ($1, $2, $3)",
            client_id,
            company_id,
            status,
        )


async def _collision(pool: asyncpg.Pool) -> None:
    """Own company 900010 has its report; an imported report of 900777 carries client_id 900005."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO companies (id, company_name) VALUES "
            "(900010, 'PT Contoh Satu'), (900777, 'PT Contoh Dua')"
        )
        await conn.execute(
            "INSERT INTO team_members VALUES ($1::uuid, 'client', TRUE, TRUE, $2)",
            PORTAL_USER,
            PORTAL_CLIENT,
        )
    await _link(pool, PORTAL_CLIENT, OWN_COMPANY, "active")
    await _report(pool, OWN_COMPANY, OWN_COMPANY, 1000)
    await _report(pool, PORTAL_CLIENT, OTHER_COMPANY, 7777)


def _request(path: str) -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""}
    )


async def _portal_endpoint(pool: asyncpg.Pool) -> dict:
    return await lkpm_router.get_draft(
        client_id=0,
        quarter="Q1",
        request=_request("/api/v1/lkpm/draft/0/Q1"),
        year=YEAR,
        current_user={"user_id": PORTAL_USER, "email": "portal@example.test", "role": "client"},
        db_pool=pool,
    )


@pytest.mark.asyncio
async def test_portal_endpoint_returns_the_callers_own_company_draft(pool: asyncpg.Pool) -> None:
    await _collision(pool)

    response = await _portal_endpoint(pool)

    assert response["draft"]["company_id"] == OWN_COMPANY, (
        f"portal draft belongs to company {response['draft']['company_id']}"
    )
    assert response["draft"]["quarterly_revenue"] == 1000


@pytest.mark.asyncio
async def test_service_method_scopes_by_active_linked_company(pool: asyncpg.Pool) -> None:
    await _collision(pool)
    service = LKPMService(pool)

    draft = await service.get_draft_for_portal_client(PORTAL_CLIENT, "Q1", YEAR)

    assert draft is not None
    assert draft.company_id == OWN_COMPANY
    keyed = await service.get_draft(PORTAL_CLIENT, "Q1", YEAR)
    assert keyed is not None and keyed.company_id == OTHER_COMPANY


@pytest.mark.asyncio
async def test_portal_client_without_an_active_link_gets_nothing(pool: asyncpg.Pool) -> None:
    await _collision(pool)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE client_company_links SET status = 'inactive'")
    service = LKPMService(pool)

    assert await service.get_draft_for_portal_client(PORTAL_CLIENT, "Q1", YEAR) is None
    with pytest.raises(HTTPException) as exc_info:
        await _portal_endpoint(pool)
    assert exc_info.value.status_code == 404

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM client_company_links")
    assert await service.get_draft_for_portal_client(PORTAL_CLIENT, "Q1", YEAR) is None


async def _portal_user(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO team_members VALUES ($1::uuid, 'client', TRUE, TRUE, $2)",
            PORTAL_USER,
            PORTAL_CLIENT,
        )


@pytest.mark.asyncio
async def test_a_draft_generated_for_the_caller_is_visible_on_the_portal_path(
    pool: asyncpg.Pool,
) -> None:
    # A draft generated for a client carries no company; its client_id is the caller's own.
    await _portal_user(pool)
    await _link(pool, PORTAL_CLIENT, OWN_COMPANY, "active")
    await _report(pool, PORTAL_CLIENT, None, 5555)

    draft = await LKPMService(pool).get_draft_for_portal_client(PORTAL_CLIENT, "Q1", YEAR)
    response = await _portal_endpoint(pool)

    assert draft is not None and draft.company_id is None
    assert draft.quarterly_revenue == 5555
    assert response["draft"]["quarterly_revenue"] == 5555


@pytest.mark.asyncio
async def test_a_draft_generated_for_another_client_is_not_visible(pool: asyncpg.Pool) -> None:
    await _link(pool, PORTAL_CLIENT, OWN_COMPANY, "active")
    await _report(pool, PORTAL_CLIENT + 1, None, 6666)

    assert await LKPMService(pool).get_draft_for_portal_client(PORTAL_CLIENT, "Q1", YEAR) is None


@pytest.mark.asyncio
async def test_a_linked_company_report_comes_before_the_callers_generated_draft(
    pool: asyncpg.Pool,
) -> None:
    await _link(pool, PORTAL_CLIENT, OWN_COMPANY, "active")
    await _report(pool, PORTAL_CLIENT, None, 5555)
    await _report(pool, OWN_COMPANY, OWN_COMPANY, 1000)

    draft = await LKPMService(pool).get_draft_for_portal_client(PORTAL_CLIENT, "Q1", YEAR)

    assert draft is not None and draft.company_id == OWN_COMPANY


@pytest.mark.asyncio
async def test_several_linked_companies_return_the_lowest_company_id(pool: asyncpg.Pool) -> None:
    await _link(pool, PORTAL_CLIENT, 900030, "active")
    await _link(pool, PORTAL_CLIENT, OWN_COMPANY, "active")
    await _report(pool, 900030, 900030, 3000)
    await _report(pool, OWN_COMPANY, OWN_COMPANY, 1000)

    draft = await LKPMService(pool).get_draft_for_portal_client(PORTAL_CLIENT, "Q1", YEAR)

    assert draft is not None and draft.company_id == OWN_COMPANY


@pytest.mark.asyncio
async def test_staff_endpoint_still_selects_a_row_by_its_own_key(pool: asyncpg.Pool) -> None:
    await _collision(pool)

    response = await lkpm_router.get_draft(
        client_id=PORTAL_CLIENT,
        quarter="Q1",
        request=_request(f"/api/v1/lkpm/draft/{PORTAL_CLIENT}/Q1"),
        year=YEAR,
        current_user={"email": "admin@example.test", "role": "admin"},
        db_pool=pool,
    )

    assert response["draft"]["company_id"] == OTHER_COMPANY
