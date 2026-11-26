"""A company the client has left gives them no tax calendar.

The portal shows the statutory tax dates only to a client with a tax footprint: a company
link, a tax practice, or an NPWP. Every reader of the dashboard takes a company link as
live when it is active and not ended; the footprint took any link at all. A client whose
only link was closed saw no company on the page and still a countdown to its taxes.

The predicate is SQL, so it runs here against a real PostgreSQL. Invented rows only.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncGenerator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.portal._mixins import dashboard as dashboard_module
from backend.services.portal.portal_service import PortalService

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_CLIENT = 900077
_OTHER_CLIENT = 900078
_USER = {"client_id": _CLIENT, "email": "person.one@example.test"}
_CLOSED_ON = date(2026, 11, 9)


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = pytest.mark.skipif(
    not _is_safe_test_database(_DB_URL), reason="needs a local test database"
)

# The columns the footprint and the company list read, with the production default of the
# link's status.
_TABLES = """
CREATE TABLE clients (id integer PRIMARY KEY, npwp text);
CREATE TABLE companies (
    id serial PRIMARY KEY,
    company_name text NOT NULL,
    company_type text,
    nib text,
    npwp_company text,
    kbli_code text,
    status text
);
CREATE TABLE client_company_links (
    id serial PRIMARY KEY,
    client_id integer NOT NULL,
    company_id integer NOT NULL,
    role text,
    is_primary boolean NOT NULL DEFAULT false,
    ownership_percentage numeric,
    status text NOT NULL DEFAULT 'active',
    end_date date,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE practice_types (id serial PRIMARY KEY, category text);
CREATE TABLE practices (
    id serial PRIMARY KEY,
    client_id integer NOT NULL,
    practice_type_id integer
);
"""


@pytest_asyncio.fixture
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    admin = await asyncpg.connect(_DB_URL)
    schema = "portal_tax_footprint_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=3, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as c:
            await c.execute(_TABLES)
            await c.execute("INSERT INTO clients (id) VALUES ($1), ($2)", _CLIENT, _OTHER_CLIENT)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.fixture
def service(pool: asyncpg.Pool) -> PortalService:
    return PortalService(pool)


async def _link(
    pool: asyncpg.Pool, status: str, end_date: date | None, *, client: int = _CLIENT
) -> None:
    async with pool.acquire() as c:
        company = await c.fetchval(
            "INSERT INTO companies (company_name) VALUES ('PT Contoh Satu') RETURNING id"
        )
        await c.execute(
            "INSERT INTO client_company_links (client_id, company_id, status, end_date) "
            "VALUES ($1, $2, $3, $4)",
            client,
            company,
            status,
            end_date,
        )


async def _practice(pool: asyncpg.Pool, category: str) -> None:
    async with pool.acquire() as c:
        kind = await c.fetchval(
            "INSERT INTO practice_types (category) VALUES ($1) RETURNING id", category
        )
        await c.execute(
            "INSERT INTO practices (client_id, practice_type_id) VALUES ($1, $2)", _CLIENT, kind
        )


async def _npwp(pool: asyncpg.Pool, value: str) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE clients SET npwp = $1 WHERE id = $2", value, _CLIENT)


async def _footprint(service: PortalService, pool: asyncpg.Pool) -> bool:
    """The footprint as the three pages ask it. The function answers True when its query
    FAILS, so a True here counts only if the query really ran: no warning may be logged."""
    warnings: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warnings.append(record)

    handler = _Keep(level=logging.WARNING)
    dashboard_module.logger.addHandler(handler)
    try:
        async with pool.acquire() as conn:
            answer = await service._has_tax_footprint(conn, _CLIENT)
    finally:
        dashboard_module.logger.removeHandler(handler)
    assert warnings == [], [record.getMessage() for record in warnings]
    return answer


async def _has_a_company_on_the_page(service: PortalService) -> bool:
    return bool(await service.get_companies(_CLIENT, current_user=_USER))


# ---------------------------------------------------------------- the link

LINKS = {
    "one live link": ([("active", None)], True),
    "a closed link and a live one": ([("inactive", None), ("active", None)], True),
    "no link": ([], False),
    "a link closed by its status": ([("inactive", None)], False),
    "a link closed by status and date": ([("inactive", _CLOSED_ON)], False),
    "a link with an end date, still called active": ([("active", _CLOSED_ON)], False),
    "two closed links": ([("inactive", None), ("inactive", _CLOSED_ON)], False),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("links", "expected"), LINKS.values(), ids=LINKS.keys())
async def test_only_a_live_link_gives_a_tax_calendar(
    service: PortalService,
    pool: asyncpg.Pool,
    links: list[tuple[str, date | None]],
    expected: bool,
) -> None:
    for status, end_date in links:
        await _link(pool, status, end_date)

    assert await _footprint(service, pool) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("links", "expected"), LINKS.values(), ids=LINKS.keys())
async def test_the_calendar_and_the_company_list_agree_on_what_a_link_is(
    service: PortalService,
    pool: asyncpg.Pool,
    links: list[tuple[str, date | None]],
    expected: bool,
) -> None:
    """With no tax practice and no NPWP, the client gets a countdown exactly when the page
    shows them a company."""
    for status, end_date in links:
        await _link(pool, status, end_date)

    on_the_page = await _has_a_company_on_the_page(service)

    assert on_the_page is expected
    assert await _footprint(service, pool) is on_the_page


@pytest.mark.asyncio
async def test_a_link_of_another_client_is_not_this_clients_footprint(
    service: PortalService, pool: asyncpg.Pool
) -> None:
    await _link(pool, "active", None, client=_OTHER_CLIENT)

    assert await _footprint(service, pool) is False


# ---------------------------------------------------------------- the other two reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("links", [[], [("inactive", None)]], ids=["no link", "a closed link"])
async def test_a_tax_practice_still_gives_a_tax_calendar(
    service: PortalService, pool: asyncpg.Pool, links: list[tuple[str, date | None]]
) -> None:
    for status, end_date in links:
        await _link(pool, status, end_date)
    await _practice(pool, "tax")

    assert await _footprint(service, pool) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("links", [[], [("inactive", None)]], ids=["no link", "a closed link"])
async def test_an_npwp_still_gives_a_tax_calendar(
    service: PortalService, pool: asyncpg.Pool, links: list[tuple[str, date | None]]
) -> None:
    for status, end_date in links:
        await _link(pool, status, end_date)
    await _npwp(pool, "00.000.000.0-000.000")

    assert await _footprint(service, pool) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "what",
    ["a practice that is not a tax one", "a blank NPWP"],
)
async def test_a_closed_link_gives_no_calendar_beside_what_is_no_reason_either(
    service: PortalService, pool: asyncpg.Pool, what: str
) -> None:
    await _link(pool, "inactive", None)
    if what == "a blank NPWP":
        await _npwp(pool, "   ")
    else:
        await _practice(pool, "immigration")

    assert await _footprint(service, pool) is False


# ---------------------------------------------------------------- when the query fails


@pytest.mark.asyncio
async def test_a_query_that_fails_still_shows_the_calendar() -> None:
    """Unchanged, and deliberate: hiding a real deadline is the worse of the two errors."""
    conn: Any = AsyncMock()
    conn.fetchval.side_effect = asyncpg.PostgresError("the query failed")

    assert await PortalService(AsyncMock())._has_tax_footprint(conn, _CLIENT) is True
