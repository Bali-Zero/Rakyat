"""Staff find a client in the /clients search (and Cmd+K) by typing a NIB or an NPWP.

``GET /api/crm/clients/?search=`` is the one query behind both the /clients search box
and the Cmd+K "Search clients for <q>" action. A NIB/NPWP lives in two places: on the
client row (``clients.nib`` / ``clients.npwp``) and, for a client created with a company,
on the linked company (``companies.nib`` / ``companies.npwp_company`` through
``client_company_links``) — the create path moves the NIB onto the company row.

These tests run the production query against a REAL Postgres, so they assert who is
found, not how the SQL is spelled. The scope cases prove the new search terms stay
inside the non-admin ownership filter (assigned_to = me OR tax_consultant = me).

All identifiers are fictitious placeholders. Skipped unless ``TEST_DATABASE_URL`` is
set and ``asyncpg`` is installed.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

import pytest

from backend.app.routers.crm_clients import list_clients

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    asyncpg is None or not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set or asyncpg missing",
)

ADMIN = {"email": "director@example.test", "role": "admin"}
STAFF = {"email": "staff@example.test", "role": "agent"}
OWNER = {"email": "owner@example.test", "role": "agent"}

CLIENT_NIB = "9120000000001"
CLIENT_NPWP = "00.000.000.0-000.000"
COMPANY_NIB = "9120000000002"
COMPANY_NPWP = "00.000.000.0-000.001"

# Only the columns the list query reads.
_SCHEMA = """
CREATE TABLE clients (
    id SERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    full_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    whatsapp TEXT,
    nationality TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    client_type TEXT NOT NULL DEFAULT 'individual',
    assigned_to TEXT,
    tax_consultant TEXT,
    avatar_url TEXT,
    first_contact_date TIMESTAMPTZ,
    last_interaction_date TIMESTAMPTZ,
    passport_number TEXT,
    passport_expiry DATE,
    date_of_birth DATE,
    gender TEXT,
    birthplace TEXT,
    company_name TEXT,
    custom_fields JSONB,
    address TEXT,
    notes TEXT,
    npwp TEXT,
    nib TEXT,
    tags TEXT[],
    ai_summary JSONB,
    ai_summary_generated_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE interactions (
    id SERIAL PRIMARY KEY,
    client_id INTEGER,
    sentiment TEXT,
    summary TEXT,
    interaction_date TIMESTAMPTZ
);
CREATE TABLE crm_guardian_summary_queue (
    id SERIAL PRIMARY KEY,
    client_id INTEGER,
    status TEXT,
    enqueued_at TIMESTAMPTZ
);
CREATE TABLE companies (
    id SERIAL PRIMARY KEY,
    company_name VARCHAR(255) NOT NULL,
    nib VARCHAR(100),
    npwp_company VARCHAR(100),
    status VARCHAR(50)
);
CREATE TABLE client_company_links (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    is_primary BOOLEAN DEFAULT FALSE,
    status VARCHAR(50) DEFAULT 'active'
);
"""


@pytest.fixture
async def pool():
    assert asyncpg is not None and TEST_DATABASE_URL is not None
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    db_name = f"crm_search_nib_test_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    await admin.close()

    base = TEST_DATABASE_URL.rsplit("/", 1)[0]
    test_pool = None
    try:
        test_pool = await asyncpg.create_pool(f"{base}/{db_name}", min_size=1, max_size=2)
        async with test_pool.acquire() as conn:
            await conn.execute(_SCHEMA)
            # Client 1: NIB/NPWP on the client row, assigned to OWNER.
            await conn.execute(
                "INSERT INTO clients (full_name, email, assigned_to, nib, npwp) "
                "VALUES ('Client One', 'client.one@example.test', $1, $2, $3)",
                OWNER["email"],
                CLIENT_NIB,
                CLIENT_NPWP,
            )
            # Client 2: NIB/NPWP only on the linked company, unassigned (admin-only).
            client_two = await conn.fetchval(
                "INSERT INTO clients (full_name, company_name) "
                "VALUES ('Client Two', 'PT Contoh Fiktif') RETURNING id"
            )
            company = await conn.fetchval(
                "INSERT INTO companies (company_name, nib, npwp_company, status) "
                "VALUES ('PT Contoh Fiktif', $1, $2, 'active') RETURNING id",
                COMPANY_NIB,
                COMPANY_NPWP,
            )
            await conn.execute(
                "INSERT INTO client_company_links (client_id, company_id, is_primary) "
                "VALUES ($1, $2, TRUE)",
                client_two,
                company,
            )
            # Client 3: no identifiers, the noise row.
            await conn.execute("INSERT INTO clients (full_name) VALUES ('Client Three')")
        yield test_pool
    finally:
        if test_pool is not None:
            await test_pool.close()
        admin = await asyncpg.connect(TEST_DATABASE_URL)
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()


async def _search(pool, user: dict[str, Any], q: str) -> list[str]:
    rows = await list_clients(
        status=None,
        assigned_to=None,
        search=q,
        nationality=None,
        unnamed=False,
        passport_expiring_days=None,
        limit=50,
        offset=0,
        db_pool=pool,
        current_user=user,
    )
    return sorted(r.full_name for r in rows)


async def test_nib_on_the_client_row_finds_the_client(pool) -> None:
    assert await _search(pool, ADMIN, CLIENT_NIB) == ["Client One"]


async def test_npwp_on_the_client_row_finds_the_client(pool) -> None:
    assert await _search(pool, ADMIN, CLIENT_NPWP) == ["Client One"]


async def test_nib_of_the_linked_company_finds_the_client(pool) -> None:
    assert await _search(pool, ADMIN, COMPANY_NIB) == ["Client Two"]


async def test_npwp_of_the_linked_company_finds_the_client(pool) -> None:
    assert await _search(pool, ADMIN, COMPANY_NPWP) == ["Client Two"]


async def test_innocence_unknown_text_finds_nobody(pool) -> None:
    assert await _search(pool, ADMIN, "zz-tidak-ada") == []


async def test_control_email_search_still_works(pool) -> None:
    assert await _search(pool, ADMIN, "client.one@example") == ["Client One"]


async def test_scope_unrelated_staff_finds_nothing_even_on_exact_nib(pool) -> None:
    assert await _search(pool, STAFF, CLIENT_NIB) == []
    assert await _search(pool, STAFF, COMPANY_NIB) == []
    assert await _search(pool, STAFF, CLIENT_NPWP) == []


async def test_scope_owner_finds_own_client_by_nib_but_not_an_unassigned_one(pool) -> None:
    assert await _search(pool, OWNER, CLIENT_NIB) == ["Client One"]
    assert await _search(pool, OWNER, COMPANY_NIB) == []
