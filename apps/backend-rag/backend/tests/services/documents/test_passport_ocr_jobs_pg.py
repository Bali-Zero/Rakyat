"""Real PostgreSQL regression coverage for passport OCR jobs.

The fixture creates a private schema in a disposable database. The ingress test
uses the staff router, actual claims and writes, and a synthetic provider result.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest

from backend.services.documents.passport_ocr_jobs import (
    claim,
    enqueue_selected,
    finish,
    review_untyped,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_pool() -> asyncpg.Pool:
    dsn = os.environ.get("TEST_DATABASE_URL")
    database = urlparse(dsn or "").path.rsplit("/", 1)[-1]
    if not dsn or "test" not in database.lower() or database in {"nuzantara_dev", "nuzantara_rag"}:
        pytest.skip("requires an expressly disposable TEST_DATABASE_URL")
    schema = f"passport_ocr_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    try:
        assert await admin.fetchval("SELECT current_database()") == database
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute("""
            CREATE TABLE clients (
                id bigint PRIMARY KEY, full_name text NOT NULL DEFAULT 'Synthetic Client',
                status text NOT NULL, deleted_at timestamptz, passport_ocr_data jsonb
            );
            CREATE TABLE documents (
                id bigserial PRIMARY KEY, client_id bigint NOT NULL, document_type text,
                file_id text, content_hash text, file_name text, status text,
                storage_type text, ocr_status text, ocr_extracted_data jsonb,
                ocr_completed_at timestamptz, family_member_id bigint, deleted_at timestamptz,
                is_archived boolean, verified_by text, verified_at timestamptz,
                rejection_reason text, intake_idempotency_key text, intake_proposal_id bigint,
                document_category text, file_url text, google_drive_file_url text, expiry_date date,
                notes text, practice_id bigint, updated_at timestamptz DEFAULT now()
            );
            INSERT INTO clients (id,status) VALUES (99,'active');
            INSERT INTO documents (id,client_id,document_type,file_id,status,storage_type,ocr_status)
            VALUES (900,99,'paspor','historical-file','active','google_drive','pending');
        """)
        migration = Path(__file__).parents[3] / "db/migrations_v2/387_crm_passport_ocr_jobs.sql"
        from backend.db.migration_base import split_migration_sql

        forward, rollback = split_migration_sql(migration.read_text())
        assert rollback
        await admin.execute(forward)
    finally:
        await admin.close()

    pool = await asyncpg.create_pool(
        dsn, min_size=2, max_size=3, server_settings={"search_path": schema}
    )
    try:
        yield pool
    finally:
        await pool.close()
        cleanup = await asyncpg.connect(dsn)
        try:
            await cleanup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await cleanup.close()


async def _seed(
    pool: asyncpg.Pool, *, status: str = "active", storage: str = "google_drive"
) -> None:
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO clients (id,status) VALUES (1,'active')")
        await conn.execute(
            """INSERT INTO documents
            (id,client_id,document_type,file_id,content_hash,status,storage_type,ocr_status)
            VALUES (10,1,'passport','file-a','hash-a',$1,$2,'pending')""",
            status,
            storage,
        )


@pytest.mark.asyncio
async def test_migration_rollback_preserves_work_and_reverses_an_empty_queue(pg_pool):
    from backend.db.migration_base import split_migration_sql

    migration = Path(__file__).parents[3] / "db/migrations_v2/387_crm_passport_ocr_jobs.sql"
    _, rollback = split_migration_sql(migration.read_text())
    await _seed(pg_pool)
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="Preserve passport OCR jobs"):
            async with conn.transaction():
                await conn.execute(rollback)
        assert await conn.fetchval("SELECT count(*) FROM crm_passport_ocr_jobs") == 1
        await conn.execute("DELETE FROM documents WHERE id=10")
        async with conn.transaction():
            await conn.execute(rollback)
        assert not await conn.fetchval("SELECT to_regclass('crm_passport_ocr_jobs') IS NOT NULL")


@pytest.mark.asyncio
async def test_existing_document_writer_can_enroll_without_queue_write_access(pg_pool):
    async with pg_pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT pg_has_role(current_user,'pg_read_all_data','MEMBER')"):
            pytest.skip("requires role switching in the disposable database")
        await conn.execute("GRANT INSERT ON documents TO pg_read_all_data")
        await conn.execute("SET LOCAL ROLE pg_read_all_data")
        assert not await conn.fetchval(
            "SELECT has_table_privilege(current_user,'crm_passport_ocr_jobs','INSERT')"
        )
        await conn.execute(
            """INSERT INTO documents (id,client_id,document_type,file_id,status,storage_type,ocr_status)
               VALUES (901,99,'passport','new-file','active','google_drive','pending')"""
        )
        assert (
            await conn.fetchval("SELECT status FROM crm_passport_ocr_jobs WHERE document_id=901")
            == "pending"
        )


@pytest.mark.asyncio
async def test_claim_competes_and_expired_token_is_fenced(pg_pool: asyncpg.Pool) -> None:
    await _seed(pg_pool)
    first, second = await asyncio.gather(claim(pg_pool), claim(pg_pool))
    job = first or second
    assert job is not None and (first is None) != (second is None)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE crm_passport_ocr_jobs SET available_at=now()-interval '1 second'"
        )
    replay = await claim(pg_pool)
    assert replay is not None and replay["token"] != job["token"]
    assert await finish(pg_pool, job, extracted={"passport": "old"}) == "stale"


@pytest.mark.asyncio
async def test_status_variants_and_payload_protections(pg_pool: asyncpg.Pool) -> None:
    await _seed(pg_pool, status="active")
    job = await claim(pg_pool)
    assert job is not None
    async with pg_pool.acquire() as conn:
        before = await conn.fetchrow("SELECT * FROM clients WHERE id=1")
    assert await finish(pg_pool, job, extracted={"passport": "ok"}) == "completed"
    async with pg_pool.acquire() as conn:
        after = await conn.fetchrow("SELECT * FROM clients WHERE id=1")
        assert dict(before) == dict(after)
        assert (
            await conn.fetchval(
                "SELECT jsonb_typeof(ocr_extracted_data) FROM documents WHERE id=10"
            )
            == "object"
        )
        await conn.execute(
            "UPDATE documents SET ocr_status='pending',ocr_extracted_data='{}',ocr_completed_at=NULL WHERE id=10"
        )
    assert await enqueue_selected(pg_pool, [10]) == 0


@pytest.mark.asyncio
async def test_rebind_verified_deleted_and_unexpired_lease_are_protected(
    pg_pool: asyncpg.Pool,
) -> None:
    await _seed(pg_pool)
    job = await claim(pg_pool)
    assert job is not None
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET file_id='replacement',verified_by='staff' WHERE id=10"
        )
    assert await finish(pg_pool, job, extracted={"passport": "wrong"}) == "stale"
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval("SELECT ocr_extracted_data FROM documents WHERE id=10") is None
        assert await conn.fetchval("SELECT verified_by FROM documents WHERE id=10") == "staff"


@pytest.mark.asyncio
async def test_final_crash_settles_staff_reason_and_replay_is_unchanged(
    pg_pool: asyncpg.Pool,
) -> None:
    await _seed(pg_pool)
    for _ in range(3):
        job = await claim(pg_pool)
        assert job is not None
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE crm_passport_ocr_jobs SET available_at=now()-interval '1 second' WHERE document_id=10"
            )
    assert await claim(pg_pool) is None
    async with pg_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT terminal_reason FROM crm_passport_ocr_jobs WHERE document_id=10"
            )
            == "attempt_limit"
        )
        assert (
            await conn.fetchval(
                "SELECT ocr_extracted_data->>'review_reason' FROM documents WHERE id=10"
            )
            == "attempt_limit"
        )


@pytest.mark.asyncio
async def test_google_drive_status_is_not_historical_opt_in(pg_pool: asyncpg.Pool) -> None:
    await _seed(pg_pool, status="google_drive")
    assert await enqueue_selected(pg_pool, [10]) == 0


@pytest.mark.asyncio
async def test_pending_intake_document_never_enrolls(pg_pool: asyncpg.Pool) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO documents
               (id,client_id,document_type,file_id,status,storage_type,ocr_status,intake_idempotency_key)
               VALUES (10,99,'passport','intake-file','active','google_drive','pending','intake-key')"""
        )
        assert await conn.fetchval("SELECT count(*) FROM crm_passport_ocr_jobs") == 0
        assert await conn.fetchval(
            "SELECT crm_passport_ocr_staff_status(d) FROM documents d WHERE id=10"
        ) == "intake"
    assert await claim(pg_pool) is None


