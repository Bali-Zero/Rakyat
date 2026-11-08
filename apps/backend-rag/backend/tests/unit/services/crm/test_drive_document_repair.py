"""Actual repair SQL and pure writer on synthetic PostgreSQL TEMP tables."""

import copy
import hashlib
import json
import os
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import asyncpg
import pytest
import pytest_asyncio

from backend.services.crm import drive_document_repair as mod
from backend.services.intake.writer import write_client_document


class Drive:
    individual_root = "individuals-root"

    def __init__(self):
        self.metadata_rows = {
            "file000001A": {
                "id": "file000001A",
                "mimeType": "application/pdf",
                "trashed": False,
                "parents": ["source-parent"],
                "size": "13",
                "version": "7",
                "md5Checksum": hashlib.md5(b"synthetic-pdf").hexdigest(),
            },
            "client-root": {
                "id": "client-root",
                "mimeType": mod.FOLDER_MIME,
                "trashed": False,
                "parents": [self.individual_root],
                "version": "4",
            },
            "target-folder": {
                "id": "target-folder",
                "name": "01_Profile",
                "mimeType": mod.FOLDER_MIME,
                "trashed": False,
                "parents": ["client-root"],
                "version": "5",
            },
        }
        self.acls = {
            fid: [{"id": "permission-1", "type": "user", "role": "writer"}]
            for fid in self.metadata_rows
        }
        self.ids = ["target-folder"]

    async def metadata(self, fid):
        return copy.deepcopy(self.metadata_rows[fid])

    async def content(self, fid):
        return {
            "size": 13,
            "sha256": hashlib.sha256(b"synthetic-pdf").hexdigest(),
            "md5": hashlib.md5(b"synthetic-pdf").hexdigest(),
        }

    async def permissions(self, fid):
        return copy.deepcopy(self.acls[fid])

    async def target_ids(self, *args):
        return self.ids


@pytest.fixture
def manifest():
    return {
        "schema_version": 1,
        "records": [
            {
                "client_id": 1,
                "file_id": "file000001A",
                "root_id": "client-root",
                "source_parent_id": "source-parent",
                "target_folder_id": "target-folder",
                "document_type": "passport",
                "document_category": "personal",
                "source_md5": hashlib.md5(b"synthetic-pdf").hexdigest(),
                "source_size": 13,
                "source_version": "7",
                "passport_sha256": mod.identity_hash("TEST-123", "number"),
                "source_content_sha256": hashlib.sha256(b"synthetic-pdf").hexdigest(),
                "name_sha256": mod.identity_hash("Synthetic Person", "name"),
                "dob_sha256": mod.identity_hash("1990-01-02", "dob"),
            }
        ],
    }


@pytest_asyncio.fixture
async def conn():
    dsn = os.environ["TEST_DATABASE_URL"]
    target = urlsplit(dsn)
    if target.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in target.path:
        pytest.skip("requires local test PostgreSQL")
    db = await asyncpg.connect(dsn, timeout=3)
    try:
        await db.execute("SET search_path TO pg_temp")
        await db.execute("""
            CREATE TEMP TABLE clients(id int, passport_number text, kitas_number text, full_name text,
                date_of_birth date, client_type text, status text, deleted_at timestamptz,
                google_drive_folder_id text, drive_folder_id text);
            INSERT INTO clients VALUES(1,'TEST-123','PERMIT-1','Synthetic Person','1990-01-02',
                'individual','prospect',NULL,'client-root',NULL);
            CREATE TEMP TABLE companies(id int,google_drive_folder_id text,tax_dept_folder_id text);
            CREATE TEMP TABLE client_drive_subfolders(client_id int,subfolder_id text);
            CREATE TEMP TABLE documents(id serial primary key,client_id int,document_type text,
                document_category text,file_name text,file_id text,file_url text,google_drive_file_url text,
                notes text,family_member_id int,practice_id int,status text,storage_type text,ocr_status text,
                intake_idempotency_key text,intake_proposal_id bigint,client_visible bool default true,
                deleted_at timestamptz,is_archived bool default false,updated_at timestamptz default now());
            CREATE UNIQUE INDEX ON documents(client_id,intake_idempotency_key)
                WHERE intake_idempotency_key IS NOT NULL;
            CREATE TEMP TABLE company_documents(google_drive_file_id text,google_drive_file_url text);
            CREATE TEMP TABLE invoices(drive_file_id text);
            CREATE TEMP TABLE practices(documents jsonb);
            CREATE TEMP TABLE lkpm_receipts(file_drive_id text);
            CREATE TEMP TABLE weekly_cashout(receipt_drive_file_id text);
        """)
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dry_run_is_read_only_then_hidden_apply_and_exact_retry(conn, manifest):
    drive = Drive()
    for method in ("metadata", "permissions", "content", "target_ids"):
        original = getattr(drive, method)

        async def outside_transaction(*args, _original=original):
            assert not conn.is_in_transaction(), "Drive I/O must precede the short write transaction"
            return await _original(*args)

        setattr(drive, method, outside_transaction)
    plan = await mod.run_repair(conn, drive, manifest)
    assert plan["writes"] == await conn.fetchval("SELECT count(*) FROM documents") == 0
    result = await mod.run_repair(
        conn, drive, manifest, apply=True, expected_plan_sha256=plan["plan_sha256"]
    )
    assert result["move_status"] == "pending"
    row = dict(await conn.fetchrow("SELECT * FROM documents"))
    assert row["file_id"] == "file000001A" and row["client_visible"] is False
    assert row["ocr_status"] == "skipped" and row["intake_proposal_id"] is None
    assert json.loads(row["notes"])["source"] == mod.SOURCE
    assert json.loads(row["notes"])["move_status"] == "pending"
    await conn.execute("UPDATE documents SET client_visible=true,is_archived=true,deleted_at=now()")
    before = dict(await conn.fetchrow("SELECT * FROM documents"))
    repeat = await mod.run_repair(
        conn, drive, manifest, apply=True, expected_plan_sha256=plan["plan_sha256"]
    )
    assert repeat["records"][0]["reused"] is True
    assert dict(await conn.fetchrow("SELECT * FROM documents")) == before
    assert drive.metadata_rows["file000001A"]["parents"] == ["source-parent"]


