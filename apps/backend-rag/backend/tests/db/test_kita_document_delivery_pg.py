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
from backend.services.portal.portal_service import PortalService

SQL_BASE = """
CREATE TABLE practice_types(id int primary key,code text,name text,category text);
CREATE TABLE practices(id int primary key,client_id int,family_member_id int,assigned_to text,
    client_visible boolean, status text, payment_status text,practice_type_id int,
    completion_date timestamptz,updated_at timestamptz);
CREATE TABLE client_family_members(id int primary key,client_id int);
CREATE TABLE documents(id serial primary key,practice_id int,client_id int,family_member_id int,
    intake_idempotency_key text UNIQUE,file_id text,file_name text,content_hash text,document_type text,uploaded_source text,status text,
    deleted_at timestamptz,deleted_by text,is_archived boolean,client_visible boolean,updated_at timestamptz);
CREATE TABLE companies(id int primary key);
CREATE TABLE company_documents(id serial primary key,company_id int,google_drive_file_id text,client_visible boolean);
CREATE TABLE client_company_links(company_id int,client_id int);
CREATE TABLE activity_log(entity_type text,entity_id int,action text,performed_by text,description text);
CREATE TABLE timeline_events(client_id int,practice_id int,event_type text,title text,description text,
    event_date timestamptz,client_visible boolean,color text,created_by text);
CREATE TABLE team_members(id varchar(36) primary key,email text,role text,active boolean);
CREATE TABLE hr_employees(id int primary key,team_member_id varchar(36) REFERENCES team_members(id),is_active boolean,profile jsonb);
CREATE TABLE hr_bonus_rates(id int primary key,practice_type_code text,is_active boolean,
    effective_from date,effective_to date,amount_idr bigint);
CREATE TABLE hr_bonus_ledger(id bigserial primary key, practice_id int,employee_id int,practice_type_code text, payroll_period_id int,
    bonus_rate_id int,amount_idr bigint,awarded_at timestamptz,status text);
CREATE TABLE hr_payroll_periods(id serial PRIMARY KEY,period_start date,payroll_month int,payroll_year int,status text);
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
            for filename in (
                "383_practice_delivery.sql",
                "384_delivery_bonus.sql",
                "385_practice_visa_followups.sql",
                "386_visa_bonus_payout_hold.sql",
            ):
                await conn.execute((folder / filename).read_text().split("-- === ROLLBACK ===")[0])
            await conn.execute("""
                CREATE TRIGGER hr_validate BEFORE INSERT ON hr_bonus_ledger
                    FOR EACH ROW EXECUTE FUNCTION hr_validate_completed_practice();
                INSERT INTO team_members VALUES('staff-1','consultant@example.test','consultant',true);
                INSERT INTO hr_employees VALUES(1,'staff-1',true,'{}'::jsonb);
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

                if data.company_id is not None:
                    await conn.execute(
                        """INSERT INTO company_documents(company_id,google_drive_file_id,client_visible)
                           SELECT $1,$2,false WHERE NOT EXISTS (
                           SELECT 1 FROM company_documents WHERE company_id=$1 AND google_drive_file_id=$2)""",
                        data.company_id,
                        data.intake_idempotency_key,
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
async def test_combined_renewal_earns_on_imk_upload_and_keeps_rate_gap(pool):
    result = await service.upload(
        pool, 8, "document_1", STAFF, final_file("combined-permit"), BackgroundTasks()
    )
    assert result["bonus_event"] == "kitas_imk_uploaded"
    assert result["status"] == "on_process"
    assert result["visa_followup"] is None
    doc_id = result["slots"][0]["document_id"]
    assert result["slots"][1]["reusable_document_id"] == doc_id
    with pytest.raises(HTTPException):
        await service.reuse(pool, 8, "document_2", STAFF, doc_id + 100)
    await service.reuse(pool, 8, "document_2", STAFF, doc_id)
    await service.waive(pool, 8, "document_3", STAFF, "No separate manpower filing in this order")
    result = await service.complete(pool, 8, STAFF, BackgroundTasks())
    assert result["bonus_event"] == "kitas_imk_uploaded"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["title", "slot", "fingerprint", "completion"])