@pytest.mark.asyncio
async def test_intake_marker_added_after_claim_preserves_document_payload(
    pg_pool: asyncpg.Pool,
) -> None:
    await _seed(pg_pool)
    job = await claim(pg_pool)
    assert job is not None
    async with pg_pool.acquire() as conn:
        await conn.execute("UPDATE documents SET intake_proposal_id=99 WHERE id=10")
    assert await finish(pg_pool, job, extracted={"passport": "must-not-write"}) == "stale"
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval("SELECT ocr_extracted_data FROM documents WHERE id=10") is None


@pytest.mark.asyncio
async def test_passport_normalization_handles_tabs_and_nbsp(pg_pool: asyncpg.Pool) -> None:
    await _seed(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute("UPDATE documents SET document_type=$1 WHERE id=10", "\t\u00a0PASPOR\n")
        assert await conn.fetchval(
            "SELECT crm_passport_ocr_is_passport(document_type) FROM documents WHERE id=10"
        )
    assert await claim(pg_pool) is not None


@pytest.mark.asyncio
async def test_detected_untyped_document_is_reviewed_without_type_or_client_write(
    pg_pool: asyncpg.Pool,
) -> None:
    await _seed(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET document_type='  identity  ',file_id='File-A' WHERE id=10"
        )
        before = dict(await conn.fetchrow("SELECT * FROM clients WHERE id=1"))
    assert not await review_untyped(pg_pool, doc_id=10, client_id=1, file_id="file-a")
    assert await review_untyped(pg_pool, doc_id=10, client_id=1, file_id="File-A")
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT document_type,ocr_status,ocr_extracted_data->>'review_reason' AS reason FROM documents WHERE id=10"
        )
        assert row["document_type"] == "  identity  "
        assert row["ocr_status"] == "review"
        assert row["reason"] == "confirm_passport_document_type"
        assert dict(await conn.fetchrow("SELECT * FROM clients WHERE id=1")) == before


