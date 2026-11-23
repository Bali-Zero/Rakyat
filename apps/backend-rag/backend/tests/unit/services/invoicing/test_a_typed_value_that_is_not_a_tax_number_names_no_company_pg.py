"""A typed value that is not a tax number names no invoice company.

The NPWP field of the invoice-to company is free text. Whatever was typed there was compared
for equality and stored, so "-" became the identity of the first company typed with it, and
the next company typed with "-" resolved to that first one: its client was linked to it and
its invoice was billed to it. A value with fewer than 15 digits is now refused before anything
is read or written.

Real PostgreSQL, in a schema of its own, with the unique index production has on the column.
Invented rows.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.invoicing.invoice_company import (
    InvoiceCompanyError,
    resolve_invoice_company,
)

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_ACTOR = "staff.one@example.test"


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

# The columns the resolver reads and writes, with the production unique index on the number.
_TABLES = """
CREATE TABLE companies (
    id SERIAL PRIMARY KEY, company_name VARCHAR NOT NULL,
    company_type VARCHAR NOT NULL DEFAULT 'PT PMA', npwp_company VARCHAR UNIQUE,
    registered_address TEXT, created_by VARCHAR, updated_by VARCHAR,
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE client_company_links (
    id SERIAL PRIMARY KEY, client_id INTEGER NOT NULL, company_id INTEGER NOT NULL,
    role VARCHAR NOT NULL DEFAULT 'shareholder', status VARCHAR DEFAULT 'active',
    UNIQUE (client_id, company_id)
);
"""


@pytest_asyncio.fixture
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    admin = await asyncpg.connect(_DB_URL)
    schema = "typed_npwp_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as conn:
            await conn.execute(_TABLES)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _resolve(pool: asyncpg.Pool, client_id: int, name: str, npwp: str | None) -> int:
    async with pool.acquire() as conn, conn.transaction():
        return await resolve_invoice_company(
            conn, client_id, company_id=None, name=name, npwp=npwp, actor=_ACTOR
        )


async def _written(pool: asyncpg.Pool) -> tuple[int, int]:
    return (
        await pool.fetchval("SELECT count(*) FROM companies"),
        await pool.fetchval("SELECT count(*) FROM client_company_links"),
    )


_NOT_A_TAX_NUMBER = ["-", "0", "n/a", "belum ada", "12.345.678.9-012.34", "0000"]


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", _NOT_A_TAX_NUMBER)
async def test_a_typed_value_with_too_few_digits_is_refused_and_nothing_is_written(
    pool: asyncpg.Pool, typed: str
) -> None:
    with pytest.raises(InvoiceCompanyError) as refused:
        await _resolve(pool, 1, "PT Synthetic Alpha", typed)

    assert refused.value.status_code == 422
    assert "invoice_company_npwp" in refused.value.detail
    assert await _written(pool) == (0, 0)


@pytest.mark.asyncio
async def test_two_companies_typed_with_the_same_placeholder_never_become_one(
    pool: asyncpg.Pool,
) -> None:
    # What main did: the first call stored "-", the second resolved to the first company.
    ids: list[int] = []
    for client_id, name in ((1, "PT Synthetic Alpha"), (2, "PT Synthetic Beta Entirely Other")):
        try:
            ids.append(await _resolve(pool, client_id, name, "-"))
        except InvoiceCompanyError:
            ids.append(await _resolve(pool, client_id, name, None))

    assert ids[0] != ids[1]
    assert await pool.fetchval("SELECT count(*) FROM companies WHERE npwp_company IS NOT NULL") == 0
    assert await pool.fetch(
        "SELECT client_id, company_id FROM client_company_links ORDER BY client_id"
    ) == [(1, ids[0]), (2, ids[1])]


@pytest.mark.asyncio
async def test_a_placeholder_already_stored_on_a_company_matches_no_other_name(
    pool: asyncpg.Pool,
) -> None:
    # A row written before this rule: it keeps its value and stops acting as an identity.
    legacy = await pool.fetchval(
        "INSERT INTO companies (company_name, npwp_company) VALUES ('PT Synthetic Lama', '-')"
        " RETURNING id"
    )

    with pytest.raises(InvoiceCompanyError) as refused:
        await _resolve(pool, 2, "PT Synthetic Beta Entirely Other", "-")

    assert refused.value.status_code == 422
    assert await _written(pool) == (1, 0)
    # Its own name still finds it, with the field left empty.
    assert await _resolve(pool, 1, "pt synthetic lama", None) == legacy
    assert await pool.fetchval("SELECT npwp_company FROM companies WHERE id = $1", legacy) == "-"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "typed"),
    [
        ("01.234.567.8-901.000", "012345678901000"),
        ("012345678901000", "01.234.567.8-901.000"),
        ("0123456789012345", "0123 4567 8901 2345"),
    ],
    ids=["15 digits, typed bare", "15 digits, typed with dots", "16 digits"],
)
async def test_a_real_tax_number_still_finds_its_company_under_another_name(
    pool: asyncpg.Pool, stored: str, typed: str
) -> None:
    company = await pool.fetchval(
        "INSERT INTO companies (company_name, npwp_company) VALUES ('PT Synthetic Lama', $1)"
        " RETURNING id",
        stored,
    )

    assert await _resolve(pool, 2, "Entirely Other Name", typed) == company
    assert await _written(pool) == (1, 1)


@pytest.mark.asyncio
async def test_a_real_tax_number_is_stored_on_a_new_company_as_typed(pool: asyncpg.Pool) -> None:
    company = await _resolve(pool, 1, "PT Synthetic Baru", " 01.234.567.8-901.000 ")

    assert (
        await pool.fetchval("SELECT npwp_company FROM companies WHERE id = $1", company)
        == "01.234.567.8-901.000"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [None, "", "   "])
async def test_an_empty_field_is_no_tax_number_and_no_error(
    pool: asyncpg.Pool, typed: str | None
) -> None:
    company = await _resolve(pool, 1, "PT Synthetic Polos", typed)

    assert await pool.fetchval("SELECT npwp_company FROM companies WHERE id = $1", company) is None
