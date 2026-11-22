"""LKPM statements that only a real database can judge: receipt listings and the auto-created configuration."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.compliance.lkpm_service import LKPMService

_DB_URL = os.environ.get("TEST_DATABASE_URL")


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

_TABLES = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    company_name TEXT NOT NULL,
    npwp_company TEXT,
    nib TEXT,
    kbli_code TEXT
);
CREATE TABLE client_company_links (
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    is_primary BOOLEAN
);
CREATE TABLE lkpm_client_config (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL,
    company_id INTEGER,
    company_name TEXT NOT NULL,
    npwp TEXT,
    nib TEXT,
    kbli_codes TEXT[] NOT NULL DEFAULT '{}',
    CONSTRAINT uq_lkpm_client UNIQUE (client_id, company_id)
);
CREATE TABLE lkpm_reports (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL,
    company_id INTEGER,
    quarter TEXT NOT NULL,
    year INTEGER NOT NULL
);
CREATE TABLE lkpm_receipts (
    id SERIAL PRIMARY KEY,
    lkpm_report_id INTEGER NOT NULL,
    nomor_laporan TEXT,
    nomor_kegiatan_usaha TEXT,
    kbli_code TEXT,
    kegiatan_usaha_desc TEXT,
    stage TEXT,
    oss_status TEXT,
    lokasi TEXT,
    tanggal_diterima DATE,
    nama_perusahaan_oss TEXT,
    file_drive_id TEXT,
    file_drive_url TEXT,
    file_name TEXT
);
"""

CLIENT = 900001
COMPANY_ONE = 900101
COMPANY_TWO = 900102
LISTINGS = ("get_receipts_for_portal_client", "get_receipts_for_client")


@pytest_asyncio.fixture()
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    schema = f"lkpm_pg_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DB_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            db_pool = await asyncpg.create_pool(
                _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
            )
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute(_TABLES)
                yield db_pool
            finally:
                await db_pool.close()
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await admin.close()


async def _company(pool: asyncpg.Pool, company_id: int, name: str, kbli: str | None = None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO companies (id, company_name, kbli_code) VALUES ($1, $2, $3)",
            company_id,
            name,
            kbli,
        )


async def _link(pool: asyncpg.Pool, client_id: int, company_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO client_company_links (client_id, company_id, status, is_primary) "
            "VALUES ($1, $2, 'active', TRUE)",
            client_id,
            company_id,
        )


async def _config(pool: asyncpg.Pool, client_id: int, company_id: int | None, name: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO lkpm_client_config (client_id, company_id, company_name) "
            "VALUES ($1, $2, $3)",
            client_id,
            company_id,
            name,
        )


async def _receipt(pool: asyncpg.Pool, report_client_id: int, company_id: int, number: str) -> None:
    async with pool.acquire() as conn:
        report_id = await conn.fetchval(
            "INSERT INTO lkpm_reports (client_id, company_id, quarter, year) "
            "VALUES ($1, $2, 'Q1', 2026) RETURNING id",
            report_client_id,
            company_id,
        )
        await conn.execute(
            "INSERT INTO lkpm_receipts (lkpm_report_id, nomor_laporan) VALUES ($1, $2)",
            report_id,
            number,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("listing", LISTINGS)
async def test_a_client_with_two_configurations_sees_each_receipt_once(
    pool: asyncpg.Pool, listing: str
) -> None:
    await _company(pool, COMPANY_ONE, "PT Contoh Satu")
    await _company(pool, COMPANY_TWO, "PT Contoh Dua")
    await _link(pool, CLIENT, COMPANY_ONE)
    await _link(pool, CLIENT, COMPANY_TWO)
    await _config(pool, CLIENT, COMPANY_ONE, "PT Contoh Satu")
    await _config(pool, CLIENT, COMPANY_TWO, "PT Contoh Dua")
    await _receipt(pool, CLIENT, COMPANY_ONE, "LAP-900001")

    rows = await getattr(LKPMService(pool), listing)(CLIENT)

    assert [(row["nomor_laporan"], row["company_name"]) for row in rows] == [
        ("LAP-900001", "PT Contoh Satu")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("listing", LISTINGS)
async def test_a_receipt_is_never_named_by_the_configuration_of_another_company(
    pool: asyncpg.Pool, listing: str
) -> None:
    # An imported report carries a company id in its client column. A different
    # client whose id is that number has a configuration for a company of its own.
    other_client = COMPANY_ONE
    await _company(pool, COMPANY_ONE, "PT Contoh Satu")
    await _company(pool, 900777, "PT Lain Tiga")
    await _link(pool, CLIENT, COMPANY_ONE)
    await _config(pool, other_client, 900777, "PT Lain Tiga")
    await _receipt(pool, other_client, COMPANY_ONE, "LAP-900002")

    rows = await getattr(LKPMService(pool), listing)(CLIENT)

    assert [(row["nomor_laporan"], row["company_name"]) for row in rows] == [
        ("LAP-900002", "PT Contoh Satu")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("listing", LISTINGS)
async def test_receipts_of_two_companies_keep_their_own_configured_names(
    pool: asyncpg.Pool, listing: str
) -> None:
    await _company(pool, COMPANY_ONE, "PT Contoh Satu")
    await _company(pool, COMPANY_TWO, "PT Contoh Dua")
    await _link(pool, CLIENT, COMPANY_ONE)
    await _link(pool, CLIENT, COMPANY_TWO)
    await _config(pool, CLIENT, COMPANY_ONE, "Contoh Satu as configured")
    await _receipt(pool, CLIENT, COMPANY_ONE, "LAP-900003")
    await _receipt(pool, CLIENT, COMPANY_TWO, "LAP-900004")

    rows = await getattr(LKPMService(pool), listing)(CLIENT)

    assert sorted((row["nomor_laporan"], row["company_name"]) for row in rows) == [
        ("LAP-900003", "Contoh Satu as configured"),
        ("LAP-900004", "PT Contoh Dua"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kbli_code", "stored"),
    [(None, []), ("   ", []), ("62011", ["62011"])],
)
async def test_the_auto_created_configuration_stores_only_a_real_activity_code(
    pool: asyncpg.Pool, kbli_code: str | None, stored: list[str]
) -> None:
    await _company(pool, COMPANY_ONE, "PT Contoh Satu", kbli_code)
    await _link(pool, CLIENT, COMPANY_ONE)

    await LKPMService(pool)._ensure_client_config(CLIENT)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT company_id, kbli_codes FROM lkpm_client_config WHERE client_id = $1", CLIENT
        )
    assert (row["company_id"], list(row["kbli_codes"])) == (COMPANY_ONE, stored)
