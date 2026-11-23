"""The permit clock, archived proof and concurrent official send on a real CI database."""

import asyncio
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.app.services.internal_email import EmailDeliveryUncertain, EmailProviderRejected
from backend.services.crm import visa_followup as tracking

USER = {"email": "consultant@example.test", "role": "consultant"}
SCHEMA = """
CREATE TABLE practice_types(id int primary key, code text, category text, name text);
CREATE TABLE practices(id int primary key, practice_type_id int, client_id int, family_member_id int,
    assigned_to text, status text, payment_status text,client_visible boolean);
CREATE TABLE client_family_members(id int primary key,client_id int);
CREATE TABLE documents(id int primary key,practice_id int,client_id int,family_member_id int,
    status text,is_archived boolean,deleted_at timestamptz,file_id text);
CREATE TABLE practice_delivery_slots(practice_id int,service_code text,slot_key text,
    document_id int REFERENCES documents(id) ON DELETE CASCADE,PRIMARY KEY(practice_id,slot_key));
CREATE TABLE hr_delivery_bonus_events(practice_id int,service_code text,event_kind text,recorded_at timestamptz);
INSERT INTO practice_types VALUES (1,'visa_c1_tourism','single_entry_visa','C1');
INSERT INTO practices VALUES (7,1,31,null,'consultant@example.test','on_process','paid',true);
INSERT INTO hr_delivery_bonus_events VALUES (7,'visa_c1_tourism','electronic_visa_issued',now()-interval '65 days');
INSERT INTO documents VALUES (11,7,31,null,'active',false,null,'permit-file');
INSERT INTO practice_delivery_slots VALUES (7,'visa_c1_tourism','document_2',11);
"""


@pytest_asyncio.fixture
async def pool():
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("NUZ_UNIVERSAL_PG_DSN", "")
    parsed = urlsplit(dsn)
    if (
        not dsn
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or "test" not in parsed.path
    ):
        pytest.skip("Local test PostgreSQL is provided in the backend CI shard")
    schema = "visa_followup_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f"CREATE SCHEMA {schema}")
    db = None
    try:
        db = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        async with db.acquire() as conn:
            await conn.execute(SCHEMA)
            migration = (
                Path(__file__).resolve().parents[2]
                / "db"
                / "migrations_v2"
                / "385_practice_visa_followups.sql"
            )
            await conn.execute(migration.read_text().split("-- === ROLLBACK ===")[0])
        yield db
    finally:
        if db is not None:
            await db.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


@pytest.mark.asyncio
async def test_existing_permit_recovered_and_deleting_or_archiving_reinstates_clock(pool):
    async with pool.acquire() as conn:
        first = await tracking.get_followup(conn, 7, user=USER)
        assert first["status"] == "fulfilled" and first["due_on"] == first[
            "started_on"
        ] + timedelta(days=30)
        await conn.execute("UPDATE documents SET is_archived=true WHERE id=11")
        assert (await tracking.get_followup(conn, 7))["status"] == "escalation_due"
        await conn.execute("UPDATE documents SET is_archived=false WHERE id=11")
        assert (await tracking.get_followup(conn, 7))["status"] == "fulfilled"
        await conn.execute("UPDATE documents SET client_id=32 WHERE id=11")
        assert (await tracking.get_followup(conn, 7))["fulfilled_at"] is None
        await conn.execute("UPDATE documents SET client_id=31 WHERE id=11")
        await conn.execute("DELETE FROM documents WHERE id=11")
        final = await tracking.get_followup(conn, 7)
        assert (
            final["started_on"] == first["started_on"]
            and final["can_escalate"]
            and final["fulfilled_at"] is None
        )


