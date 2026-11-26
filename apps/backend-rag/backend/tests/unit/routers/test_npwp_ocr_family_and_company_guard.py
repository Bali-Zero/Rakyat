"""The NPWP OCR writes carry their guards in their own statements (DAI-208).

Behaviour on real tables is in
`tests/app/routers/test_npwp_ocr_family_and_company_guard_pg.py`; this unit test
pins the statement shape where no database is available: both NPWP writes bind
the document id and skip a family member's document, the company write only
fills an empty NPWP, and an NPWP another company holds is skipped without an
error and without its value in the log. Synthetic values only.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

ROUTER = "backend.app.routers.crm_enhanced"
NPWP = "00.000.000.0-000.000"
_OCR = {"npwp_number": NPWP, "kpp_name": "KPP Synthetic", "confidence": 0.9}


def _pool(conn: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


async def _run(conn: MagicMock, doc_id: int | None = 5) -> dict[str, Any]:
    from backend.app.routers.crm_enhanced import _auto_ocr_npwp

    with (
        patch(f"{ROUTER}._download_drive_file", new=AsyncMock(return_value=(b"x", "image/jpeg"))),
        patch(f"{ROUTER}._gemini_ocr", new=AsyncMock(return_value="{}")),
        patch(f"{ROUTER}.extract_json_from_llm_response", return_value=dict(_OCR)),
    ):
        return await _auto_ocr_npwp(_pool(conn), 42, "synthetic-file", doc_id)


def _write(conn: MagicMock, table: str) -> tuple[str, tuple[Any, ...]]:
    calls = [c for c in conn.execute.call_args_list if f"UPDATE {table}" in c.args[0]]
    assert len(calls) == 1, calls
    return calls[0].args[0], calls[0].args[1:]


def _squash(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.asyncio
async def test_company_write_fills_only_an_empty_npwp_and_skips_family_documents() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 10})
    conn.execute = AsyncMock(return_value="UPDATE 1")

    result = await _run(conn, doc_id=5)

    assert result["success"] is True
    sql, args = _write(conn, "companies")
    assert "(npwp_company IS NULL OR npwp_company = '')" in _squash(sql)
    assert "WHERE id = $3 AND family_member_id IS NOT NULL" in _squash(sql)
    assert args == (NPWP, 10, 5)


@pytest.mark.asyncio
async def test_client_write_skips_family_documents() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")

    result = await _run(conn, doc_id=5)

    assert result["success"] is True
    sql, args = _write(conn, "clients")
    assert "WHERE id = $3 AND family_member_id IS NOT NULL" in _squash(sql)
    assert args[1:] == (42, 5)


@pytest.mark.asyncio
async def test_an_npwp_held_by_another_company_is_skipped_and_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def execute(sql: str, *args: Any) -> str:
        if "UPDATE companies" in sql:
            raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
        return "UPDATE 1"

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 10})
    conn.execute = AsyncMock(side_effect=execute)

    with caplog.at_level(logging.DEBUG, logger=ROUTER):
        result = await _run(conn, doc_id=5)

    assert result["success"] is True
    assert any("already used by another company" in r.getMessage() for r in caplog.records)
    assert all(NPWP not in r.getMessage() for r in caplog.records)