async def test_direct_writers_cannot_reassign_an_evidenced_practice(pool, evidence):
    async with pool.acquire() as conn:
        if evidence == "title":
            await conn.execute(
                "INSERT INTO practice_delivery_titles VALUES (7,'visa_c1_tourism',ARRAY['Defined output'])"
            )
        elif evidence == "slot":
            await conn.execute(
                "INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,waiver_reason,updated_by) VALUES (7,'visa_c1_tourism','conditional','Not included','staff')"
            )
        elif evidence == "fingerprint":
            await conn.execute(
                "INSERT INTO practice_delivery_fingerprints VALUES (7,repeat('a',64),'visa_c1_tourism','electronic_visa')"
            )
        else:
            await conn.execute(
                "INSERT INTO practice_delivery_completions(practice_id,service_code,delivered_by) VALUES(7,'visa_c1_tourism','staff')"
            )
        for update in ("client_id=32", "family_member_id=19", "practice_type_id=2"):
            with pytest.raises(asyncpg.CheckViolationError) as refused:
                await conn.execute(f"UPDATE practices SET {update} WHERE id=7")
            assert refused.value.constraint_name == "practice_delivery_subject_immutable"
        # Idempotent upstream writes, unrelated state, and legacy imports are unaffected.
        await conn.execute(
            "UPDATE practices SET family_member_id=NULL,client_id=31,practice_type_id=1 WHERE id=7"
        )
        await conn.execute("UPDATE practices SET status='cancelled' WHERE id=7")
        await conn.execute(
            "UPDATE practices SET client_id=32,family_member_id=19,practice_type_id=1 WHERE id=8"
        )
        assert await conn.fetchval("SELECT status FROM practices WHERE id=7") == "cancelled"
        assert await conn.fetchval("SELECT client_id FROM practices WHERE id=8") == 32


@pytest.mark.asyncio
async def test_client_cannot_remove_final_evidence_and_staff_can_repair_after_completion(pool):
    client = {"client_id": 31, "email": "client@example.test"}
    vault = PortalService(pool)
    issuance = await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    with pytest.raises(HTTPException) as issued:
        await vault.soft_delete_document(
            31, issuance["slots"][0]["document_id"], current_user=client
        )
    assert issued.value.status_code == 409
    await service.upload(pool, 7, "document_2", STAFF, final_file("itk"), BackgroundTasks())
    closed = await service.complete(pool, 7, STAFF, BackgroundTasks())
    old = closed["slots"][1]["document_id"]
    with pytest.raises(HTTPException) as protected:
        await vault.soft_delete_document(31, old, current_user=client)
    assert protected.value.status_code == 409
    assert await vault.soft_delete_document(32, old, current_user={"client_id": 32}) is None
    async with pool.acquire() as conn:
        original = await conn.fetchrow(
            "SELECT delivered_at,delivered_by FROM practice_delivery_completions WHERE practice_id=7"
        )
        await conn.execute("UPDATE documents SET is_archived=true WHERE id=$1", old)
        view = await service.state(conn, await service.practice(conn, 7, STAFF), STAFF)
        assert view["slots"][1]["can_repair"] and not view["slots"][0]["can_repair"]
        assert view["visa_followup"]["fulfilled_at"] is None
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException):
        await service.upload(
            pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), tasks, repair=True
        )
    with pytest.raises(HTTPException):
        await service.upload(
            pool,
            7,
            "document_2",
            {"email": "other@example.test", "role": "consultant"},
            final_file("restored-itk"),
            tasks,
            repair=True,
        )
    repaired = await service.upload(
        pool, 7, "document_2", STAFF, final_file("restored-itk"), tasks, repair=True
    )
    assert (
        repaired["status"] == "completed"
        and repaired["visa_followup"]["fulfilled_at"]
        and not repaired["slots"][1]["can_repair"]
    )
    assert not tasks.tasks
    repeated = await service.upload(
        pool, 7, "document_2", STAFF, final_file("restored-itk"), tasks, repair=True
    )
    assert repeated["slots"][1]["document_id"] == repaired["slots"][1]["document_id"]
    with pytest.raises(HTTPException):
        await service.upload(
            pool, 7, "document_2", STAFF, final_file("different-live-itk"), tasks, repair=True
        )
    async with pool.acquire() as conn:
        assert original == await conn.fetchrow(
            "SELECT delivered_at,delivered_by FROM practice_delivery_completions WHERE practice_id=7"
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=7")
            == 1
        )
        assert await conn.fetchval("SELECT count(*) FROM timeline_events WHERE practice_id=7") == 1
        assert [d["file_name"] for d in (await service.portal_files(conn, 7, 31))["documents"]] == [
            "electronic-visa.pdf",
            "restored-itk.pdf",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False])
