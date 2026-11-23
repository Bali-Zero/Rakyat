"""Run the issued visa → post-entry permit → final delivery on a real test PostgreSQL.

Only the external file sink and post-commit cache invalidations are substituted.
CI exposes TEST_DATABASE_URL on every backend shard. Each test owns a disposable schema.
"""

import asyncio
import base64
import os
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException

from backend.app.routers.crm_practice_delivery import DeliveryFile
from backend.services.crm import practice_delivery as service

SQL_BASE = """
CREATE TABLE practice_types(id int primary key,code text,name text,category text);
CREATE TABLE practices(id int primary key,client_id int,family_member_id int,assigned_to text,
    client_visible boolean, status text, payment_status text,practice_type_id int,
    completion_date timestamptz,updated_at timestamptz);
CREATE TABLE client_family_members(id int primary key,client_id int);
CREATE TABLE documents(id serial primary key,practice_id int,client_id int,family_member_id int,
    intake_idempotency_key text UNIQUE,file_id text,file_name text,content_hash text,document_type text,uploaded_source text,status text,
    deleted_at timestamptz,is_archived boolean,client_visible boolean,updated_at timestamptz);
CREATE TABLE companies(id int primary key);
CREATE TABLE company_documents(id int primary key,company_id int,google_drive_file_id text,client_visible boolean);
CREATE TABLE client_company_links(company_id int,client_id int);
CREATE TABLE activity_log(entity_type text,entity_id int,action text,performed_by text,description text);
CREATE TABLE timeline_events(client_id int,practice_id int,event_type text,title text,description text,
    event_date timestamptz,client_visible boolean,color text,created_by text);
CREATE TABLE team_members(id int primary key,email text,role text,active boolean);
CREATE TABLE hr_employees(id int primary key,team_member_id int,is_active boolean,profile jsonb);
CREATE TABLE hr_bonus_rates(id int primary key,practice_type_code text,is_active boolean,
    effective_from date,effective_to date,amount_idr bigint);
CREATE TABLE hr_bonus_ledger(id bigserial primary key, practice_id int,employee_id int,practice_type_code text,
    bonus_rate_id int,amount_idr bigint,awarded_at timestamptz,status text);
CREATE TABLE hr_payroll_periods(payroll_month int,payroll_year int,status text);
"""
STAFF = {"email": "consultant@example.test", "role": "consultant"}


