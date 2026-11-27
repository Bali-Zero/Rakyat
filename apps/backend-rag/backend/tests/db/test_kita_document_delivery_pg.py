"""Run the issued visa → post-entry permit → final delivery on a real test PostgreSQL.

Only the external file sink and post-commit cache invalidations are substituted.
CI exposes TEST_DATABASE_URL on every backend shard. Each test owns a disposable schema.
"""

import asyncio
import base64
import hashlib
import os
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException

from backend.app.routers.crm_enhanced_documents import _active_duplicate_document_id
from backend.app.routers.crm_practice_delivery import DeliveryFile
from backend.services.crm import practice_delivery as service
from backend.services.portal.portal_service import PortalService

SQL_BASE = """
CREATE TABLE practice_types(id int primary key,code text,name text,category text);
CREATE TABLE practices(id int primary key,client_id int,family_member_id int,assigned_to text,
    client_visible boolean, status text, payment_status text,practice_type_id int,
    completion_date timestamptz,updated_at timestamptz,quoted_price numeric(12,2),discount_amount numeric(12,2));
CREATE TABLE invoices(id serial primary key,practice_id int,amount_idr numeric);
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
CREATE TABLE hr_bonus_rates(id serial primary key,practice_type_code text unique,is_active boolean,
    effective_from date,effective_to date,amount_idr bigint,notes text,updated_at timestamptz);
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
                "388_bonus_rates_forfeit.sql",
                "390_first_proof_bonus.sql",
            ):
                await conn.execute((folder / filename).read_text().split("-- === ROLLBACK ===")[0])
            await conn.execute("""
                CREATE TRIGGER hr_validate BEFORE INSERT ON hr_bonus_ledger
                    FOR EACH ROW EXECUTE FUNCTION hr_validate_completed_practice();
                INSERT INTO team_members VALUES('staff-1','consultant@example.test','consultant',true);
                INSERT INTO hr_employees VALUES(1,'staff-1',true,'{}'::jsonb);
                INSERT INTO practice_types VALUES(1,'visa_c1_tourism','C1','single_entry_visa'),
                    (2,'kitas_working_extend','Working KITAS renewal','kitas'),
                    (3,'kitap_dependent_merp','Dependent KITAP MERP','kitap'),
                    (4,'ext_b1_voa','VOA extension','visa'),(5,'other_custom','Custom','other');
                INSERT INTO hr_bonus_rates(practice_type_code,is_active,effective_from,amount_idr)
                    VALUES('visa_c1_tourism',true,'2025-01-01',100000),
                    ('kitap_dependent_merp',true,'2025-01-01',175000);
                INSERT INTO practices VALUES(7,31,null,'consultant@example.test',true,'on_process','paid',1,null,now()),
                    (8,31,null,'consultant@example.test',true,'on_process','paid',2,null,now());
            """)

        async def sink(client_id, data, **kwargs):
            assert data.client_visible is False
            digest = hashlib.md5(base64.b64decode(data.file), usedforsecurity=False).hexdigest()
            async with db.acquire() as conn:
                if data.company_id is None and await _active_duplicate_document_id(
                    conn,
                    client_id,
                    digest,
                    practice_id=data.practice_id,
                    family_member_id=data.family_member_id,
                    missing_delivery_slot=kwargs.get("missing_delivery_slot"),
                ):
                    raise HTTPException(409, "Duplicate file: same subject and contents")
                await conn.execute(
                    """INSERT INTO documents(practice_id,client_id,family_member_id,
                    intake_idempotency_key,file_id,file_name,uploaded_source,status,client_visible,document_type,content_hash)
                    VALUES($1,$2,$3,$4,$4,$5,'team','active',false,$6,$7)
                    ON CONFLICT(intake_idempotency_key) DO NOTHING""",
                    data.practice_id,
                    client_id,
                    data.family_member_id,
                    data.intake_idempotency_key,
                    data.file_name,
                    data.document_type,
                    digest,
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
async def test_concurrent_issue_one_bonus_and_first_proof_completion(pool):
    # The same issuance submitted twice completes the practice once and pays once.
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
        assert tuple(row.values()) == ("completed", "pending", "electronic_visa_issued", 100000)
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
        assert await conn.fetchval("SELECT count(*) FROM practice_delivery_completions") == 1
        assert await conn.fetchval("SELECT count(*) FROM timeline_events") == 1
        portal = await service.portal_files(conn, 7, 31)
        assert [r["file_name"] for r in portal["documents"]] == ["electronic-visa.pdf"]
        with pytest.raises(HTTPException):
            await service.portal_files(conn, 7, 32)
    # complete() on an already completed practice is an idempotent replay.
    assert (await service.complete(pool, 7, STAFF, BackgroundTasks()))["status"] == "completed"
    await service.upload(
        pool, 7, "document_2", STAFF, final_file("itk"), BackgroundTasks(), repair=True
    )
    async with pool.acquire() as conn:
        assert len((await service.portal_files(conn, 7, 31))["documents"]) == 2
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert len((await service.portal_files(conn, 7, 31))["documents"]) == 2
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
        assert await conn.fetchval("SELECT count(*) FROM timeline_events") == 1


@pytest.mark.asyncio
async def test_combined_renewal_completes_on_imk_upload_and_keeps_rate_gap(pool):
    await service.waive(pool, 8, "document_3", STAFF, "No separate manpower filing in this order")
    result = await service.upload(
        pool, 8, "document_1", STAFF, final_file("combined-permit"), BackgroundTasks()
    )
    assert result["bonus_event"] == "kitas_imk_uploaded"
    assert result["status"] == "completed"
    assert result["visa_followup"] is None
    doc_id = result["slots"][0]["document_id"]
    assert result["slots"][1]["reusable_document_id"] == doc_id
    with pytest.raises(HTTPException):
        await service.reuse(pool, 8, "document_2", STAFF, doc_id + 100, BackgroundTasks())
    # The combined file reaches the completed case through the repair path.
    await service.reuse(pool, 8, "document_2", STAFF, doc_id, BackgroundTasks())
    result = await service.complete(pool, 8, STAFF, BackgroundTasks())
    assert result["bonus_event"] == "kitas_imk_uploaded"
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM documents") == 1
        assert await conn.fetchval("SELECT count(*) FROM practice_delivery_completions") == 1
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
        await service.upload(
            pool, 7, "document_2", STAFF, disguised, BackgroundTasks(), repair=True
        )
    # An old upload might have left all current slot pointers after a correction.
    await service.upload(
        pool,
        7,
        "electronic_visa",
        STAFF,
        final_file("corrected-electronic-visa"),
        BackgroundTasks(),
        repair=True,
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE documents SET is_archived=true WHERE id=$1", first_id)
    with pytest.raises(HTTPException, match="another output"):
        await service.upload(
            pool, 7, "document_2", STAFF, disguised, BackgroundTasks(), repair=True
        )
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id=$1", first_id)
    with pytest.raises(HTTPException, match="another output"):
        await service.upload(
            pool, 7, "document_2", STAFF, disguised, BackgroundTasks(), repair=True
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=7") == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM practice_delivery_slots WHERE practice_id=7")
            == 1
        )
    # The first proof already completed the practice; the follow-up is still unfulfilled.
    closed = await service.complete(pool, 7, STAFF, BackgroundTasks())
    assert closed["status"] == "completed" and closed["visa_followup"]["fulfilled_at"] is None


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
        # Whichever file won the race, the loser is never counted as the other output.
        won = await conn.fetchval(
            "SELECT slot_key FROM practice_delivery_slots WHERE practice_id=7"
        )
        completed = await conn.fetchval("SELECT count(*) FROM practice_delivery_completions")
        assert completed == (1 if won == "electronic_visa" else 0)
    if completed == 0:
        with pytest.raises(HTTPException, match="Upload the first final document first"):
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
    assert issuance["status"] == "completed"
    with pytest.raises(HTTPException) as issued:
        await vault.soft_delete_document(
            31, issuance["slots"][0]["document_id"], current_user=client
        )
    assert issued.value.status_code == 409
    await service.upload(
        pool, 7, "document_2", STAFF, final_file("itk"), BackgroundTasks(), repair=True
    )
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
        # Every non-waived slot of a completed practice accepts a file, empty or filled.
        assert view["slots"][1]["can_repair"] and view["slots"][0]["can_repair"]
        assert view["visa_followup"]["fulfilled_at"] is None
    tasks = BackgroundTasks()
    # Replaying the first proof's bytes is idempotent: same document, no new award.
    replay = await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), tasks, repair=True
    )
    assert replay["slots"][0]["document_id"] == issuance["slots"][0]["document_id"]
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
    assert repaired["status"] == "completed" and repaired["visa_followup"]["fulfilled_at"]
    assert not tasks.tasks
    repeated = await service.upload(
        pool, 7, "document_2", STAFF, final_file("restored-itk"), tasks, repair=True
    )
    assert repeated["slots"][1]["document_id"] == repaired["slots"][1]["document_id"]
    # A different live file now replaces the slot (replace is open to every slot).
    replaced = await service.upload(
        pool, 7, "document_2", STAFF, final_file("different-live-itk"), tasks, repair=True
    )
    assert replaced["slots"][1]["document_id"] != repaired["slots"][1]["document_id"]
    async with pool.acquire() as conn:
        assert original == await conn.fetchrow(
            "SELECT delivered_at,delivered_by FROM practice_delivery_completions WHERE practice_id=7"
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=7")
            == 1
        )
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
        assert await conn.fetchval("SELECT count(*) FROM timeline_events WHERE practice_id=7") == 1
        assert [d["file_name"] for d in (await service.portal_files(conn, 7, 31))["documents"]] == [
            "electronic-visa.pdf",
            "different-live-itk.pdf",
        ]


@pytest.mark.asyncio
async def test_practice_completed_before_the_cutoff_receives_its_documents_but_earns_nothing(pool):
    # Closed by a status edit before Final delivery existed: no completion record, and a
    # completion date before LATE_PROOF_FROM, so its proof settles nothing now.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE practices SET status='completed',completion_date='2026-10-27 08:00+08' WHERE id=7"
        )
        view = await service.state(conn, await service.practice(conn, 7, STAFF), STAFF)
    assert not view["delivery_recorded"]
    assert view["slots"][1]["can_repair"] and view["slots"][0]["can_repair"]
    tasks = BackgroundTasks()
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), tasks, repair=True
    )
    attached = await service.upload(
        pool, 7, "document_2", STAFF, final_file("late-itk"), tasks, repair=True
    )
    assert attached["status"] == "completed" and not attached["delivery_recorded"]
    assert attached["slots"][1]["document_id"] and attached["slots"][1]["can_repair"]
    assert attached["visa_followup"] is None
    assert not tasks.tasks
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM hr_delivery_bonus_events") == 0
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 0
        assert await conn.fetchval("SELECT count(*) FROM practice_delivery_completions") == 0
        assert await conn.fetchval("SELECT status FROM practices WHERE id=7") == "completed"
        assert sorted(
            d["file_name"] for d in (await service.portal_files(conn, 7, 31))["documents"]
        ) == ["electronic-visa.pdf", "late-itk.pdf"]


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
        await service.reuse(pool, 8, "document_2", STAFF, old_doc, BackgroundTasks())
    # The practice completed on its first proof; the replacement is a repair and keeps the
    # company identity of the slot it replaces.
    await service.upload(
        pool, 8, "document_1", STAFF, final_file("replaced-imk"), BackgroundTasks(), repair=True
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
            == 81
        )


@pytest.mark.asyncio
async def test_completed_renewal_can_restore_a_missing_compound_permit_without_another_award(pool):
    await service.waive(pool, 8, "document_3", STAFF, "Not in the original contract")
    original = await service.upload(
        pool, 8, "document_1", STAFF, final_file("old-combined"), BackgroundTasks()
    )
    old_id = original["slots"][0]["document_id"]
    await service.reuse(pool, 8, "document_2", STAFF, old_id, BackgroundTasks())
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
    repaired = await service.reuse(pool, 8, "document_2", STAFF, new_id, BackgroundTasks())
    assert [s["document_id"] for s in repaired["slots"][:2]] == [new_id, new_id]
    # Filled slots of a completed practice stay open to a corrected file.
    assert repaired["slots"][0]["can_repair"] and repaired["slots"][1]["can_repair"]
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=8")
            == 1
        )
        assert await conn.fetchval("SELECT count(*) FROM timeline_events WHERE practice_id=8") == 1
        assert (await service.portal_files(conn, 8, 31))["documents"] == [
            {"id": new_id, "file_name": "replacement-combined.pdf"}
        ]


@pytest.mark.asyncio
async def test_same_bytes_repair_only_exempts_its_missing_storage_pointer(pool):
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    permit = final_file("itk")
    posted = await service.upload(
        pool, 7, "document_2", STAFF, permit, BackgroundTasks(), repair=True
    )
    original = posted["slots"][1]["document_id"]
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        digest = await conn.fetchval("SELECT content_hash FROM documents WHERE id=$1", original)
        # A real file continues to deduplicate even if the caller names its slot.
        assert (
            await _active_duplicate_document_id(
                conn, 31, digest, practice_id=7, missing_delivery_slot="document_2"
            )
            == original
        )
        await conn.execute("UPDATE documents SET file_id=NULL WHERE id=$1", original)
        assert await _active_duplicate_document_id(conn, 31, digest, practice_id=7) == original
        assert (
            await _active_duplicate_document_id(
                conn, 31, digest, practice_id=7, missing_delivery_slot="electronic_visa"
            )
            == original
        )
        assert (
            await _active_duplicate_document_id(
                conn, 31, digest, practice_id=7, missing_delivery_slot="document_2"
            )
            is None
        )
    repaired = await service.upload(
        pool, 7, "document_2", STAFF, permit, BackgroundTasks(), repair=True
    )
    assert repaired["status"] == "completed" and repaired["slots"][1]["document_id"] != original
    assert repaired["visa_followup"]["fulfilled_at"]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=7") == 1
        assert await conn.fetchval("SELECT count(*) FROM timeline_events WHERE practice_id=7") == 1


async def add_practice(conn, practice_id, type_id, status="on_process", completed_at=None):
    await conn.execute(
        """INSERT INTO practices(id,client_id,assigned_to,client_visible,status,payment_status,
               practice_type_id,completion_date,updated_at)
           VALUES($1,31,'consultant@example.test',true,$2,'paid',$3,$4,now())""",
        practice_id,
        status,
        type_id,
        completed_at,
    )


async def counts(conn, practice_id):
    return {
        "events": await conn.fetchval(
            "SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=$1", practice_id
        ),
        "ledger": await conn.fetchval(
            "SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=$1", practice_id
        ),
        "completions": await conn.fetchval(
            "SELECT count(*) FROM practice_delivery_completions WHERE practice_id=$1", practice_id
        ),
        "timeline": await conn.fetchval(
            "SELECT count(*) FROM timeline_events WHERE practice_id=$1", practice_id
        ),
    }


@pytest.mark.asyncio
async def test_first_document_of_a_service_delivered_practice_completes_and_earns(pool):
    async with pool.acquire() as conn:
        await add_practice(conn, 9, 3)
    first = await service.upload(
        pool, 9, "document_1", STAFF, final_file("eitap"), BackgroundTasks()
    )
    assert first["status"] == "completed" and first["bonus_event"] == "service_delivered"
    assert first["delivery_recorded"] and first["slots"][1]["document_id"] is None
    async with pool.acquire() as conn:
        assert await counts(conn, 9) == {"events": 1, "ledger": 1, "completions": 1, "timeline": 1}
        row = await conn.fetchrow(
            "SELECT amount_idr, earned_at > now() - interval '1 minute' AS fresh,"
            " (SELECT completion_date FROM practices WHERE id=9) IS NOT NULL AS dated"
            " FROM hr_delivery_bonus_events WHERE practice_id=9"
        )
        assert tuple(row.values()) == (175000, True, True)
        assert [d["file_name"] for d in (await service.portal_files(conn, 9, 31))["documents"]] == [
            "eitap.pdf"
        ]
    # The other box is filled later on the completed practice and never pays again.
    tasks = BackgroundTasks()
    later = await service.upload(
        pool, 9, "document_2", STAFF, final_file("imk"), tasks, repair=True
    )
    assert later["slots"][1]["document_id"] and later["status"] == "completed"
    assert not tasks.tasks
    async with pool.acquire() as conn:
        assert await counts(conn, 9) == {"events": 1, "ledger": 1, "completions": 1, "timeline": 1}
        assert len((await service.portal_files(conn, 9, 31))["documents"]) == 2


@pytest.mark.asyncio
async def test_evisa_upload_completes_the_visa_and_starts_the_followup_that_itk_fulfils(pool):
    first = await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    assert first["status"] == "completed" and first["bonus_event"] == "electronic_visa_issued"
    assert first["visa_followup"] and first["visa_followup"]["fulfilled_at"] is None
    async with pool.acquire() as conn:
        assert await counts(conn, 7) == {"events": 1, "ledger": 1, "completions": 1, "timeline": 1}
    fulfilled = await service.upload(
        pool, 7, "document_2", STAFF, final_file("itk"), BackgroundTasks(), repair=True
    )
    assert fulfilled["visa_followup"]["fulfilled_at"] and fulfilled["status"] == "completed"
    async with pool.acquire() as conn:
        assert await counts(conn, 7) == {"events": 1, "ledger": 1, "completions": 1, "timeline": 1}


@pytest.mark.asyncio
async def test_double_submitted_first_proof_completes_and_pays_once(pool):
    async with pool.acquire() as conn:
        await add_practice(conn, 9, 3)
    results = await asyncio.gather(
        *[
            service.upload(pool, 9, "document_1", STAFF, final_file("eitap"), BackgroundTasks())
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert all(isinstance(r, dict) and r["status"] == "completed" for r in results), results
    async with pool.acquire() as conn:
        assert await counts(conn, 9) == {"events": 1, "ledger": 1, "completions": 1, "timeline": 1}
        assert (
            await conn.fetchval("SELECT count(*) FROM practice_delivery_slots WHERE practice_id=9")
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completed_on,earns",
    [
        (datetime(2026, 11, 17, 4, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 27, 4, tzinfo=timezone.utc), False),
    ],
)
async def test_legacy_completed_practice_earns_on_its_proof_only_from_november(
    pool, completed_on, earns
):
    async with pool.acquire() as conn:
        await add_practice(conn, 9, 3, status="completed", completed_at=completed_on)
    result = await service.upload(
        pool, 9, "document_1", STAFF, final_file("eitap"), BackgroundTasks(), repair=True
    )
    assert result["status"] == "completed" and not result["delivery_recorded"]
    async with pool.acquire() as conn:
        # No completion row is ever invented for a practice closed by a status edit.
        assert await counts(conn, 9) == {
            "events": int(earns),
            "ledger": int(earns),
            "completions": 0,
            "timeline": 0,
        }
    # Replaying the same proof does not pay twice.
    await service.upload(
        pool, 9, "document_1", STAFF, final_file("eitap"), BackgroundTasks(), repair=True
    )
    async with pool.acquire() as conn:
        assert (await counts(conn, 9))["events"] == int(earns)


@pytest.mark.asyncio
async def test_a_zero_rate_is_no_bonus_never_a_ledger_row(pool):
    async with pool.acquire() as conn:
        await add_practice(conn, 9, 5)
        await conn.execute(
            "INSERT INTO practice_delivery_titles VALUES (9,'other_custom',ARRAY['Final advice'])"
        )
        view = await service.state(conn, await service.practice(conn, 9, STAFF), STAFF)
        assert view["bonus_rate"] == 0
    done = await service.upload(pool, 9, "custom_1", STAFF, final_file("advice"), BackgroundTasks())
    assert done["status"] == "completed" and done["bonus_status"] == "no_bonus"
    assert done["bonus_rate"] == 0
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT amount_idr FROM hr_delivery_bonus_events WHERE practice_id=9"
            )
            == 0
        )
        assert await counts(conn, 9) == {"events": 1, "ledger": 0, "completions": 1, "timeline": 1}


@pytest.mark.asyncio
async def test_replayed_upload_and_company_inheritance_on_a_completed_practice(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO companies VALUES(81),(82); INSERT INTO client_company_links VALUES(81,31),(82,31)"
        )
        await add_practice(conn, 9, 3)
    company = final_file("eitap").model_copy(update={"company_id": 81})
    await service.upload(pool, 9, "document_1", STAFF, company, BackgroundTasks())
    # An empty slot of the completed case joins the company the other slot already names.
    joined = await service.upload(
        pool, 9, "document_2", STAFF, final_file("imk"), BackgroundTasks(), repair=True
    )
    # A repair cannot move a slot to another company.
    with pytest.raises(HTTPException, match="cannot change"):
        await service.upload(
            pool,
            9,
            "document_2",
            STAFF,
            final_file("other-company").model_copy(update={"company_id": 82}),
            BackgroundTasks(),
            repair=True,
        )
    replay = await service.upload(
        pool, 9, "document_2", STAFF, final_file("imk"), BackgroundTasks(), repair=True
    )
    assert replay["slots"][1]["document_id"] == joined["slots"][1]["document_id"]
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT array_agg(company_id ORDER BY slot_key) FROM practice_delivery_slots WHERE practice_id=9"
        ) == [81, 81]
        assert await counts(conn, 9) == {"events": 1, "ledger": 1, "completions": 1, "timeline": 1}


@pytest.mark.asyncio
async def test_migration_390_backfills_legacy_november_proofs_and_zeroes_custom_receipts(pool):
    folder = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
    migration = (folder / "390_first_proof_bonus.sql").read_text().split("-- === ROLLBACK ===")[0]
    nov = datetime(2026, 11, 17, 4, tzinfo=timezone.utc)
    async with pool.acquire() as conn:
        for pid, type_id, status, when in (
            (20, 4, "completed", nov),
            (21, 4, "completed", datetime(2026, 10, 27, 4, tzinfo=timezone.utc)),
            (22, 4, "completed", nov),
            (23, 2, "completed", nov),
            (24, 5, "on_process", None),
            (25, 4, "completed", nov),
        ):
            await add_practice(conn, pid, type_id, status=status, completed_at=when)
            if pid != 24:
                doc = await conn.fetchval(
                    "INSERT INTO documents(practice_id,client_id,file_id,file_name,status,client_visible)"
                    " VALUES($1::int,31,'f'||$1::text,'proof.pdf','active',false) RETURNING id",
                    pid,
                )
                await conn.execute(
                    "INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,updated_by,updated_at)"
                    " SELECT $1,pt.code,'document_1',$2,'staff','2026-11-18 10:00+08' FROM practices p"
                    " JOIN practice_types pt ON pt.id=p.practice_type_id WHERE p.id=$1",
                    pid,
                    doc,
                )
        # Paid at or above the forfeit price, except practice 25 which was paid below it.
        await conn.execute(
            "UPDATE practices SET quoted_price=CASE WHEN id=25 THEN 500000 ELSE 1000000 END"
        )
        await conn.execute(
            "INSERT INTO practice_delivery_completions(practice_id,service_code,delivered_by) VALUES(22,'ext_b1_voa','staff')"
        )
        await conn.execute(
            "INSERT INTO hr_delivery_bonus_events(practice_id,employee_id,service_code,event_kind,earned_at,awarded_at)"
            " VALUES(24,1,'other_custom','service_delivered','2026-11-20 10:00+08','2026-11-20 10:00+08')"
        )
        await conn.execute(migration)
        await conn.execute(migration)  # replay: nothing new
        events = {
            r["practice_id"]: r
            for r in await conn.fetch(
                "SELECT practice_id,event_kind,amount_idr,ledger_id,earned_at FROM hr_delivery_bonus_events"
            )
        }
        assert sorted(events) == [20, 23, 24, 25]
        assert events[20]["event_kind"] == "service_delivered" and events[20]["amount_idr"] == 15000
        assert events[20]["earned_at"] == datetime(2026, 11, 18, 2, tzinfo=timezone.utc)
        assert events[23]["event_kind"] == "kitas_imk_uploaded"
        assert events[24]["amount_idr"] == 0
        assert events[25]["amount_idr"] is None
        assert await conn.fetchval(
            "SELECT forfeited FROM hr_delivery_bonus_events WHERE practice_id=25"
        )
        assert all(e["ledger_id"] is None for e in events.values())
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 0