@pytest.mark.asyncio
async def test_concurrent_private_declaration_uses_one_fixed_extension(pool):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id=11")
        # Build a new 45-day fixture record. Production does not edit day zero.
        await conn.execute("DELETE FROM practice_visa_followups WHERE practice_id=7")
        await conn.execute("""INSERT INTO practice_visa_followups(practice_id,service_code,slot_key,started_on,due_on,allow_postpone)
             VALUES (7,'visa_c1_tourism','document_2',(now() AT TIME ZONE 'Asia/Makassar')::date-45,
                     (now() AT TIME ZONE 'Asia/Makassar')::date-15,true)""")
    results = await asyncio.gather(
        tracking.postpone(pool, 7, USER, "Client is still abroad"),
        tracking.postpone(pool, 7, USER, "The trip has moved"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in results) == 1
    outcome = next(r for r in results if isinstance(r, dict))
    assert (
        outcome["due_on"] == outcome["started_on"] + timedelta(days=60)
        and not outcome["can_postpone"]
    )


@pytest.mark.asyncio
async def test_concurrent_explicit_send_records_one_accepted_attempt(pool, monkeypatch):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id=11")
    entered, release = asyncio.Event(), asyncio.Event()

    async def accepted(**kwargs):
        assert kwargs["to"] == "zero@balizero.com" and kwargs["raise_on_failure"]
        entered.set()
        await release.wait()
        return True

    sender = AsyncMock(side_effect=accepted)
    monkeypatch.setattr(tracking, "send_internal_email", sender)
    task = asyncio.create_task(tracking.escalate(pool, 7, USER, "Travel remains on hold"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(HTTPException) as competing:
            await tracking.escalate(pool, 7, USER, "Travel remains on hold")
        assert competing.value.status_code == 409
    finally:
        release.set()
    first = await task
    second = await tracking.escalate(pool, 7, USER, "Travel remains on hold")
    assert (
        first["email_sent_at"] == second["email_sent_at"]
        and first["status"] == "email_sent_permit_due"
    )
    assert sender.await_count == 1
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM practice_visa_followup_send_attempts WHERE outcome='sent'"
            )
            == 1
        )


@pytest.mark.asyncio
async def test_known_rejection_retries_same_frozen_payload_and_key(pool, monkeypatch):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id=11")
    sender = AsyncMock(side_effect=[EmailProviderRejected("rejected"), True])
    monkeypatch.setattr(tracking, "send_internal_email", sender)
    with pytest.raises(HTTPException) as rejected:
        await tracking.escalate(pool, 7, USER, "Awaiting permit from immigration")
    assert rejected.value.status_code == 502
    with pytest.raises(HTTPException) as change:
        await tracking.escalate(pool, 7, USER, "Changed reason")
    assert change.value.status_code == 409
    result = await tracking.escalate(pool, 7, USER, "Awaiting permit from immigration")
    assert result["email_sent_at"] is not None and result["status"] == "email_sent_permit_due"
    assert sender.await_args_list[0].kwargs == sender.await_args_list[1].kwargs
    async with pool.acquire() as conn:
        outcomes = await conn.fetch(
            "SELECT outcome FROM practice_visa_followup_send_attempts ORDER BY attempt"
        )
        assert [r["outcome"] for r in outcomes] == ["failed", "sent"]


@pytest.mark.asyncio
async def test_first_upload_clock_uses_frozen_wita_receipt_when_replayed_on_another_day(
    pool, monkeypatch
):
    monkeypatch.setattr(
        tracking, "delivery_policy", lambda _: {"followup_slot": "document_2", "can_postpone": True}
    )
    monkeypatch.setattr(tracking, "business_today", lambda: date(2026, 3, 22))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM practice_visa_followups WHERE practice_id=7")
        await conn.execute(
            "UPDATE hr_delivery_bonus_events SET recorded_at='2026-01-28 16:01:00+00' WHERE practice_id=7"
        )
        await tracking.record_visa_milestone(
            conn, {"id": 7, "code": "visa_c1_tourism"}, "electronic_visa"
        )
        await tracking.record_visa_milestone(
            conn, {"id": 7, "code": "visa_c1_tourism"}, "electronic_visa"
        )
        view = await tracking.get_followup(conn, 7, user=USER)
    assert view["started_on"] == date(2026, 1, 29)
    assert view["due_on"] == date(2026, 2, 28) and view["final_due_on"] == date(2026, 3, 30)


@pytest.mark.asyncio
async def test_owner_attests_positive_receipt_without_resending_or_releasing_pay(pool, monkeypatch):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id=11")
    transport = AsyncMock(side_effect=EmailDeliveryUncertain("response lost"))
    monkeypatch.setattr(tracking, "send_internal_email", transport)
    with pytest.raises(HTTPException) as lost:
        await tracking.escalate(pool, 7, USER, "Client delayed arrival")
    assert lost.value.status_code == 502
    reference = "synthetic-brevo-message-7"
    with pytest.raises(HTTPException) as denied:
        await tracking.confirm_receipt(pool, 7, USER, reference)
    assert denied.value.status_code == 403
    owner = {"email": "zero@balizero.com", "role": "admin"}
    confirmed = await tracking.confirm_receipt(pool, 7, owner, reference)
    assert (
        confirmed["email_receipt_confirmed_at"] is not None
        and confirmed["status"] == "email_sent_permit_due"
    )
    assert await tracking.confirm_receipt(pool, 7, owner, reference) == confirmed
    assert (await tracking.escalate(pool, 7, USER, "Client delayed arrival"))[
        "email_state"
    ] == "sent"
    assert transport.await_count == 1
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT outcome FROM practice_visa_followup_send_attempts WHERE practice_id=7"
            )
            == "uncertain"
        )
        saved = await conn.fetchrow(
            "SELECT email_receipt_confirmed_by,email_receipt_reference,fulfilled_at FROM practice_visa_followups WHERE practice_id=7"
        )
        assert tuple(saved.values()) == (owner["email"], reference, None)
