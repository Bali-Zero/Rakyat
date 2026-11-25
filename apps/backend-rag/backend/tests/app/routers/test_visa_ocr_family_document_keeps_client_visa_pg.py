"""Visa OCR of a family member's document never rewrites the main client's visa.

`_auto_ocr_visa` stores the OCR on the document and syncs the client's own visa
fields (`current_visa_type`, `visa_expiry_date`, `current_visa_sponsor`). A
document with `family_member_id` is a family member's permit: its OCR stays on
the document and the client row is left as it was. Runs the real statements on
real tables (invented rows, no client data); the OCR call itself is faked.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers.crm_enhanced import _auto_ocr_visa

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

_CLIENT = 900101
_OWN_DOC = 1
_FAMILY_DOC = 2

# The columns the real tables have, as far as `_auto_ocr_visa` writes them.
_TABLES = f"""
CREATE TABLE clients (
    id integer PRIMARY KEY, phone text, current_visa_type varchar(50),
    visa_expiry_date date, current_visa_sponsor varchar(255),
    updated_at timestamptz DEFAULT now()
);
CREATE TABLE documents (
    id integer PRIMARY KEY, client_id integer, family_member_id integer,
    document_type text, ocr_status text, ocr_completed_at timestamptz,
    ocr_extracted_data jsonb, expiry_date date, issue_date date,
    updated_at timestamptz DEFAULT now()
);
INSERT INTO clients (id, phone, current_visa_type, visa_expiry_date, current_visa_sponsor)
VALUES ({_CLIENT}, '+6289000000001', 'E33G', '2027-01-15', 'PT Synthetic Own Sponsor');
INSERT INTO documents (id, client_id, family_member_id, document_type)
VALUES ({_OWN_DOC}, {_CLIENT}, NULL, 'visa'), ({_FAMILY_DOC}, {_CLIENT}, 7, 'visa');
"""

_FAMILY_PERMIT_OCR = {
    "visa_type": "ITK",
    "visa_number": "SYN-0001",
    "expiry_date": "2027-12-31",
    "issue_date": "2026-11-01",
    "sponsor": "PT Synthetic Family Sponsor",
    "confidence": 0.9,
}


@pytest_asyncio.fixture
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    admin = await asyncpg.connect(_DB_URL)
    schema = "visa_ocr_family_" + uuid4().hex
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


async def _run_ocr(pool: asyncpg.Pool, doc_id: int | None) -> dict[str, Any]:
    with (
        patch(
            "backend.app.routers.crm_enhanced._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch("backend.app.routers.crm_enhanced._gemini_ocr", new=AsyncMock(return_value="{}")),
        patch(
            "backend.app.routers.crm_enhanced.extract_json_from_llm_response",
            return_value=dict(_FAMILY_PERMIT_OCR),
        ),
    ):
        return await _auto_ocr_visa(pool, _CLIENT, "synthetic-file", doc_id)


async def _client_visa(pool: asyncpg.Pool) -> tuple[Any, ...]:
    row = await pool.fetchrow(
        "SELECT current_visa_type, visa_expiry_date, current_visa_sponsor FROM clients WHERE id = $1",
        _CLIENT,
    )
    return tuple(row)


@pytest.mark.asyncio
async def test_family_member_document_leaves_the_client_visa_unchanged(
    pool: asyncpg.Pool,
) -> None:
    before = await _client_visa(pool)

    result = await _run_ocr(pool, _FAMILY_DOC)

    assert result["success"] is True
    assert await _client_visa(pool) == before
    # The OCR is still kept on the family member's document.
    doc = await pool.fetchrow(
        "SELECT ocr_status, expiry_date, issue_date FROM documents WHERE id = $1", _FAMILY_DOC
    )
    assert doc["ocr_status"] == "completed"
    assert doc["expiry_date"] == date(2027, 12, 31)
    assert doc["issue_date"] == date(2026, 11, 1)


@pytest.mark.asyncio
async def test_the_client_own_document_still_syncs_the_client_visa(pool: asyncpg.Pool) -> None:
    result = await _run_ocr(pool, _OWN_DOC)

    assert result["success"] is True
    assert await _client_visa(pool) == (
        "ITK",
        date(2027, 12, 31),
        "PT Synthetic Family Sponsor",
    )


@pytest.mark.asyncio
async def test_ocr_without_a_document_still_syncs_the_client_visa(pool: asyncpg.Pool) -> None:
    result = await _run_ocr(pool, None)

    assert result["success"] is True
    visa_type, expiry, _ = await _client_visa(pool)
    assert (visa_type, expiry) == ("ITK", date(2027, 12, 31))
