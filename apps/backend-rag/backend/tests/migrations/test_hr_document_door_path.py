"""The HR document door (STAFF_CARD.md C12) DB step against a real Postgres: the card write and the
audit row share one transaction, and a failure leaves neither. Synthetic data only."""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest

from backend.app.services.hr.hr_documents import HRDocCode, HRDocError, HRDocumentService
from backend.tests.migrations.test_hr_card_write_path import pool  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

A_ID, A_MAIL = "tm-a", "fake.a@balizero.com"
_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS security_audit_log (
    id BIGSERIAL PRIMARY KEY, user_id VARCHAR(255), user_email VARCHAR(255),
    action VARCHAR(100) NOT NULL, resource_type VARCHAR(100), resource_id VARCHAR(255),
    ip_address INET, user_agent TEXT, details JSONB, success BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())
"""


async def _prep(pool: asyncpg.Pool) -> HRDocumentService:  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute(_AUDIT_DDL)
    return HRDocumentService(pool, drive=None)


async def _step(svc: HRDocumentService, slot: str, new_id: str | None, who=(A_ID, A_MAIL)):
    return await svc._db_step(
        actor_id="tm-owner", member_id=who[0], email=who[1], slot=slot, new_id=new_id
    )


async def _profile(pool: asyncpg.Pool, member: str = A_ID) -> Any:  # noqa: F811
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT profile::text FROM hr_employees WHERE team_member_id = $1", member
        )
    return json.loads(raw)


async def _audit(pool: asyncpg.Pool) -> list[asyncpg.Record]:  # noqa: F811
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM security_audit_log ORDER BY id")


@pytest.mark.asyncio
async def test_upload_writes_card_and_audit_together(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    assert await _step(svc, "ktp", "f1") is None
    assert await _profile(pool) == {"docs": {"ktp": "f1"}}
    rows = await _audit(pool)
    assert len(rows) == 1 and rows[0]["action"] == "hr_document_upload"
    assert rows[0]["resource_id"] == A_ID and rows[0]["user_id"] == "tm-owner"
    assert rows[0]["details"] == {"slot": "ktp", "new_id": "f1", "previous_id": None}


@pytest.mark.asyncio
async def test_replace_returns_previous_and_keeps_the_other_slots(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    await _step(svc, "ktp", "f1")
    await _step(svc, "kk", "f2")
    assert await _step(svc, "ktp", "f3") == "f1"
    assert (await _profile(pool))["docs"] == {"ktp": "f3", "kk": "f2"}
    assert (await _audit(pool))[-1]["details"]["previous_id"] == "f1"


@pytest.mark.asyncio
async def test_contract_slot_uses_contract_file_id_and_keeps_the_number(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = 'tm-a'",
            {"contract": {"number": "C-1"}, "unit": "Tax"},
        )
    await _step(svc, "contract", "c1")
    assert (await _profile(pool))["contract"] == {"number": "C-1", "file_id": "c1"}
    assert await _step(svc, "contract", None) == "c1"
    assert await _profile(pool) == {"contract": {"number": "C-1"}, "unit": "Tax"}


@pytest.mark.asyncio
async def test_remove_clears_the_slot_and_audits(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    await _step(svc, "npwp", "f1")
    await _step(svc, "kk", "f2")
    assert await _step(svc, "npwp", None) == "f1"
    assert (await _profile(pool))["docs"] == {"kk": "f2"}
    last = (await _audit(pool))[-1]
    assert last["action"] == "hr_document_remove"
    assert last["details"] == {"slot": "npwp", "new_id": None, "previous_id": "f1"}


@pytest.mark.asyncio
async def test_removing_an_empty_slot_changes_and_audits_nothing(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    assert await _step(svc, "kk", None) is None
    assert await _profile(pool) == {} and await _audit(pool) == []


@pytest.mark.asyncio
async def test_failed_audit_rolls_the_card_back(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    async with pool.acquire() as conn:  # make the audit insert fail
        await conn.execute("ALTER TABLE security_audit_log ADD CONSTRAINT nope CHECK (false)")
    with pytest.raises(HRDocError) as exc:
        await _step(svc, "ktp", "f1")
    assert exc.value.code is HRDocCode.DB_ERROR
    assert await _profile(pool) == {}  # neither the id...
    assert await _audit(pool) == []  # ...nor the row


@pytest.mark.asyncio
async def test_no_hr_row_and_invisible_person_write_nothing(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    with pytest.raises(HRDocError) as exc:  # tm-b is staff but has no hr_employees row
        await _step(svc, "ktp", "f1", who=("tm-b", "fake.b@balizero.com"))
    assert exc.value.code is HRDocCode.HR_RECORD_REQUIRED
    with pytest.raises(HRDocError) as exc2:  # tm-c is a client
        await _step(svc, "ktp", "f1", who=("tm-c", "fake.c@balizero.com"))
    assert exc2.value.code is HRDocCode.NOT_FOUND
    with pytest.raises(HRDocError) as exc3:  # stale email
        await _step(svc, "ktp", "f1", who=(A_ID, "other@balizero.com"))
    assert exc3.value.code is HRDocCode.NOT_FOUND
    assert await _audit(pool) == []