@pytest.mark.asyncio
async def test_stale_digest_writes_nothing(conn, manifest):
    with pytest.raises(mod.RepairRejected, match="PLAN_STALE"):
        await mod.run_repair(conn, Drive(), manifest, apply=True, expected_plan_sha256="0" * 64)
    assert await conn.fetchval("SELECT count(*) FROM documents") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "identity",
        "duplicate_identity",
        "archived",
        "root",
        "shared_root",
        "foreign_doc",
        "own_nonrepair",
        "billing",
        "practice",
        "company",
    ],
)
async def test_database_drift_and_references_fail_closed(conn, manifest, mutation):
    statements = {
        "identity": "UPDATE clients SET full_name='Different Synthetic Person'",
        "duplicate_identity": "INSERT INTO clients SELECT 2,passport_number,kitas_number,full_name,date_of_birth,client_type,status,NULL,'another-root',NULL FROM clients",
        "archived": "UPDATE clients SET status='archived'",
        "root": "UPDATE clients SET google_drive_folder_id='another-root'",
        "shared_root": "INSERT INTO clients(id,status,google_drive_folder_id) VALUES(2,'archived','client-root')",
        "foreign_doc": "INSERT INTO documents(client_id,file_id) VALUES(2,'file000001A')",
        "own_nonrepair": "INSERT INTO documents(client_id,file_id) VALUES(1,'file000001A')",
        "billing": "INSERT INTO invoices VALUES('file000001A')",
        "practice": 'INSERT INTO practices VALUES(\'{"invoice":{"invoice_drive_id":"file000001A"}}\')',
        "company": "INSERT INTO company_documents VALUES('file000001A',NULL)",
    }
    plan = await mod.run_repair(conn, Drive(), manifest)
    await conn.execute(statements[mutation])
    count = await conn.fetchval("SELECT count(*) FROM documents")
    with pytest.raises(mod.RepairRejected):
        await mod.run_repair(
            conn, Drive(), manifest, apply=True, expected_plan_sha256=plan["plan_sha256"]
        )
    assert await conn.fetchval("SELECT count(*) FROM documents") == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "md5",
        "size",
        "version",
        "parent",
        "trashed",
        "shortcut",
        "target",
        "acl",
        "both_acl",
        "duplicate_target",
    ],
)
async def test_drive_drift_is_rejected_without_write(conn, manifest, mutation):
    drive = Drive()
    plan = await mod.run_repair(conn, drive, manifest)
    source = drive.metadata_rows["file000001A"]
    if mutation in {"md5", "size", "version", "parent", "trashed", "shortcut"}:
        key, value = {
            "md5": ("md5Checksum", "b" * 32),
            "size": ("size", "999"),
            "version": ("version", "8"),
            "parent": ("parents", ["another-parent"]),
            "trashed": ("trashed", True),
            "shortcut": ("shortcutDetails", {"targetId": "another-file"}),
        }[mutation]
        source[key] = value
    elif mutation == "target":
        drive.metadata_rows["target-folder"]["parents"] = ["another-root"]
    elif mutation == "duplicate_target":
        drive.ids.append("another-target")
    else:
        drive.acls["target-folder"][0]["role"] = "reader"
        if mutation == "both_acl":
            drive.acls["file000001A"][0]["role"] = "reader"
    with pytest.raises(mod.RepairRejected):
        await mod.run_repair(
            conn, drive, manifest, apply=True, expected_plan_sha256=plan["plan_sha256"]
        )
    assert await conn.fetchval("SELECT count(*) FROM documents") == 0


@pytest.mark.asyncio
async def test_rechecks_state_inside_transaction_before_insert(conn, manifest, monkeypatch):
    drive = Drive()
    plan = await mod.run_repair(conn, drive, manifest)
    original = mod.inspect_record
    calls = 0

    async def inspect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if kwargs.get("snapshot") is not None:
            await conn.execute("UPDATE clients SET status='archived'")
        return await original(*args, **kwargs)

    monkeypatch.setattr(mod, "inspect_record", inspect)
    with pytest.raises(mod.RepairRejected):
        await mod.run_repair(
            conn, drive, manifest, apply=True, expected_plan_sha256=plan["plan_sha256"]
        )
    assert await conn.fetchval("SELECT count(*) FROM documents") == 0