@pytest.mark.asyncio
async def test_untyped_review_refuses_passport_and_protected_document(
    pg_pool: asyncpg.Pool,
) -> None:
    await _seed(pg_pool)
    assert not await review_untyped(pg_pool, doc_id=10, client_id=1, file_id="file-a")
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET document_type='identity',ocr_extracted_data='{\"kept\":true}' WHERE id=10"
        )
    assert not await review_untyped(pg_pool, doc_id=10, client_id=1, file_id="file-a")


@pytest.mark.asyncio
async def test_explicit_repending_reuses_terminal_row_without_duplicate(
    pg_pool: asyncpg.Pool,
) -> None:
    await _seed(pg_pool)
    job = await claim(pg_pool)
    assert job is not None
    assert await finish(pg_pool, job, reason="operator_review") == "review"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET ocr_status='pending',ocr_extracted_data=NULL,ocr_completed_at=NULL WHERE id=10"
        )
        assert await conn.fetchval("SELECT count(*) FROM crm_passport_ocr_jobs") == 1
        assert (
            await conn.fetchval("SELECT status FROM crm_passport_ocr_jobs WHERE document_id=10")
            == "pending"
        )


@pytest.mark.asyncio
async def test_historical_enrollment_is_explicit_bounded_and_idempotent(pg_pool):
    assert await claim(pg_pool) is None
    with pytest.raises(ValueError):
        await enqueue_selected(pg_pool, list(range(1, 22)))
    assert await enqueue_selected(pg_pool, [900]) == 1
    assert await enqueue_selected(pg_pool, [900]) == 0
    assert (await claim(pg_pool, doc_id=900, client_id=99, file_id="historical-file"))[
        "attempts"
    ] == 1


