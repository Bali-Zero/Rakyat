"""NPWP OCR writes only where the NPWP belongs (DAI-208).

`_auto_ocr_npwp` stores the OCR on the document, then writes the NPWP to the
client's linked company (`companies.npwp_company`) or, with no company, to the
client's `custom_fields`. Three rules hold on real tables:

- a family member's document (`family_member_id` set) keeps its OCR on the
  document only: neither the company nor the client changes;
- a company NPWP is filled when empty and never replaced;
- an NPWP another company already holds (the column is unique) is skipped
  without an error.

Runs the real statements on real tables (invented rows, no client data); the
OCR call itself is faked.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers.crm_enhanced import _auto_ocr_npwp

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

OCR_NPWP = "00.000.000.0-000.000"
OTHER_NPWP = "00.000.000.0-000.001"

_WITH_COMPANY = 900201  # linked to company 1 (empty NPWP)
_WITH_FILLED_COMPANY = 900202  # linked to company 2 (NPWP already set)
_NO_COMPANY = 900203  # no company

_DOCS = {
    # (client, family_member_id)
    11: (_WITH_COMPANY, None),
    12: (_WITH_COMPANY, 7),
    21: (_WITH_FILLED_COMPANY, None),
    31: (_NO_COMPANY, None),
    32: (_NO_COMPANY, 8),
}

# The columns the real tables have, as far as `_auto_ocr_npwp` reads or writes them.
_TABLES = f"""
CREATE TABLE clients (
    id integer PRIMARY KEY, custom_fields jsonb,
    updated_at timestamptz DEFAULT now()
);
CREATE TABLE companies (
    id integer PRIMARY KEY, company_name text, npwp_company varchar(100) UNIQUE,
    updated_at timestamptz DEFAULT now()
);
CREATE TABLE client_company_links (client_id integer, company_id integer);
CREATE TABLE documents (
    id integer PRIMARY KEY, client_id integer, family_member_id integer,
    document_type text, ocr_status text, ocr_completed_at timestamptz,
    ocr_extracted_data jsonb, updated_at timestamptz DEFAULT now()
);
INSERT INTO clients (id, custom_fields) VALUES
    ({_WITH_COMPANY}, NULL),
    ({_WITH_FILLED_COMPANY}, NULL),
    ({_NO_COMPANY}, '{{"note": "synthetic"}}');
INSERT INTO companies (id, company_name, npwp_company) VALUES
    (1, 'PT Synthetic Empty', NULL),
    (2, 'PT Synthetic Filled', '{OTHER_NPWP}');
INSERT INTO client_company_links (client_id, company_id) VALUES
    ({_WITH_COMPANY}, 1), ({_WITH_FILLED_COMPANY}, 2);
INSERT INTO documents (id, client_id, family_member_id, document_type) VALUES
    {", ".join(f"({d}, {c}, {'NULL' if f is None else f}, 'npwp')" for d, (c, f) in _DOCS.items())};
"""


@pytest_asyncio.fixture
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    admin = await asyncpg.connect(_DB_URL)
    schema = "npwp_ocr_guard_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as c:
            await c.execute(_TABLES)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _run_ocr(
    pool: asyncpg.Pool, client_id: int, doc_id: int | None, npwp: str = OCR_NPWP
) -> dict[str, Any]:
    with (
        patch(
            "backend.app.routers.crm_enhanced._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch("backend.app.routers.crm_enhanced._gemini_ocr", new=AsyncMock(return_value="{}")),
        patch(
            "backend.app.routers.crm_enhanced.extract_json_from_llm_response",
            return_value={"npwp_number": npwp, "kpp_name": "KPP Synthetic", "confidence": 0.9},
        ),
    ):
        return await _auto_ocr_npwp(pool, client_id, "synthetic-file", doc_id)


async def _company_npwp(pool: asyncpg.Pool, company_id: int) -> str | None:
    return await pool.fetchval("SELECT npwp_company FROM companies WHERE id = $1", company_id)


async def _custom_fields(pool: asyncpg.Pool, client_id: int) -> dict[str, Any] | None:
    raw = await pool.fetchval("SELECT custom_fields FROM clients WHERE id = $1", client_id)
    return json.loads(raw) if raw else None


async def _doc_ocr_status(pool: asyncpg.Pool, doc_id: int) -> str | None:
    return await pool.fetchval("SELECT ocr_status FROM documents WHERE id = $1", doc_id)


@pytest.mark.asyncio
async def test_family_document_leaves_the_client_company_npwp_empty(pool: asyncpg.Pool) -> None:
    result = await _run_ocr(pool, _WITH_COMPANY, 12)

    assert result["success"] is True
    assert await _company_npwp(pool, 1) is None
    assert await _doc_ocr_status(pool, 12) == "completed"


@pytest.mark.asyncio
async def test_family_document_leaves_the_client_custom_fields_alone(pool: asyncpg.Pool) -> None:
    result = await _run_ocr(pool, _NO_COMPANY, 32)

    assert result["success"] is True
    assert await _custom_fields(pool, _NO_COMPANY) == {"note": "synthetic"}
    assert await _doc_ocr_status(pool, 32) == "completed"


@pytest.mark.asyncio
async def test_a_company_npwp_already_set_is_never_replaced(pool: asyncpg.Pool) -> None:
    result = await _run_ocr(pool, _WITH_FILLED_COMPANY, 21)

    assert result["success"] is True
    assert await _company_npwp(pool, 2) == OTHER_NPWP


@pytest.mark.asyncio
async def test_an_empty_company_npwp_is_filled_from_the_client_own_document(
    pool: asyncpg.Pool,
) -> None:
    result = await _run_ocr(pool, _WITH_COMPANY, 11)

    assert result["success"] is True
    assert await _company_npwp(pool, 1) == OCR_NPWP


@pytest.mark.asyncio
async def test_a_client_without_company_gets_the_npwp_in_custom_fields(
    pool: asyncpg.Pool,
) -> None:
    result = await _run_ocr(pool, _NO_COMPANY, 31)

    assert result["success"] is True
    assert await _custom_fields(pool, _NO_COMPANY) == {
        "note": "synthetic",
        "npwp": OCR_NPWP,
        "kpp": "KPP Synthetic",
    }


@pytest.mark.asyncio
async def test_ocr_without_a_document_still_fills_an_empty_company_npwp(
    pool: asyncpg.Pool,
) -> None:
    result = await _run_ocr(pool, _WITH_COMPANY, None)

    assert result["success"] is True
    assert await _company_npwp(pool, 1) == OCR_NPWP


@pytest.mark.asyncio
async def test_an_npwp_held_by_another_company_is_skipped_without_an_error(
    pool: asyncpg.Pool,
) -> None:
    result = await _run_ocr(pool, _WITH_COMPANY, 11, npwp=OTHER_NPWP)

    assert result["success"] is True
    assert await _company_npwp(pool, 1) is None
    assert await _company_npwp(pool, 2) == OTHER_NPWP
    assert await _doc_ocr_status(pool, 11) == "completed"