@pytest_asyncio.fixture
async def pool(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("NUZ_UNIVERSAL_PG_DSN", "")
    url = urlsplit(dsn)
    if not dsn or url.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in url.path:
        pytest.skip("Requires a local test database, provided by the backend CI shard")
    schema = "kita_delivery_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f"CREATE SCHEMA {schema}")
    db = None
    try:
        db = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        folder = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
        async with db.acquire() as conn:
            await conn.execute(SQL_BASE)
            for filename in ("383_practice_delivery.sql", "384_delivery_bonus.sql"):
                await conn.execute((folder / filename).read_text().split("-- === ROLLBACK ===")[0])
            await conn.execute("""
                CREATE TRIGGER hr_validate BEFORE INSERT ON hr_bonus_ledger
                    FOR EACH ROW EXECUTE FUNCTION hr_validate_completed_practice();
                INSERT INTO team_members VALUES(1,'consultant@example.test','consultant',true);
                INSERT INTO hr_employees VALUES(1,1,true,'{}'::jsonb);
                INSERT INTO practice_types VALUES(1,'visa_c1_tourism','C1','single_entry_visa'),
                    (2,'kitas_working_extend','Working KITAS renewal','kitas');
                INSERT INTO hr_bonus_rates VALUES(1,'visa_c1_tourism',true,'2025-01-01',null,100000);
                INSERT INTO practices VALUES(7,31,null,'consultant@example.test',true,'on_process','paid',1,null,now()),
                    (8,31,null,'consultant@example.test',true,'on_process','paid',2,null,now());
            """)

        async def sink(client_id, data, **kwargs):
            assert data.client_visible is False
            async with db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO documents(practice_id,client_id,family_member_id,
                    intake_idempotency_key,file_id,file_name,uploaded_source,status,client_visible,document_type)
                    VALUES($1,$2,$3,$4,$4,$5,'team','active',false,$6)
                    ON CONFLICT(intake_idempotency_key) DO NOTHING""",
                    data.practice_id,
                    client_id,
                    data.family_member_id,
                    data.intake_idempotency_key,
                    data.file_name,
                    data.document_type,
                )

        monkeypatch.setattr(service, "upload_document_base64", sink)
        monkeypatch.setattr(service, "invalidate_delivery_cache", AsyncMock())
        yield db
    finally:
        if db is not None:
            await db.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


def final_file(name):
    return DeliveryFile(
        file_name=f"{name}.pdf",
        issued_on=date(2026, 1, 28) if name == "electronic-visa" else None,
        mime_type="application/pdf",
        file_content=base64.b64encode(f"%PDF-1.7\n{name}".encode()).decode(),
    )


@pytest.mark.asyncio
async def test_concurrent_issue_one_bonus_and_post_entry_completion(pool):
    # The same issuance submitted twice can neither pay twice nor prematurely close.
    await asyncio.gather(
        *[
            service.upload(
                pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
            )
            for _ in range(2)
        ]
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.status,b.status AS bonus,e.event_kind,b.amount_idr FROM practices p JOIN hr_delivery_bonus_events e ON e.practice_id=p.id JOIN hr_bonus_ledger b ON b.id=e.ledger_id WHERE p.id=7"
        )
        assert tuple(row.values()) == ("on_process", "pending", "electronic_visa_issued", 100000)
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
        portal = await service.portal_files(conn, 7, 31)
        assert [r["file_name"] for r in portal["documents"]] == ["electronic-visa.pdf"]
        with pytest.raises(HTTPException):
            await service.portal_files(conn, 7, 32)
    with pytest.raises(HTTPException, match="Upload every required"):
        await service.complete(pool, 7, STAFF, BackgroundTasks())
    await service.upload(pool, 7, "document_2", STAFF, final_file("itk"), BackgroundTasks())
    async with pool.acquire() as conn:
        assert len((await service.portal_files(conn, 7, 31))["documents"]) == 1
    result = await service.complete(pool, 7, STAFF, BackgroundTasks())
    assert result["status"] == "completed"
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert len((await service.portal_files(conn, 7, 31))["documents"]) == 2
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
        assert await conn.fetchval("SELECT count(*) FROM timeline_events") == 1


@pytest.mark.asyncio
async def test_combined_renewal_earns_only_at_completion_and_keeps_rate_gap(pool):
    result = await service.upload(
        pool, 8, "document_1", STAFF, final_file("combined-permit"), BackgroundTasks()
    )
    assert not result["bonus_event"]
    doc_id = result["slots"][0]["document_id"]
    assert result["slots"][1]["reusable_document_id"] == doc_id
    with pytest.raises(HTTPException):
        await service.reuse(pool, 8, "document_2", STAFF, doc_id + 100)
    await service.reuse(pool, 8, "document_2", STAFF, doc_id)
    await service.waive(pool, 8, "document_3", STAFF, "No separate manpower filing in this order")
    result = await service.complete(pool, 8, STAFF, BackgroundTasks())
    assert result["bonus_event"] == "service_delivered"
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM documents") == 1
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_delivery_bonus_events WHERE amount_idr IS NULL"
            )
            == 1
        )
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 0


@pytest.mark.asyncio
async def test_replaced_archived_and_deleted_evisa_never_counts_as_itk(pool):
    visa = final_file("electronic-visa")
    original = await service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks())
    first_id = original["slots"][0]["document_id"]
    disguised = visa.model_copy(update={"issued_on": None, "file_name": "visit-stay-permit.pdf"})
    with pytest.raises(HTTPException, match="another output"):
        await service.upload(pool, 7, "document_2", STAFF, disguised, BackgroundTasks())
    # An old upload might have left all current slot pointers after a correction.
    await service.upload(
        pool,
        7,
        "electronic_visa",
        STAFF,
        final_file("corrected-electronic-visa"),
        BackgroundTasks(),
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE documents SET is_archived=true WHERE id=$1", first_id)
    with pytest.raises(HTTPException, match="another output"):
        await service.upload(pool, 7, "document_2", STAFF, disguised, BackgroundTasks())
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id=$1", first_id)
    with pytest.raises(HTTPException, match="another output"):
        await service.upload(pool, 7, "document_2", STAFF, disguised, BackgroundTasks())
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=7") == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM practice_delivery_slots WHERE practice_id=7")
            == 1
        )
    with pytest.raises(HTTPException, match="Upload every required"):
        await service.complete(pool, 7, STAFF, BackgroundTasks())


@pytest.mark.asyncio
async def test_concurrent_different_slots_cannot_claim_same_content(pool):
    visa = final_file("electronic-visa")
    results = await asyncio.gather(
        service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks()),
        service.upload(
            pool,
            7,
            "document_2",
            STAFF,
            visa.model_copy(update={"issued_on": None}),
            BackgroundTasks(),
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in results) == 1
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM practice_delivery_slots WHERE practice_id=7")
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM practice_delivery_fingerprints WHERE practice_id=7"
            )
            == 1
        )
    with pytest.raises(HTTPException, match="Upload every required"):
        await service.complete(pool, 7, STAFF, BackgroundTasks())