async def test_company_slot_replacement_uses_previous_company_identity(pool, shared):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO companies VALUES(81),(82); INSERT INTO client_company_links VALUES(81,31),(82,31)"
        )
    initial = await service.upload(
        pool,
        8,
        "document_1",
        STAFF,
        final_file("original-compound").model_copy(update={"company_id": 81}),
        BackgroundTasks(),
    )
    old_doc = initial["slots"][0]["document_id"]
    async with pool.acquire() as conn:
        old_company_doc = await conn.fetchval(
            "SELECT company_document_id FROM practice_delivery_slots WHERE practice_id=8 AND slot_key='document_1'"
        )
        unrelated = await conn.fetchval(
            "INSERT INTO company_documents(company_id,google_drive_file_id,client_visible) SELECT 82,file_id,true FROM documents WHERE id=$1 RETURNING id",
            old_doc,
        )
    if shared:
        await service.reuse(pool, 8, "document_2", STAFF, old_doc)
    await service.upload(
        pool, 8, "document_1", STAFF, final_file("replaced-imk"), BackgroundTasks()
    )
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT client_visible FROM company_documents WHERE id=$1", old_company_doc
            )
            is shared
        )
        assert (
            await conn.fetchval(
                "SELECT client_visible FROM company_documents WHERE id=$1", unrelated
            )
            is True
        )
        assert (
            await conn.fetchval(
                "SELECT company_id FROM practice_delivery_slots WHERE practice_id=8 AND slot_key='document_1'"
            )
            is None
        )


@pytest.mark.asyncio
async def test_completed_renewal_can_restore_a_missing_compound_permit_without_another_award(pool):
    original = await service.upload(
        pool, 8, "document_1", STAFF, final_file("old-combined"), BackgroundTasks()
    )
    old_id = original["slots"][0]["document_id"]
    await service.reuse(pool, 8, "document_2", STAFF, old_id)
    await service.waive(pool, 8, "document_3", STAFF, "Not in the original contract")
    await service.complete(pool, 8, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        await conn.execute("UPDATE documents SET is_archived=true WHERE id=$1", old_id)
    recovered = await service.upload(
        pool,
        8,
        "document_1",
        STAFF,
        final_file("replacement-combined"),
        BackgroundTasks(),
        repair=True,
    )
    new_id = recovered["slots"][0]["document_id"]
    assert (
        recovered["slots"][1]["can_repair"]
        and recovered["slots"][1]["reusable_document_id"] == new_id
    )
    repaired = await service.reuse(pool, 8, "document_2", STAFF, new_id)
    assert [s["document_id"] for s in repaired["slots"][:2]] == [new_id, new_id]
    assert not repaired["slots"][0]["can_repair"] and not repaired["slots"][1]["can_repair"]
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=8")
            == 1
        )
        assert await conn.fetchval("SELECT count(*) FROM timeline_events WHERE practice_id=8") == 1
        assert (await service.portal_files(conn, 8, 31))["documents"] == [
            {"id": new_id, "file_name": "replacement-combined.pdf"}
        ]