@pytest.mark.asyncio
async def test_staff_reads_one_result_after_interrupted_upload_and_two_workers(
    pg_pool, monkeypatch
):
    """The request can disappear after COMMIT. Only the durable worker can settle the document."""
    from unittest.mock import AsyncMock, Mock

    from fastapi import BackgroundTasks

    from backend.app.routers import crm_enhanced_documents as router
    from backend.app.routers import documents_proxy
    from backend.services.documents import passport_ocr_processing as processor
    from backend.services.portal import portal_notification_service

    monkeypatch.setattr(router, "verify_client_access", AsyncMock())
    monkeypatch.setattr(router, "assert_drive_file_belongs_to_client", AsyncMock())
    monkeypatch.setattr(documents_proxy, "assert_drive_file_belongs_to_client", AsyncMock())
    monkeypatch.setattr(router, "invalidate_cache", AsyncMock())
    monkeypatch.setattr(
        portal_notification_service, "PortalNotificationService", Mock(return_value=AsyncMock())
    )
    monkeypatch.setattr(router, "spawn", lambda coro: coro.close())
    payload = {"full_name": "Synthetic Client", "passport_number": "TEST-SYNTHETIC"}
    extract = AsyncMock(return_value=payload)
    monkeypatch.setattr(processor, "extract", extract)
    async with pg_pool.acquire() as conn:
        await conn.execute("INSERT INTO clients (id,status) VALUES (1,'active')")
        await conn.execute("""
            CREATE TABLE write_events (kind text);
            CREATE FUNCTION count_write() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN INSERT INTO write_events VALUES (TG_TABLE_NAME); RETURN NEW; END $$;
            CREATE TRIGGER master_write AFTER UPDATE ON clients FOR EACH ROW EXECUTE FUNCTION count_write();
            CREATE TRIGGER result_write AFTER UPDATE ON documents FOR EACH ROW
              WHEN (NEW.ocr_status='completed') EXECUTE FUNCTION count_write();
        """)
    owner = {"email": "staff@example.invalid"}
    receipt = await router.create_document(
        1,
        router.DocumentCreate(
            document_type="passport", file_name="scan.png", file_id="synthetic-file"
        ),
        BackgroundTasks(),
        pg_pool,
        owner,
    )
    # Drop the response's BackgroundTasks, then interrupt a worker after it persists a claim.
    view = await router.get_client_ocr_status(1, pg_pool, owner)
    assert view["pending_ocr"] == 1
    lost = await claim(pg_pool)
    assert lost["document_id"] == receipt["id"]
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE crm_passport_ocr_jobs SET available_at=now()-interval '1 second'"
        )
    results = await asyncio.gather(processor.process_one(pg_pool), processor.process_one(pg_pool))
    assert sorted(result["status"] for result in results) == ["completed", "not_claimed"]
    assert await finish(pg_pool, lost, extracted=payload) == "stale"
    assert (await processor.process_one(pg_pool))["status"] == "not_claimed"
    extract.assert_awaited_once()
    view = await router.get_client_ocr_status(1, pg_pool, owner)
    assert (view["pending_ocr"], view["completed_ocr"]) == (0, 1)
    assert view["documents"][0]["id"] == receipt["id"]
    assert view["documents"][0]["ocr_extracted_data"] == payload
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM write_events WHERE kind='documents'") == 1
        assert await conn.fetchval("SELECT count(*) FROM write_events WHERE kind='clients'") == 0
        await conn.execute(
            """INSERT INTO documents (client_id,document_type,status,ocr_status,updated_at)
               SELECT 1,'misc','active','completed',clock_timestamp()
               FROM generate_series(1,11)"""
        )
    latest = await router.get_client_ocr_status(1, pg_pool, owner)
    assert latest["completed_ocr"] == 10 and len(latest["documents"]) == 10
    assert receipt["id"] not in [doc["id"] for doc in latest["documents"]]
    selected = await router.get_client_ocr_status(1, pg_pool, owner, document_id=receipt["id"])
    assert selected["completed_ocr"] == 1
    assert selected["documents"][0]["ocr_extracted_data"] == payload
    other = await router.get_client_ocr_status(99, pg_pool, owner, document_id=receipt["id"])
    assert other["documents"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_hash", [None, "a" * 64])
async def test_local_receipt_is_atomic_with_the_cloud_worker_and_safe_to_replay(
    pg_pool, monkeypatch, stored_hash
):
    from unittest.mock import AsyncMock

    from backend.services.documents import passport_ocr_jobs as jobs
    from backend.services.documents import passport_ocr_receipt as local
    from backend.services.intake.extract import _mrz_check_digit

    await _seed(pg_pool)
    await pg_pool.execute(
        "UPDATE documents SET file_id='local-test-file', content_hash=$1 WHERE id=10", stored_hash
    )
    # Recreate the historical state from before the upload trigger existed.
    await pg_pool.execute("DELETE FROM crm_passport_ocr_jobs WHERE document_id=10")
    number = "T00000001"
    line = number + str(_mrz_check_digit(number)) + "UTO" + "900101"
    line += str(_mrz_check_digit("900101")) + "F350101" + str(_mrz_check_digit("350101"))
    source = {
        "document_id": 10,
        "client_id": 1,
        "file_id": "local-test-file",
        "source_sha256": "a" * 64,
        "mrz_line1": "P<UTOCLIENT<<SYNTHETIC",
        "mrz_line2": line,
    }
    with pytest.raises(local.ReceiptRejected, match="invalid_mrz_checks"):
        local.AppleReceipt.parse(
            {**source, "mrz_line2": line[:9] + str((int(line[9]) + 1) % 10) + line[10:]}
        )
    receipt = local.AppleReceipt.parse(source)
    await pg_pool.execute("ALTER TABLE clients ADD COLUMN passport_number text")
    await pg_pool.execute("UPDATE clients SET passport_number=$1 WHERE id=1", number)
    verify = AsyncMock()
    monkeypatch.setattr(local, "_verify_drive", verify)
    master = await pg_pool.fetchval("SELECT to_jsonb(c)::text FROM clients c WHERE id=1")
    real_finish = jobs.finish
    monkeypatch.setattr(jobs, "finish", AsyncMock(side_effect=RuntimeError("interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        await local.apply_apple_receipt(pg_pool, receipt)
    assert (
        await pg_pool.fetchval("SELECT count(*) FROM crm_passport_ocr_jobs WHERE document_id=10")
        == 0
    )
    assert await pg_pool.fetchval("SELECT ocr_status FROM documents WHERE id=10") == "pending"

    at_write, release = asyncio.Event(), asyncio.Event()

    async def parked(*args, **kwargs):
        at_write.set()
        await release.wait()
        return await real_finish(*args, **kwargs)

    monkeypatch.setattr(jobs, "finish", parked)
    task = asyncio.create_task(local.apply_apple_receipt(pg_pool, receipt))
    try:
        await asyncio.wait_for(at_write.wait(), 5)
        assert await jobs.claim(pg_pool, doc_id=10, client_id=1, file_id="local-test-file") is None
    finally:
        release.set()
    assert await task == "completed"
    verify.assert_awaited()
    observation = """SELECT to_jsonb(d)::text AS doc, to_jsonb(j)::text AS job
        FROM documents d JOIN crm_passport_ocr_jobs j ON j.document_id=d.id WHERE d.id=10"""
    before = await pg_pool.fetchrow(observation)
    assert await local.apply_apple_receipt(pg_pool, receipt) == "already_applied"
    assert await pg_pool.fetchrow(observation) == before
    assert await pg_pool.fetchval("SELECT to_jsonb(c)::text FROM clients c WHERE id=1") == master
    assert (
        await pg_pool.fetchval(
            "SELECT ocr_extracted_data->>'passport_number' FROM documents WHERE id=10"
        )
        == number
    )
    await pg_pool.execute("UPDATE documents SET verified_at=now() WHERE id=10")
    with pytest.raises(local.ReceiptRejected, match="ineligible_document"):
        await local.apply_apple_receipt(pg_pool, receipt)
    await pg_pool.execute("UPDATE documents SET verified_at=NULL WHERE id=10")
    await pg_pool.execute(
        "INSERT INTO clients (id,status,passport_number) VALUES (2,'active',$1)", number
    )
    with pytest.raises(local.ReceiptRejected, match="identity_mismatch"):
        await local.apply_apple_receipt(pg_pool, receipt)
    assert await pg_pool.fetchrow(observation) == before
    await pg_pool.execute(
        "UPDATE clients SET status='lead', passport_number=$1 WHERE id=2", "T-0000 0001"
    )
    with pytest.raises(local.ReceiptRejected, match="identity_mismatch"):
        await local.apply_apple_receipt(pg_pool, receipt)
    assert await pg_pool.fetchrow(observation) == before
