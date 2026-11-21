"""Invoice-company search on real PostgreSQL (synthetic data only).

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.app.routers import crm_practices
from backend.services.invoicing import invoice_company

pytestmark = pytest.mark.integration

_SCHEMA_SQL = """
CREATE TABLE clients (
    id SERIAL PRIMARY KEY, full_name VARCHAR NOT NULL, assigned_to VARCHAR
);
CREATE TABLE practices (
    id SERIAL PRIMARY KEY, client_id INTEGER, created_by VARCHAR, assigned_to VARCHAR
);
CREATE TABLE companies (
    id SERIAL PRIMARY KEY, company_name VARCHAR NOT NULL,
    company_type VARCHAR NOT NULL DEFAULT 'PT PMA', npwp_company VARCHAR UNIQUE,
    created_by VARCHAR, updated_by VARCHAR
);
CREATE TABLE client_company_links (
    id SERIAL PRIMARY KEY, client_id INTEGER NOT NULL, company_id INTEGER NOT NULL,
    role VARCHAR NOT NULL DEFAULT 'shareholder', is_primary BOOLEAN DEFAULT false,
    status VARCHAR DEFAULT 'active', end_date DATE, UNIQUE (client_id, company_id)
);
"""
ADMIN = {"email": "synthetic.admin@example.invalid", "role": "admin"}
STAFF = {"email": "synthetic.staff@example.invalid", "role": "staff"}


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    schema = "invoice_search_" + uuid4().hex
    created = None
    try:
        identity = await admin.fetchrow(
            "SELECT current_database() AS db, system_identifier::text AS system_id "
            "FROM pg_control_system()"
        )
        if dict(identity) != {"db": "universal_db_test", "system_id": expected}:
            pytest.fail("Refusing a database outside this disposable test cluster")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = await asyncpg.create_pool(
            dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
        )
        await created.execute(_SCHEMA_SQL)
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def person(pool: asyncpg.Pool, assigned_to: str | None = None) -> int:
    return await pool.fetchval(
        "INSERT INTO clients (full_name, assigned_to) VALUES ('Synthetic Person', $1) RETURNING id",
        assigned_to,
    )


async def company(pool: asyncpg.Pool, name: str, npwp: str | None = None) -> int:
    return await pool.fetchval(
        "INSERT INTO companies (company_name, npwp_company) VALUES ($1, $2) RETURNING id",
        name,
        npwp,
    )


async def search(pool: asyncpg.Pool, client_id: int, q: str, user: dict = ADMIN) -> list[dict]:
    result = await crm_practices.invoice_company_search(
        request=None, client_id=client_id, q=q, db_pool=pool, current_user=user
    )
    return result["companies"]


def names(rows: list[dict]) -> list[str]:
    return [r["company_name"] for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed",
    ["bayu", "BAYU", "Bayu", "PT.BAYU", "pt bayu", "PT. Bayu", "cv bayu", " bayu "],
)
async def test_casing_prefix_and_punctuation_find_the_same_company(
    pool: asyncpg.Pool, typed: str
) -> None:
    client = await person(pool)
    target = await company(pool, "PT BAYU BALI NOL")
    await company(pool, "PT Synthetic Lain")
    rows = await search(pool, client, typed)
    assert [r["id"] for r in rows] == [target]


@pytest.mark.asyncio
async def test_every_word_must_appear_in_any_order(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    await company(pool, "PT BAYU BALI NOL")
    await company(pool, "PT Bayu Santero")
    assert names(await search(pool, client, "bali bayu")) == ["PT BAYU BALI NOL"]
    assert names(await search(pool, client, "nol bayu bali")) == ["PT BAYU BALI NOL"]
    assert await search(pool, client, "bayu jakarta") == []


@pytest.mark.asyncio
async def test_trailing_tbk_is_ignored_on_both_sides(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    await company(pool, "PT Synthetic Maju, Tbk.")
    assert names(await search(pool, client, "synthetic maju tbk")) == ["PT Synthetic Maju, Tbk."]
    assert names(await search(pool, client, "PT SYNTHETIC MAJU")) == ["PT Synthetic Maju, Tbk."]


@pytest.mark.asyncio
async def test_npwp_digits_match_in_any_formatting(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    target = await company(pool, "PT Synthetic Pajak", "01.234.567.8-901.000")
    await company(pool, "PT Synthetic Lain", "99.999.999.9-999.000")
    for typed in ("012345678901000", "01.234.567.8-901", "0123456789", "567.8"):
        rows = await search(pool, client, typed)
        assert [r["id"] for r in rows] == [target], typed
    assert await search(pool, client, "5555") == []
    assert await search(pool, client, "123") == []


@pytest.mark.asyncio
async def test_short_or_empty_or_legal_only_queries_find_nothing(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    await company(pool, "PT Bayu Bali Nol")
    for typed in ("", " ", "b", "pt", "PT.", "cv tbk"):
        assert await search(pool, client, typed) == [], typed


@pytest.mark.asyncio
async def test_no_match_returns_an_empty_list(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    await company(pool, "PT Bayu Bali Nol")
    assert await search(pool, client, "zzzz") == []


@pytest.mark.asyncio
async def test_ranking_is_linked_then_exact_then_prefix_then_contains(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    await company(pool, "PT Zeta Bayu Extra")
    prefix = await company(pool, "PT Bayu Prefix")
    exact = await company(pool, "PT. BAYU")
    linked_contains = await company(pool, "PT Yyy Bayu Linked")
    ended = await company(pool, "PT Xxx Bayu Ended")
    await pool.execute(
        "INSERT INTO client_company_links (client_id, company_id) VALUES ($1, $2)",
        client,
        linked_contains,
    )
    await pool.execute(
        "INSERT INTO client_company_links (client_id, company_id, end_date)"
        " VALUES ($1, $2, '2020-01-01')",
        client,
        ended,
    )
    rows = await search(pool, client, "bayu")
    assert [r["id"] for r in rows][:3] == [linked_contains, exact, prefix]
    assert [r["linked"] for r in rows] == [True, False, False, False, False]
    assert rows[3]["company_name"] in {"PT Zeta Bayu Extra", "PT Xxx Bayu Ended"}


@pytest.mark.asyncio
async def test_at_most_eight_results(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    for i in range(12):
        await company(pool, f"PT Synthetic Bayu {i:02d}")
    assert len(await search(pool, client, "bayu")) == 8


@pytest.mark.asyncio
async def test_search_and_save_never_disagree_on_what_is_the_same_company(
    pool: asyncpg.Pool,
) -> None:
    client = await person(pool)
    target = await company(pool, "PT Synthetic Maju, Tbk.")
    assert [r["id"] for r in await search(pool, client, "synthetic maju")] == [target]
    async with pool.acquire() as conn:
        async with conn.transaction():
            resolved = await invoice_company.resolve_invoice_company(
                conn,
                client,
                company_id=None,
                name="CV synthetic MAJU",
                npwp=None,
                actor="synthetic",
            )
    assert resolved == target
    assert await pool.fetchval("SELECT count(*) FROM companies") == 1


@pytest.mark.asyncio
async def test_search_needs_the_same_client_visibility_as_the_suggestion(
    pool: asyncpg.Pool,
) -> None:
    mine = await person(pool, assigned_to=STAFF["email"])
    theirs = await person(pool, assigned_to="synthetic.someone@example.invalid")
    await company(pool, "PT Bayu Bali Nol")
    assert names(await search(pool, mine, "bayu", STAFF)) == ["PT Bayu Bali Nol"]
    with pytest.raises(HTTPException) as err:
        await search(pool, theirs, "bayu", STAFF)
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_a_legal_word_that_is_part_of_the_name_is_kept_like_the_save_path_keeps_it(
    pool: asyncpg.Pool,
) -> None:
    client = await person(pool)
    target = await company(pool, "PT Bayu CV")
    await company(pool, "PT Bayu Santero")
    assert [r["id"] for r in await search(pool, client, "Bayu CV")] == [target]
    assert {r["company_name"] for r in await search(pool, client, "bayu")} == {
        "PT Bayu CV",
        "PT Bayu Santero",
    }


@pytest.mark.asyncio
async def test_unicode_casefold_matches_in_search_and_save(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    target = await company(pool, "PT Straße Mulia")
    assert [r["id"] for r in await search(pool, client, "STRASSE")] == [target]
    async with pool.acquire() as conn:
        async with conn.transaction():
            resolved = await invoice_company.resolve_invoice_company(
                conn, client, company_id=None, name="strasse mulia", npwp=None, actor="synthetic"
            )
    assert resolved == target
    assert await pool.fetchval("SELECT count(*) FROM companies") == 1


@pytest.mark.asyncio
async def test_trailing_tbk_with_punctuation_never_duplicates_on_save(pool: asyncpg.Pool) -> None:
    client = await person(pool)
    target = await company(pool, "PT Maju Jaya Tbk,")
    assert [r["id"] for r in await search(pool, client, "maju jaya")] == [target]
    async with pool.acquire() as conn:
        async with conn.transaction():
            resolved = await invoice_company.resolve_invoice_company(
                conn, client, company_id=None, name="Maju Jaya", npwp=None, actor="synthetic"
            )
    assert resolved == target
    assert await pool.fetchval("SELECT count(*) FROM companies") == 1


_FORMS = [
    "PT. Bayu",
    "PT: Bayu",
    "PT-Bayu",
    "(PT) Bayu",
    "Bayu Tbk:",
    "Bayu - Tbk",
    "Bayu (Tbk).",
    "Bayu Tbk,",
]


async def save_name(pool: asyncpg.Pool, client: int, name: str) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await invoice_company.resolve_invoice_company(
                conn, client, company_id=None, name=name, npwp=None, actor="synthetic"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", _FORMS)
async def test_stored_affix_form_is_found_and_reused_by_a_plain_name(
    pool: asyncpg.Pool, stored: str
) -> None:
    client = await person(pool)
    target = await company(pool, stored)
    assert [r["id"] for r in await search(pool, client, "bayu")] == [target]
    assert await save_name(pool, client, "Bayu") == target
    assert await pool.fetchval("SELECT count(*) FROM companies") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", _FORMS)
async def test_typed_affix_form_finds_and_reuses_a_plain_stored_name(
    pool: asyncpg.Pool, typed: str
) -> None:
    client = await person(pool)
    target = await company(pool, "PT Bayu")
    assert [r["id"] for r in await search(pool, client, typed)] == [target]
    assert await save_name(pool, client, typed) == target
    assert await pool.fetchval("SELECT count(*) FROM companies") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["Bayu Tbkx", "PTB Bayu", "Atbk Bayu"])
async def test_letters_that_only_look_like_an_affix_neither_match_nor_reuse(
    pool: asyncpg.Pool, typed: str
) -> None:
    client = await person(pool)
    existing = await company(pool, "PT Bayu")
    assert await search(pool, client, typed) == []
    assert await save_name(pool, client, typed) != existing
    assert await pool.fetchval("SELECT count(*) FROM companies") == 2