@pytest.mark.asyncio
async def test_writer_rejects_missing_real_provenance_before_db_access():
    conn = AsyncMock()
    with pytest.raises(ValueError, match="REPAIR_PROVENANCE_REQUIRED"):
        await write_client_document(conn, 1, {}, proposal_id=None, idempotency_key="test")
    conn.fetchval.assert_not_awaited()


def test_manifest_rejects_pii_extra_fields_and_unbounded_batch(manifest):
    manifest["records"][0]["full_name"] = "Forbidden field"
    with pytest.raises(mod.RepairRejected, match="MANIFEST_FIELDS"):
        mod.validate_manifest(manifest)
    del manifest["records"][0]["full_name"]
    manifest["records"] *= 21
    with pytest.raises(mod.RepairRejected, match="MANIFEST_COUNT"):
        mod.validate_manifest(manifest)


def test_identity_normalization_is_ordered_and_rejects_empty():
    assert mod.identity_hash("Synthetíc Person", "name") == mod.identity_hash(
        "SYNTHETIC PERSON", "name"
    )
    assert mod.identity_hash("Person Synthetic", "name") != mod.identity_hash(
        "Synthetic Person", "name"
    )
    assert mod.identity_hash("te-st 123", "number") == mod.identity_hash("TEST123", "number")
    for value, kind in [("---", "number"), (" ", "name"), ("2000-99-99", "dob")]:
        with pytest.raises(mod.RepairRejected):
            mod.identity_hash(value, kind)


@pytest.mark.asyncio
async def test_itap_uses_passport_reference_without_inventing_permit_number(conn, manifest):
    manifest["records"][0]["document_type"] = "itap"
    manifest["records"][0]["document_category"] = "immigration"
    drive = Drive()
    drive.metadata_rows["target-folder"]["name"] = "02_Immigration"
    await conn.execute("UPDATE clients SET kitas_number=NULL")
    plan = await mod.run_repair(conn, drive, manifest)
    await mod.run_repair(
        conn, drive, manifest, apply=True, expected_plan_sha256=plan["plan_sha256"]
    )
    assert await conn.fetchval("SELECT document_type FROM documents") == "itap"


@pytest.mark.asyncio
async def test_existing_intake_defaults_unchanged(conn):
    doc_id = await write_client_document(
        conn, 1, {"document_type": "passport"}, proposal_id=123, idempotency_key="intake:test"
    )
    row = await conn.fetchrow("SELECT * FROM documents WHERE id=$1", doc_id)
    assert row["ocr_status"] == "completed" and row["client_visible"] is True
    assert row["intake_proposal_id"] == 123


@pytest.mark.asyncio
async def test_declared_content_hash_must_match_drive_hash(conn, manifest):
    manifest["records"][0]["source_content_sha256"] = "0" * 64
    with pytest.raises(mod.RepairRejected, match="CONTENT_MISMATCH"):
        await mod.run_repair(conn, Drive(), manifest)
    assert await conn.fetchval("SELECT count(*) FROM documents") == 0


@pytest.mark.asyncio
async def test_stream_hashing_is_bounded_and_measures_actual_bytes(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    class Response:
        status_code = 200

        async def aiter_raw(self, chunk_size):
            assert chunk_size == 65536
            for chunk in [b"synthetic", b"-pdf"]:
                yield chunk

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            yield Response()

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: Client())
    reader = object.__new__(mod.ReadOnlyDrive)
    reader.drive = SimpleNamespace(credentials=SimpleNamespace(valid=True, token="synthetic"))
    hashed = await reader.content("file000001A")
    assert hashed == {
        "size": 13,
        "sha256": hashlib.sha256(b"synthetic-pdf").hexdigest(),
        "md5": hashlib.md5(b"synthetic-pdf").hexdigest(),
    }
    monkeypatch.setattr(mod, "MAX_BYTES", 10)
    with pytest.raises(mod.RepairRejected, match="CONTENT_SIZE_LIMIT"):
        await reader.content("file000001A")


def test_cli_defaults_to_plan_and_errors_are_safe(monkeypatch, capsys):
    from backend.scripts import crm_drive_import as cli

    monkeypatch.setattr(cli.logging, "disable", lambda _level: None)
    captured = []

    async def run(args):
        captured.append(args)
        return {"mode": "plan", "writes": 0}

    monkeypatch.setattr(cli, "execute", run)
    assert cli.main(["--manifest", "synthetic-manifest.json"]) == 0
    assert captured[0].apply is False

    async def fail(args):
        raise RuntimeError("PRIVATE_PROVIDER_PAYLOAD")

    monkeypatch.setattr(cli, "execute", fail)
    assert cli.main(["--manifest", "synthetic-manifest.json"]) == 2
    assert "PRIVATE_PROVIDER_PAYLOAD" not in capsys.readouterr().out
