"""Tests for Tube 1: company_documents writes, UUID boundaries, Drive folder, and standalone CRM push."""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException

_APP_ROOT = Path(__file__).resolve().parents[3]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from backend.app.routers import crm_enhanced_documents as docs_router  # noqa: E402
from backend.app.routers import intake_review  # noqa: E402
from backend.app.setup import sentry_config  # noqa: E402
from backend.services.intake import crm_delivery, crm_push, routing, writer  # noqa: E402
from backend.services.integrations.service_account_drive_service import (  # noqa: E402
    ServiceAccountDriveService,
)

DSN = os.environ.get("INTAKE_TEST_DSN", "postgresql://nuzantara@localhost:5432/nuzantara_test")


async def _dsn_reachable() -> bool:
    try:
        conn = await asyncpg.connect(DSN)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    if not await _dsn_reachable():
        pytest.skip(f"local intake DB not reachable at {DSN}")
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield pool
    await pool.close()


_DEV_COLS_25 = [
    {"column_name": name}
    for name in (
        "id",
        "uuid",
        "company_id",
        "document_type",
        "document_subtype",
        "document_number",
        "document_title",
        "description",
        "issue_date",
        "expiry_date",
        "reminder_date",
        "storage_type",
        "google_drive_file_id",
        "google_drive_file_url",
        "file_name",
        "file_size_kb",
        "mime_type",
        "is_verified",
        "verified_by",
        "verified_at",
        "status",
        "notes",
        "created_at",
        "updated_at",
        "uploaded_by",
    )
]

_PROD_COLS_38 = _DEV_COLS_25 + [
    {"column_name": name}
    for name in (
        "ocr_status",
        "ocr_extracted_data",
        "ocr_completed_at",
        "client_visible",
        "deleted_at",
        "is_archived",
        "drive_folder",
        "period",
        "fiscal_year",
        "fiscal_period",
        "content_hash",
        "intake_idempotency_key",
        "intake_proposal_id",
    )
]


class _FakeTx:
    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _CompanyWriterConn:
    """Minimal asyncpg-like connection tracking SQL statements for Tube 1 writer tests."""

    def __init__(self, *, prod_schema: bool) -> None:
        self.cols = _PROD_COLS_38 if prod_schema else _DEV_COLS_25
        self.prod_schema = prod_schema
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.company_docs: dict[int, dict[str, Any]] = {}
        self.audits: list[dict[str, Any]] = []
        self.proposals: dict[int, dict[str, Any]] = {
            501: {"id": 501, "status": "review_claimed", "decision": "NEEDS_REVIEW"}
        }
        self._next_doc_id = 700
        self._next_audit_id = 900
        self._next_correction_id = 100

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((sql, args))
        if "information_schema.columns" in sql and "company_documents" in sql:
            return list(self.cols)
        if "information_schema.columns" in sql and "intake_commit_audit" in sql:
            return [{"column_name": "company_doc_id"}]
        if "FROM pg_index i" in sql:
            if self.prod_schema:
                return [
                    {
                        "pred": "(intake_idempotency_key IS NOT NULL)",
                        "cols": ["company_id", "intake_idempotency_key"],
                    }
                ]
            return []
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.queries.append((sql, args))
        if "FROM companies WHERE id = $1" in sql:
            cid = int(args[0])
            if cid == 42:
                return {"id": 42, "status": "active", "company_name": "Contoh Entity"}
            return None
        if "FROM intake_queue WHERE id = $1" in sql:
            qid = int(args[0])
            if qid == 8801:
                return {
                    "id": 8801,
                    "source": "whatsapp",
                    "source_ref": "msg_comp_1",
                    "blob_hash": "a" * 64,
                    "pipeline_version": "v2.4-ocr",
                    "stage_output": {
                        "file_name": "NIB_Contoh.pdf",
                        "document_type": "nib",
                        "extracted_fields": {},
                    },
                }
            return None
        if "FROM intake_commit_audit" in sql:
            key = args[0]
            cid = int(args[1])
            for row in reversed(self.audits):
                if (
                    row["idempotency_key"] == key
                    and row["outcome"] == "committed"
                    and row["plan"].get("company_id") == cid
                ):
                    return {
                        "client_id": row["client_id"],
                        "proposal_id": row["proposal_id"],
                        "plan": dict(row["plan"]),
                    }
            return None
        if "FROM document_routing_proposal" in sql:
            pid = int(args[0])
            return self.proposals.get(pid)
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.queries.append((sql, args))
        if "FROM pg_indexes" in sql and "company_documents" in sql:
            return 1 if self.prod_schema else None
        if "SELECT intake_proposal_id FROM company_documents WHERE id = $1" in sql:
            doc = self.company_docs.get(int(args[0]))
            return doc.get("intake_proposal_id") if doc else None
        if "SELECT id FROM company_documents" in sql:
            cid = int(args[0])
            key = str(args[1])
            for did, row in self.company_docs.items():
                if row["company_id"] != cid:
                    continue
                if self.prod_schema:
                    if row.get("intake_idempotency_key") == key:
                        return did
                else:
                    if key in str(row.get("notes") or ""):
                        return did
            return None
        if "INSERT INTO company_documents" in sql:
            self._next_doc_id += 1
            did = self._next_doc_id
            col_part = sql.split("INSERT INTO company_documents (", 1)[1].split(")", 1)[0]
            col_names = [c.strip() for c in col_part.split(",")]
            row = dict(zip(col_names, args, strict=False))
            row["id"] = did
            self.company_docs[did] = row
            return did
        if "INSERT INTO intake_corrections" in sql:
            self._next_correction_id += 1
            return self._next_correction_id
        if "INSERT INTO intake_commit_audit" in sql:
            self._next_audit_id += 1
            aid = self._next_audit_id
            plan_raw = args[10]
            plan_dict = json.loads(plan_raw) if isinstance(plan_raw, str) else dict(plan_raw)
            self.audits.append(
                {
                    "id": aid,
                    "proposal_id": args[0],
                    "queue_id": args[1],
                    "client_id": args[2],
                    "doc_id": args[3],
                    "practice_id": args[4],
                    "decision": args[5],
                    "committed_by": args[6],
                    "dry_run": args[7],
                    "outcome": args[8],
                    "idempotency_key": args[9],
                    "plan": plan_dict,
                    "company_doc_id": args[12] if len(args) > 12 else None,
                }
            )
            return aid
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.queries.append((sql, args))
        if sql.startswith("DELETE FROM company_documents"):
            self.company_docs.pop(int(args[0]), None)
        elif "UPDATE document_routing_proposal" in sql and "CASE status" in sql:
            pid = int(args[0])
            cur = self.proposals.get(pid)
            if cur and cur["status"] in ("routed", "auto_routed"):
                cur["status"] = "review_claimed" if cur["status"] == "routed" else "review_pending"
        elif "UPDATE document_routing_proposal" in sql and len(args) >= 3:
            pid = int(args[0])
            from_status = str(args[1])
            target_status = str(args[2])
            cur = self.proposals.get(pid)
            if cur and cur["status"] == from_status:
                cur["status"] = target_status
        return "OK"


class _FakePool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


class _FakeAcquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _standalone_company_proposal() -> dict[str, Any]:
    return {
        "id": 501,
        "proposal_id": 501,
        "queue_id": 8801,
        "doc_index": 0,
        "status": "review_claimed",
        "pipeline_version": "v2.4-ocr",
        "entity_resolution": {"decision": "NEEDS_REVIEW"},
        "routing": {
            "client_id": None,
            "company_id": None,
            "practice_id": None,
            "doc_type": "nib",
            "payload": {
                "file_name": "NIB_Contoh.pdf",
                "file_id": "drive-file-101",
                "file_url": "https://drive.example/file-101",
                "document_type": "nib",
                "document_category": "licensing",
                "notes": "extracted nib",
                "client_visible": True,
            },
        },
        "source_snapshot": {"wa_msg_id": "msg_comp_1"},
    }


@pytest.mark.asyncio
async def test_write_company_document_38col_vs_25col_schema() -> None:
    prod_conn = _CompanyWriterConn(prod_schema=True)
    prod_id = await writer.write_company_document(
        prod_conn,  # type: ignore[arg-type]
        42,
        {
            "document_type": "nib",
            "document_category": "licensing",
            "document_number": "DOC-NUM-A",
            "file_name": "NIB.pdf",
            "file_id": "gd-1",
            "file_url": "https://drive.example/1",
            "notes": "verified",
            "client_visible": True,
        },
        proposal_id=501,
        idempotency_key="idem-prod-1",
        committed_by="tester",
    )
    assert prod_id == 701
    prod_inserts = [q for q, _ in prod_conn.queries if "INSERT INTO company_documents" in q]
    assert len(prod_inserts) == 1
    assert "ON CONFLICT (company_id, intake_idempotency_key)" in prod_inserts[0]
    assert prod_conn.company_docs[prod_id]["intake_idempotency_key"] == "idem-prod-1"
    assert prod_conn.company_docs[prod_id]["client_visible"] is True

    dev_conn = _CompanyWriterConn(prod_schema=False)
    dev_id = await writer.write_company_document(
        dev_conn,  # type: ignore[arg-type]
        42,
        {
            "document_type": "nib",
            "document_category": "licensing",
            "file_name": "NIB.pdf",
            "file_id": "gd-2",
            "file_url": "https://drive.example/2",
            "notes": "verified",
        },
        proposal_id=501,
        idempotency_key="idem-dev-1",
        committed_by="tester",
    )
    assert dev_id == 701
    dev_inserts = [q for q, _ in dev_conn.queries if "INSERT INTO company_documents" in q]
    assert len(dev_inserts) == 1
    assert "ON CONFLICT" not in dev_inserts[0]
    assert "[intake_key:idem-dev-1]" in str(dev_conn.company_docs[dev_id]["notes"])


@pytest.mark.asyncio
async def test_execute_and_rollback_standalone_company_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "true")
    conn = _CompanyWriterConn(prod_schema=True)
    proposal = _standalone_company_proposal()

    plan = await writer.plan_commit(
        proposal,
        conn,  # type: ignore[arg-type]
        committed_by="tester",
        override_company_id=42,
        company_explicit=True,
    )
    assert plan.client_id is None
    assert plan.company_id == 42

    res = await writer.execute_commit(plan, conn, dry_run=False)  # type: ignore[arg-type]
    assert res.outcome == "committed"
    assert res.doc_id is None
    assert res.company_doc_id == 701
    assert len(conn.company_docs) == 1
    assert conn.proposals[501]["status"] == "routed"

    rb = await writer.rollback_commit(
        conn,  # type: ignore[arg-type]
        client_id=None,
        company_id=42,
        idempotency_key=plan.idempotency_key,
        committed_by="tester",
    )
    assert rb.outcome == "rolled_back"
    assert rb.company_doc_id == 701
    assert len(conn.company_docs) == 0
    assert conn.proposals[501]["status"] == "review_claimed"


@pytest.mark.asyncio
async def test_crm_push_company_uuid_boundary_and_standalone_delivery(tmp_path: Path) -> None:
    captured_urls: list[str] = []
    captured_payloads: list[dict[str, Any]] = []
    comp_uuid = str(uuid.uuid4())
    hdr_stub = "stub-hdr"
    blob = tmp_path / "fake.pdf"
    blob.write_bytes(b"file")

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        captured_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "success": True,
                "company_document_id": 880,
                "file_url": "https://drive.example/comp-doc",
            },
        )

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        with patch("backend.services.intake.crm_push._get_client", return_value=mock_http):
            bad = await crm_push.push_committed_company_document(
                bearer_token=hdr_stub,
                crm_write_key=hdr_stub,
                company_uuid="not-a-uuid",
                file_name="nib.pdf",
                document_type="nib",
                blob_path=str(blob),
                base_url="https://fly.example",
            )
            assert bad.ok is False
            assert bad.status == "company_identity_unresolved"

            ok = await crm_push.push_committed_company_document(
                bearer_token=None,
                crm_write_key=hdr_stub,
                company_id=42,
                company_uuid=comp_uuid,
                file_name="nib.pdf",
                document_type="nib",
                document_category="licensing",
                blob_path=str(blob),
                client_visible=False,
                intake_idempotency_key="idem-standalone-1",
                base_url="https://fly.example",
            )
    finally:
        await mock_http.aclose()

    assert ok.ok is True
    assert ok.status == "pushed"
    assert ok.fly_company_doc_id == 880
    assert captured_urls == [f"https://fly.example/api/crm/internal/companies/{comp_uuid}/documents/upload"]
    assert captured_payloads[0]["client_visible"] is False
    assert captured_payloads[0]["intake_idempotency_key"] == "idem-standalone-1"


@pytest.mark.asyncio
async def test_standalone_company_internal_upload_endpoint_and_uuid_guard() -> None:
    comp_uuid = str(uuid.uuid4())
    payload = docs_router.CompanyDocumentUploadBase64(
        file="ZmlsZQ==",
        file_name="NIB_Contoh.pdf",
        document_type="nib",
        document_category="licensing",
        client_visible=True,
        intake_idempotency_key="idem-fly-1",
    )

    with pytest.raises(HTTPException) as bad_uuid:
        await docs_router.upload_company_document_base64_internal(
            company_uuid="not-a-uuid",
            data=payload,
            pool=MagicMock(),
            actor="svc-intake",
        )
    assert bad_uuid.value.status_code == 422

    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_FakeTx())
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": 42,
                "uuid": comp_uuid,
                "company_name": "Contoh Entity",
                "google_drive_folder_id": "folder-42",
                "status": "active",
            },
            None,
            {"id": 42, "status": "active"},
            None,
        ]
    )
    conn.fetch = AsyncMock(return_value=_PROD_COLS_38)
    conn.fetchval = AsyncMock(side_effect=[None, 912])
    pool = _FakePool(conn)

    mock_drive = MagicMock()
    mock_drive.resolve_canonical_folder = AsyncMock(return_value="sub-folder-42")
    mock_drive.upload_file_to_folder = AsyncMock(
        return_value={
            "id": "gd-comp-1",
            "webViewLink": "https://drive.example/gd-comp-1",
        }
    )

    with (
        patch(
            "backend.app.routers.crm_enhanced_documents.ServiceAccountDriveService",
            return_value=mock_drive,
        ),
        patch("backend.app.routers.crm_enhanced_documents.invalidate_cache", new=AsyncMock()),
    ):
        res = await docs_router.upload_company_document_base64_internal(
            company_uuid=comp_uuid,
            data=payload,
            pool=pool,
            actor="svc-intake",
        )

    assert res["success"] is True
    assert res["company_document_id"] == 912
    assert res["company_id"] == 42
    insert_calls = [
        c for c in conn.fetchval.await_args_list if "INSERT INTO company_documents" in str(c.args[0])
    ]
    assert len(insert_calls) == 1
    assert "client_visible" in insert_calls[0].args[0]
    assert "intake_idempotency_key" in insert_calls[0].args[0]


@pytest.mark.asyncio
async def test_ensure_company_folder_avoids_client_drive_subfolders() -> None:
    svc = ServiceAccountDriveService.__new__(ServiceAccountDriveService)
    svc._parent_folder_for_client_type = MagicMock(return_value="root-companies-folder")  # type: ignore[method-assign]
    svc.find_folder = AsyncMock(return_value=None)  # type: ignore[method-assign]
    svc.create_folder = AsyncMock(return_value={"id": "comp-root-folder"})  # type: ignore[method-assign]
    svc.resolve_canonical_folder = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda name, parent_id, conn=None: f"sub-{name}"
    )

    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_FakeTx())
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={"company_name": "Contoh Entity", "google_drive_folder_id": None}
    )

    res = await svc.ensure_company_folder(42, conn=conn, db_pool=MagicMock())

    assert res["root_folder_id"] == "comp-root-folder"
    sql_calls = " ".join(str(c.args[0]) for c in conn.execute.await_args_list)
    assert "client_drive_subfolders" not in sql_calls
    assert "UPDATE companies SET google_drive_folder_id = $1" in sql_calls


@pytest.mark.asyncio
async def test_routing_commit_gate_exposes_company_attach_eligible() -> None:
    conn_mock = AsyncMock(spec=asyncpg.Connection)
    with (
        patch(
            "backend.services.intake.routing.resolve_entity",
            new=AsyncMock(
                return_value={
                    "decision": routing.DECISION_AUTO_ATTACH,
                    "subject_kind": "company",
                    "candidates": [{"company_id": 42}],
                    "reason": {"reason": "nib_exact"},
                }
            ),
        ),
        patch(
            "backend.services.intake.routing._resolve_routing_target",
            new=AsyncMock(
                return_value={
                    "client_id": None,
                    "company_id": 42,
                    "practice_id": None,
                    "practice_hint": None,
                }
            ),
        ),
    ):
        prop = await routing.build_routing_proposal(8801, {}, "nib", _FakePool(conn_mock))  # type: ignore[arg-type]

    assert prop["commit_gate"]["decision"] == routing.DECISION_AUTO_ATTACH
    assert prop["commit_gate"]["auto_attach_eligible"] is False
    assert prop["commit_gate"]["company_attach_eligible"] is True


@pytest.mark.asyncio
async def test_upload_document_base64_rejects_numeric_company_id_when_phone_core_present() -> None:
    conn = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await docs_router._resolve_upload_company(
            conn,
            client_id=10,
            reference=42,
            expected_phone_core="SYNTH_CORE",
            lock=False,
        )
    assert exc.value.status_code == 409
    assert "company_identity_unresolved" in str(exc.value.detail)
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_committed_to_crm_updates_company_document_on_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTAKE_CRM_PUSH_ENABLED", "true")
    comp_uuid = str(uuid.uuid4())
    hdr_stub = "stub-hdr"
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_FakeTx())
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"blob_path": "/tmp/nib.pdf", "mime_type": "application/pdf"},
            {"google_drive_file_id": None, "google_drive_file_url": None},
        ]
    )
    conn.fetchval = AsyncMock(return_value=comp_uuid)
    pool = _FakePool(conn)
    plan = writer.CommitPlan(
        proposal_id=501,
        queue_id=8801,
        client_id=None,
        practice_id=None,
        decision="NEEDS_REVIEW",
        doc_type="nib",
        committed_by="tester",
        idempotency_key="idem-standalone-deliver",
        payload={
            "file_name": "NIB.pdf",
            "document_type": "nib",
            "document_category": "licensing",
        },
        company_id=42,
    )
    result = writer.CommitResult(
        proposal_id=501,
        dry_run=False,
        outcome="committed",
        doc_id=None,
        practice_id=None,
        audit_id=901,
        company_id=42,
        company_doc_id=701,
    )

    push_mock = AsyncMock(
        return_value=crm_push.CrmPushResult(
            ok=True,
            status="pushed",
            fly_company_doc_id=880,
            file_id="gd-880",
            file_url="https://drive.example/comp-delivered",
        )
    )
    with patch(
        "backend.services.intake.crm_push.push_committed_company_document",
        push_mock,
    ):
        delivery = await crm_delivery.deliver_committed_to_crm(
            pool=pool,
            queue_id=8801,
            plan=plan,
            result=result,
            crm_write_key=hdr_stub,
        )

    assert delivery["status"] == "pushed"
    assert delivery["fly_company_doc_id"] == 880
    update_calls = [
        c for c in conn.execute.await_args_list if "UPDATE company_documents" in str(c.args[0])
    ]
    assert len(update_calls) == 1
    assert "google_drive_file_url" in update_calls[0].args[0]


def test_extract_document_number_is_doc_type_aware() -> None:
    fields = {"npwp": "NPWP-FIELD-1", "nib": "NIB-FIELD-2", "passport_number": "PASS-FIELD-3"}
    assert writer._extract_document_number(fields, doc_type="nib") == "NIB-FIELD-2"
    assert writer._extract_document_number(fields, doc_type="passport") == "PASS-FIELD-3"
    assert writer._extract_document_number(fields, doc_type="npwp") == "NPWP-FIELD-1"


@pytest.mark.asyncio
async def test_plan_commit_client_reassignment_clears_stale_company_id() -> None:
    proposal = {
        "id": 502,
        "queue_id": 8801,
        "pipeline_version": "v2.4-ocr",
        "status": "review_claimed",
        "entity_resolution": {"decision": "LINK_CANDIDATE", "score": 0.9},
        "routing": {
            "client_id": 10,
            "company_id": 42,
            "practice_id": None,
            "doc_type": "passport",
            "fields": {},
        },
        "source_snapshot": {"wa_msg_id": "msg_reassign"},
    }
    conn = _CompanyWriterConn(prod_schema=True)
    orig_fetchrow = conn.fetchrow

    async def _fetchrow_with_client(sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM clients WHERE id = $1" in sql:
            return {"id": int(args[0]), "deleted_at": None}
        return await orig_fetchrow(sql, *args)

    conn.fetchrow = _fetchrow_with_client  # type: ignore[method-assign]
    plan = await writer.plan_commit(
        proposal,
        conn,  # type: ignore[arg-type]
        committed_by="tester",
        override_client_id=99,
        client_explicit=True,
    )
    assert plan.client_id == 99
    assert plan.company_id is None


def test_intake_review_parse_explicit_optional_int_rejects_malformed() -> None:
    assert intake_review._parse_explicit_optional_int({}, "company_id") == (False, None)
    assert intake_review._parse_explicit_optional_int({"company_id": None}, "company_id") == (
        True,
        None,
    )
    assert intake_review._parse_explicit_optional_int({"company_id": 42}, "company_id") == (
        True,
        42,
    )
    for bad in ("12a", 12.5, True):
        with pytest.raises(HTTPException) as exc:
            intake_review._parse_explicit_optional_int({"company_id": bad}, "company_id")
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_crm_delivery_passes_write_key_with_bearer_and_handles_postgres_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTAKE_CRM_PUSH_ENABLED", "true")
    monkeypatch.setenv("INTAKE_CRM_PUSH_WRITE_KEY", "env-key-stub")
    plan = writer.CommitPlan(
        proposal_id=503,
        queue_id=8801,
        client_id=None,
        practice_id=None,
        decision="NEEDS_REVIEW",
        doc_type="nib",
        committed_by="tester",
        idempotency_key="idem-err-1",
        payload={"file_name": "NIB.pdf", "document_type": "nib"},
        company_id=42,
    )
    result = writer.CommitResult(
        proposal_id=503,
        dry_run=False,
        outcome="committed",
        doc_id=None,
        practice_id=None,
        audit_id=902,
        company_id=42,
        company_doc_id=702,
    )

    err_conn = AsyncMock()
    err_conn.fetchrow = AsyncMock(side_effect=asyncpg.PostgresError("simulated pg failure"))
    err_pool = _FakePool(err_conn)
    err_out = await crm_delivery.deliver_committed_to_crm(
        pool=err_pool,
        queue_id=8801,
        plan=plan,
        result=result,
        bearer_token="bearer-stub",
    )
    assert err_out["status"] == "error"

    ok_conn = AsyncMock()
    ok_conn.transaction = MagicMock(return_value=_FakeTx())
    ok_conn.execute = AsyncMock()
    ok_conn.fetchrow = AsyncMock(
        side_effect=[
            {"blob_path": "/tmp/nib.pdf", "mime_type": "application/pdf"},
            {"google_drive_file_id": None, "google_drive_file_url": None},
        ]
    )
    ok_pool = _FakePool(ok_conn)
    resolver_mock = AsyncMock(
        return_value=crm_push.CrmPushResult(ok=False, status="denied_rbac", detail="stub")
    )
    with patch(
        "backend.services.intake.crm_delivery._resolve_and_push_locked",
        resolver_mock,
    ):
        await crm_delivery.deliver_committed_to_crm(
            pool=ok_pool,
            queue_id=8801,
            plan=plan,
            result=result,
            bearer_token="bearer-stub",
        )
    assert resolver_mock.await_args.kwargs["crm_write_key"] == "env-key-stub"


@pytest.mark.asyncio
async def test_crm_push_company_falls_back_to_internal_write_key_on_403(
    tmp_path: Path,
) -> None:
    comp_uuid = str(uuid.uuid4())
    blob = tmp_path / "nib.pdf"
    blob.write_bytes(b"pdf-bytes")
    seen_calls: list[tuple[str, str | None, str | None]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_calls.append(
            (
                str(request.url),
                request.headers.get("Authorization"),
                request.headers.get("X-CRM-Write-Key"),
            )
        )
        if "/api/crm/companies/" in str(request.url):
            return httpx.Response(403, text="forbidden for staff")
        return httpx.Response(
            200,
            json={"success": True, "company_document_id": 881, "file_url": "https://drive.example/881"},
        )

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        with patch("backend.services.intake.crm_push._get_client", return_value=mock_http):
            res = await crm_push.push_committed_company_document(
                bearer_token="staff-bearer-stub",
                crm_write_key="internal-key-stub",
                company_id=42,
                company_uuid=comp_uuid,
                file_name="nib.pdf",
                document_type="nib",
                blob_path=str(blob),
                base_url="https://fly.example",
            )
    finally:
        await mock_http.aclose()

    assert res.ok is True
    assert res.status == "pushed"
    assert res.fly_company_doc_id == 881
    assert len(seen_calls) == 2
    assert seen_calls[0][0].endswith(f"/api/crm/companies/{comp_uuid}/documents/upload")
    assert seen_calls[1][0].endswith(f"/api/crm/internal/companies/{comp_uuid}/documents/upload")
    assert seen_calls[1][2] == "internal-key-stub"


@pytest.mark.asyncio
async def test_linked_upload_retry_reuses_drive_file_and_shares_single_transaction() -> None:
    payload = docs_router.DocumentUploadBase64(
        file="ZmlsZQ==",
        file_name="nib.pdf",
        document_type="nib",
        document_category="pma",
        mime_type="application/pdf",
        company_id=10,
        intake_idempotency_key="idem-linked-retry-1",
    )
    conn = AsyncMock()
    tx_enter_count = 0
    tx_exit_exc: list[Any] = []

    class _CountingTx:
        async def __aenter__(self) -> _CountingTx:
            nonlocal tx_enter_count
            tx_enter_count += 1
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            tx_exit_exc.append(exc)
            return False

    conn.transaction = MagicMock(side_effect=lambda: _CountingTx())
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [{"column_name": "id"}, {"column_name": "client_id"}, {"column_name": "notes"}, {"column_name": "intake_idempotency_key"}, {"column_name": "client_visible"}],
            _PROD_COLS_38,
        ]
    )
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": 1, "full_name": "Test Person", "client_type": "individual", "google_drive_folder_id": "rf1", "phone": None, "phone_normalized": None},
            {"company_id": 10},
            {"id": 701, "file_id": "gd-existing", "file_url": "https://drive.example/gd-existing"},
            None,
            {"id": 701, "file_id": "gd-existing", "file_url": "https://drive.example/gd-existing"},
            None,
        ]
    )
    conn.fetchval = AsyncMock(side_effect=[None, 301])

    drive_service = MagicMock()
    drive_service.upload_file_to_folder = AsyncMock()

    with (
        patch("backend.app.routers.crm_enhanced_documents.verify_client_access", new=AsyncMock()),
        patch("backend.app.routers.crm_enhanced_documents.invalidate_cache", new=AsyncMock()),
        patch("backend.app.routers.crm_enhanced_documents.ServiceAccountDriveService", return_value=drive_service),
    ):
        res = await docs_router.upload_document_base64(
            client_id=1,
            data=payload,
            pool=_FakePool(conn),
            current_user={"id": "1", "email": "staff-user", "role": "admin"},
            background_tasks=BackgroundTasks(),
        )

    assert res["success"] is True
    assert res["document_id"] == 701
    assert res["company_document_id"] == 301
    drive_service.upload_file_to_folder.assert_not_awaited()
    assert tx_enter_count == 1

    # Verify TX aborts atomically when company_documents INSERT fails inside linked upload.
    conn_fail = AsyncMock()
    fail_tx_exc: list[Any] = []

    class _FailCheckTx:
        async def __aenter__(self) -> _FailCheckTx:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            fail_tx_exc.append(exc)
            return False

    conn_fail.transaction = MagicMock(return_value=_FailCheckTx())
    conn_fail.execute = AsyncMock()
    conn_fail.fetch = AsyncMock(
        side_effect=[
            [{"column_name": "id"}, {"column_name": "notes"}, {"column_name": "intake_idempotency_key"}],
            _PROD_COLS_38,
        ]
    )
    conn_fail.fetchrow = AsyncMock(
        side_effect=[
            {"id": 1, "full_name": "Test Person", "client_type": "individual", "google_drive_folder_id": "rf1", "phone": None, "phone_normalized": None},
            {"company_id": 10},
            None,
            None,
            None,
        ]
    )
    conn_fail.fetchval = AsyncMock(side_effect=[702, None, RuntimeError("company insert boom")])
    drive_ok = MagicMock()
    drive_ok.get_file_metadata = AsyncMock(return_value={"id": "rf1", "trashed": False, "parents": ["ind"]})
    drive_ok.resolve_canonical_folder = AsyncMock(return_value="sub1")
    drive_ok.upload_file_to_folder = AsyncMock(return_value={"id": "gd-new", "webViewLink": "https://drive.example/gd-new"})

    with (
        patch("backend.app.routers.crm_enhanced_documents.verify_client_access", new=AsyncMock()),
        patch("backend.app.routers.crm_enhanced_documents.invalidate_cache", new=AsyncMock()),
        patch("backend.app.routers.crm_enhanced_documents.ServiceAccountDriveService", return_value=drive_ok),
        patch("backend.app.routers.crm_enhanced_documents.physical_root_layout", return_value="individual"),
    ):
        with pytest.raises(HTTPException, match="company insert boom"):
            await docs_router.upload_document_base64(
                client_id=1,
                data=docs_router.DocumentUploadBase64(
                    file="ZmlsZQ==",
                    file_name="nib.pdf",
                    document_type="nib",
                    document_category="pma",
                    mime_type="application/pdf",
                    company_id=10,
                    intake_idempotency_key="idem-linked-fail-1",
                ),
                pool=_FakePool(conn_fail),
                current_user={"id": "1", "email": "staff-user", "role": "admin"},
                background_tasks=BackgroundTasks(),
            )
    assert isinstance(fail_tx_exc[0], RuntimeError)


@pytest.mark.asyncio
async def test_standalone_company_upload_25col_dedup_and_client_visible_fail_closed() -> None:
    comp_uuid = str(uuid.uuid4())
    # 1) Fail closed when client_visible=False on 25-col table missing client_visible.
    conn_fail = AsyncMock()
    conn_fail.fetchrow = AsyncMock(
        return_value={
            "id": 42,
            "uuid": comp_uuid,
            "company_name": "Contoh Entity",
            "google_drive_folder_id": "folder-42",
            "status": "active",
        }
    )
    conn_fail.fetch = AsyncMock(return_value=_DEV_COLS_25)
    with pytest.raises(HTTPException) as exc:
        await docs_router.upload_company_document_base64_internal(
            company_uuid=comp_uuid,
            data=docs_router.CompanyDocumentUploadBase64(
                file="ZmlsZQ==",
                file_name="nib.pdf",
                document_type="nib",
                client_visible=False,
                intake_idempotency_key="idem-25col-1",
            ),
            pool=_FakePool(conn_fail),
            actor="svc-intake",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "client_visible_column_missing"

    # 2) Pre-Drive [intake_key:...] probe short-circuits cleanly on 25-col table.
    conn_dedup = AsyncMock()
    conn_dedup.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": 42,
                "uuid": comp_uuid,
                "company_name": "Contoh Entity",
                "google_drive_folder_id": "folder-42",
                "status": "active",
            },
            {
                "id": 915,
                "google_drive_file_id": "gd-prev",
                "google_drive_file_url": "https://drive.example/gd-prev",
            },
        ]
    )
    conn_dedup.fetch = AsyncMock(return_value=_DEV_COLS_25)
    res = await docs_router.upload_company_document_base64_internal(
        company_uuid=comp_uuid,
        data=docs_router.CompanyDocumentUploadBase64(
            file="ZmlsZQ==",
            file_name="nib.pdf",
            document_type="nib",
            intake_idempotency_key="idem-25col-hit",
        ),
        pool=_FakePool(conn_dedup),
        actor="svc-intake",
    )
    assert res["deduplicated"] is True
    assert res["company_document_id"] == 915
    assert "notes LIKE $2" in str(conn_dedup.fetchrow.await_args_list[1].args[0])


def test_sentry_pii_substrings_include_document_number() -> None:
    assert "document_number" in sentry_config._PII_KEY_SUBSTRINGS


@pytest.mark.asyncio
async def test_pg_standalone_company_commit_idempotent_and_rollback(
    pg_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "true")
    tag = f"t1comp-{uuid.uuid4().hex[:8]}"
    created: dict[str, list[int]] = {"companies": [], "instances": [], "queues": [], "proposals": []}
    async with pg_pool.acquire() as conn:
        comp_id = int(
            await conn.fetchval(
                "INSERT INTO companies (company_name, company_type, status) "
                "VALUES ($1, 'PT PMA', 'active') RETURNING id",
                f"PT {tag}",
            )
        )
        created["companies"].append(comp_id)
        bh = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
        inst_id = int(
            await conn.fetchval(
                "INSERT INTO document_instances (blob_hash, pipeline_version, blob_path, first_source) "
                "VALUES ($1, 'v2.4-ocr', $2, 'whatsapp') RETURNING id",
                bh,
                f"/tmp/{tag}.pdf",
            )
        )
        created["instances"].append(inst_id)
        qid = int(
            await conn.fetchval(
                "INSERT INTO intake_queue "
                "(instance_id, source, source_ref, blob_path, blob_hash, pipeline_version, "
                "status, stage_output, intake_key) "
                "VALUES ($1, 'whatsapp', $2, $3, $4, 'v2.4-ocr', 'review_pending', $5::jsonb, $6) "
                "RETURNING id",
                inst_id,
                tag,
                f"/tmp/{tag}.pdf",
                bh,
                json.dumps({"file_name": f"{tag}_nib.pdf", "document_type": "nib", "extracted_fields": {}}),
                f"wa|{tag}|{bh}|v2.4-ocr|{uuid.uuid4().hex[:6]}",
            )
        )
        created["queues"].append(qid)
        pid = int(
            await conn.fetchval(
                "INSERT INTO document_routing_proposal "
                "(queue_id, doc_index, pipeline_version, routing_key, entity_resolution, "
                "routing, commit_gate, status, lease_owner, lease_expires_at, claim_token) "
                "VALUES ($1, 0, 'v2.4-ocr', $2, $3::jsonb, $4::jsonb, $5::jsonb, 'review_claimed', "
                "'tester', now() + interval '15 min', $6) RETURNING id",
                qid,
                tag,
                json.dumps({"decision": "LINK_CANDIDATE", "candidates": []}),
                json.dumps({"client_id": None, "company_id": comp_id, "doc_type": "nib", "fields": {}}),
                json.dumps({"requires_human": True}),
                uuid.uuid4(),
            )
        )
        created["proposals"].append(pid)

    try:
        async with pg_pool.acquire() as conn:
            prop_row = await conn.fetchrow(
                "SELECT * FROM document_routing_proposal WHERE id = $1", pid
            )
            plan = await writer.plan_commit(
                dict(prop_row),
                conn,
                committed_by="tester",
                override_company_id=comp_id,
                company_explicit=True,
            )
            assert plan.client_id is None
            assert plan.company_id == comp_id
            res = await writer.execute_commit(plan, conn, dry_run=False)
            assert res.outcome == "committed"
            assert res.company_doc_id is not None

            cd_row = await conn.fetchrow(
                "SELECT id, created_at, updated_at, notes FROM company_documents WHERE id = $1",
                res.company_doc_id,
            )
            assert cd_row is not None
            assert cd_row["created_at"] is not None
            assert cd_row["updated_at"] is not None
            assert f"[intake_key:{plan.idempotency_key}]" in str(cd_row["notes"] or "")

            dup_id = await writer.write_company_document(
                conn,
                comp_id,
                plan.payload,
                proposal_id=pid,
                idempotency_key=plan.idempotency_key,
                committed_by="tester",
            )
            assert dup_id == res.company_doc_id

            rb = await writer.rollback_commit(
                conn,
                client_id=None,
                company_id=comp_id,
                idempotency_key=plan.idempotency_key,
                committed_by="tester",
            )
            assert rb.outcome == "rolled_back"
            assert rb.company_doc_id == res.company_doc_id
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM company_documents WHERE id = $1", res.company_doc_id
                )
                == 0
            )
    finally:
        async with pg_pool.acquire() as conn:
            for comp in created["companies"]:
                await conn.execute("DELETE FROM company_documents WHERE company_id = $1", comp)
            for pid_d in created["proposals"]:
                await conn.execute("DELETE FROM intake_commit_audit WHERE proposal_id = $1", pid_d)
                await conn.execute("DELETE FROM document_routing_proposal WHERE id = $1", pid_d)
            for qid_d in created["queues"]:
                await conn.execute("DELETE FROM intake_queue WHERE id = $1", qid_d)
            for iid_d in created["instances"]:
                await conn.execute("DELETE FROM document_instances WHERE id = $1", iid_d)
            for comp in created["companies"]:
                await conn.execute("DELETE FROM companies WHERE id = $1", comp)


@pytest.mark.asyncio
async def test_pg_linked_commit_refuses_standalone_rollback_and_allows_full_rollback(
    pg_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "true")
    tag = f"t1link-{uuid.uuid4().hex[:8]}"
    created: dict[str, list[int]] = {
        "clients": [],
        "companies": [],
        "instances": [],
        "queues": [],
        "proposals": [],
    }
    async with pg_pool.acquire() as conn:
        client_id = int(
            await conn.fetchval(
                "INSERT INTO clients (full_name) VALUES ($1) RETURNING id",
                f"Person {tag}",
            )
        )
        comp_id = int(
            await conn.fetchval(
                "INSERT INTO companies (company_name, company_type, status) "
                "VALUES ($1, 'PT PMA', 'active') RETURNING id",
                f"PT {tag}",
            )
        )
        await conn.execute(
            "INSERT INTO client_company_links (client_id, company_id, role, is_primary, status) "
            "VALUES ($1, $2, 'director', true, 'active')",
            client_id,
            comp_id,
        )
        created["clients"].append(client_id)
        created["companies"].append(comp_id)
        bh = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
        inst_id = int(
            await conn.fetchval(
                "INSERT INTO document_instances (blob_hash, pipeline_version, blob_path, first_source) "
                "VALUES ($1, 'v2.4-ocr', $2, 'whatsapp') RETURNING id",
                bh,
                f"/tmp/{tag}.pdf",
            )
        )
        created["instances"].append(inst_id)
        qid = int(
            await conn.fetchval(
                "INSERT INTO intake_queue "
                "(instance_id, source, source_ref, blob_path, blob_hash, pipeline_version, "
                "status, stage_output, intake_key) "
                "VALUES ($1, 'whatsapp', $2, $3, $4, 'v2.4-ocr', 'review_pending', $5::jsonb, $6) "
                "RETURNING id",
                inst_id,
                tag,
                f"/tmp/{tag}.pdf",
                bh,
                json.dumps({"file_name": f"{tag}_nib.pdf", "document_type": "nib", "extracted_fields": {}}),
                f"wa|{tag}|{bh}|v2.4-ocr|{uuid.uuid4().hex[:6]}",
            )
        )
        created["queues"].append(qid)
        pid = int(
            await conn.fetchval(
                "INSERT INTO document_routing_proposal "
                "(queue_id, doc_index, pipeline_version, routing_key, entity_resolution, "
                "routing, commit_gate, status, lease_owner, lease_expires_at, claim_token) "
                "VALUES ($1, 0, 'v2.4-ocr', $2, $3::jsonb, $4::jsonb, $5::jsonb, 'review_claimed', "
                "'tester', now() + interval '15 min', $6) RETURNING id",
                qid,
                tag,
                json.dumps({"decision": "LINK_CANDIDATE", "candidates": []}),
                json.dumps({"client_id": client_id, "company_id": comp_id, "doc_type": "nib", "fields": {}}),
                json.dumps({"requires_human": True}),
                uuid.uuid4(),
            )
        )
        created["proposals"].append(pid)

    try:
        async with pg_pool.acquire() as conn:
            prop_row = await conn.fetchrow(
                "SELECT * FROM document_routing_proposal WHERE id = $1", pid
            )
            plan = await writer.plan_commit(dict(prop_row), conn, committed_by="tester")
            res = await writer.execute_commit(plan, conn, dry_run=False)
            assert res.outcome == "committed"
            assert res.doc_id is not None
            assert res.company_doc_id is not None

            with pytest.raises(ValueError, match="linked client\\+company"):
                await writer.rollback_commit(
                    conn,
                    client_id=None,
                    company_id=comp_id,
                    idempotency_key=plan.idempotency_key,
                    committed_by="tester",
                )
            assert await conn.fetchval("SELECT count(*) FROM documents WHERE id = $1", res.doc_id) == 1
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM company_documents WHERE id = $1", res.company_doc_id
                )
                == 1
            )

            rb = await writer.rollback_commit(
                conn,
                client_id=client_id,
                company_id=comp_id,
                idempotency_key=plan.idempotency_key,
                committed_by="tester",
            )
            assert rb.outcome == "rolled_back"
            assert await conn.fetchval("SELECT count(*) FROM documents WHERE id = $1", res.doc_id) == 0
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM company_documents WHERE id = $1", res.company_doc_id
                )
                == 0
            )
    finally:
        async with pg_pool.acquire() as conn:
            for clid in created["clients"]:
                await conn.execute("DELETE FROM documents WHERE client_id = $1", clid)
                await conn.execute("DELETE FROM intake_commit_audit WHERE client_id = $1", clid)
            for comp in created["companies"]:
                await conn.execute("DELETE FROM client_company_links WHERE company_id = $1", comp)
                await conn.execute("DELETE FROM company_documents WHERE company_id = $1", comp)
            for pid_d in created["proposals"]:
                await conn.execute("DELETE FROM intake_commit_audit WHERE proposal_id = $1", pid_d)
                await conn.execute("DELETE FROM document_routing_proposal WHERE id = $1", pid_d)
            for qid_d in created["queues"]:
                await conn.execute("DELETE FROM intake_queue WHERE id = $1", qid_d)
            for iid_d in created["instances"]:
                await conn.execute("DELETE FROM document_instances WHERE id = $1", iid_d)
            for clid in created["clients"]:
                await conn.execute("DELETE FROM clients WHERE id = $1", clid)
            for comp in created["companies"]:
                await conn.execute("DELETE FROM companies WHERE id = $1", comp)


@pytest.mark.asyncio
async def test_pg_rolledback_38col_partial_unique_index_and_for_share_company_link() -> None:
    if not await _dsn_reachable():
        pytest.skip(f"local intake DB not reachable at {DSN}")

    conn = await asyncpg.connect(DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "ALTER TABLE company_documents "
            "ADD COLUMN IF NOT EXISTS intake_idempotency_key text, "
            "ADD COLUMN IF NOT EXISTS intake_proposal_id bigint, "
            "ADD COLUMN IF NOT EXISTS client_visible boolean NOT NULL DEFAULT false"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_company_docs_intake_key "
            "ON company_documents (company_id, intake_idempotency_key) "
            "WHERE intake_idempotency_key IS NOT NULL"
        )
        await conn.execute(
            "ALTER TABLE companies ADD COLUMN IF NOT EXISTS uuid text DEFAULT gen_random_uuid()::text"
        )
        assert await writer._has_company_intake_unique_index(conn) is True

        tag = uuid.uuid4().hex[:8]
        client_id = int(
            await conn.fetchval("INSERT INTO clients (full_name) VALUES ($1) RETURNING id", f"P-{tag}")
        )
        comp_uuid = uuid.uuid4()
        comp_id = int(
            await conn.fetchval(
                "INSERT INTO companies (company_name, company_type, status, uuid) "
                "VALUES ($1, 'PT PMA', 'active', $2) RETURNING id",
                f"PT-{tag}",
                str(comp_uuid),
            )
        )
        await conn.execute(
            "INSERT INTO client_company_links (client_id, company_id, role, is_primary, status) "
            "VALUES ($1, $2, 'director', true, 'active')",
            client_id,
            comp_id,
        )

        payload = {"document_type": "nib", "file_name": "NIB.pdf", "file_id": "gd-pg-1", "client_visible": True}
        id1 = await writer.write_company_document(
            conn, comp_id, payload, proposal_id=None, idempotency_key=f"k-{tag}", committed_by="tester"
        )
        id2 = await writer.write_company_document(
            conn, comp_id, payload, proposal_id=None, idempotency_key=f"k-{tag}", committed_by="tester"
        )
        assert id1 == id2

        resolved = await docs_router._resolve_upload_company(conn, client_id, comp_uuid, None, lock=True)
        assert resolved == comp_id
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_concurrent_post_upload_dedup_returns_winning_drive_file() -> None:
    """Concurrent race after Drive upload must return the winning row's file ref."""
    comp_uuid = str(uuid.uuid4())
    comp_payload = docs_router.CompanyDocumentUploadBase64(
        file="ZmlsZQ==",
        file_name="nib.pdf",
        document_type="nib",
        document_category="licensing",
        intake_idempotency_key="idem-race-comp-1",
    )

    comp_conn = AsyncMock()
    comp_conn.transaction = MagicMock(return_value=_FakeTx())
    comp_conn.execute = AsyncMock()
    comp_conn.fetch = AsyncMock(return_value=_PROD_COLS_38)
    comp_conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": 42,
                "uuid": comp_uuid,
                "company_name": "Contoh Entity",
                "google_drive_folder_id": "folder-42",
                "status": "active",
            },
            None,
            {"id": 42, "status": "active"},
            {
                "id": 990,
                "google_drive_file_id": "gd-winning-comp",
                "google_drive_file_url": "https://drive.example/winning-comp",
            },
        ]
    )
    comp_conn.fetchval = AsyncMock()

    mock_comp_drive = MagicMock()
    mock_comp_drive.resolve_canonical_folder = AsyncMock(return_value="sub-folder-42")
    mock_comp_drive.upload_file_to_folder = AsyncMock(
        return_value={
            "id": "gd-losing-comp",
            "webViewLink": "https://drive.example/losing-comp",
        }
    )

    with (
        patch(
            "backend.app.routers.crm_enhanced_documents.ServiceAccountDriveService",
            return_value=mock_comp_drive,
        ),
        patch("backend.app.routers.crm_enhanced_documents.invalidate_cache", new=AsyncMock()),
    ):
        comp_res = await docs_router.upload_company_document_base64_internal(
            company_uuid=comp_uuid,
            data=comp_payload,
            pool=_FakePool(comp_conn),
            actor="svc-intake",
        )

    assert comp_res == {
        "success": True,
        "company_document_id": 990,
        "company_id": 42,
        "file_id": "gd-winning-comp",
        "file_url": "https://drive.example/winning-comp",
        "deduplicated": True,
    }
    comp_conn.fetchval.assert_not_awaited()

    doc_payload = docs_router.DocumentUploadBase64(
        file="ZmlsZQ==",
        file_name="passport.pdf",
        document_type="passport",
        document_category="immigration",
        mime_type="application/pdf",
        intake_idempotency_key="idem-race-doc-1",
    )
    doc_conn = AsyncMock()
    doc_conn.transaction = MagicMock(return_value=_FakeTx())
    doc_conn.execute = AsyncMock()
    doc_conn.fetch = AsyncMock(
        return_value=[
            {"column_name": "id"},
            {"column_name": "notes"},
            {"column_name": "intake_idempotency_key"},
        ]
    )
    doc_conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": 1,
                "full_name": "Test Person",
                "client_type": "individual",
                "google_drive_folder_id": "rf1",
                "phone": None,
                "phone_normalized": None,
            },
            None,
            {
                "id": 880,
                "file_id": "gd-winning-doc",
                "file_url": "https://drive.example/winning-doc",
            },
        ]
    )
    doc_conn.fetchval = AsyncMock()

    mock_doc_drive = MagicMock()
    mock_doc_drive.get_file_metadata = AsyncMock(
        return_value={"id": "rf1", "trashed": False, "parents": ["ind"]}
    )
    mock_doc_drive.resolve_canonical_folder = AsyncMock(return_value="sub1")
    mock_doc_drive.upload_file_to_folder = AsyncMock(
        return_value={
            "id": "gd-losing-doc",
            "webViewLink": "https://drive.example/losing-doc",
        }
    )

    bg_tasks = BackgroundTasks()
    with (
        patch("backend.app.routers.crm_enhanced_documents.verify_client_access", new=AsyncMock()),
        patch(
            "backend.app.routers.crm_enhanced_documents._active_duplicate_document_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "backend.app.routers.crm_enhanced_documents.ServiceAccountDriveService",
            return_value=mock_doc_drive,
        ),
        patch(
            "backend.app.routers.crm_enhanced_documents.physical_root_layout",
            return_value="individual",
        ),
        patch("backend.app.routers.crm_enhanced_documents.invalidate_cache", new=AsyncMock()),
    ):
        doc_res = await docs_router.upload_document_base64(
            client_id=1,
            data=doc_payload,
            pool=_FakePool(doc_conn),
            current_user={"id": "1", "email": "staff-user", "role": "admin"},
            background_tasks=bg_tasks,
        )

    assert doc_res == {
        "success": True,
        "document_id": 880,
        "file_id": "gd-winning-doc",
        "file_url": "https://drive.example/winning-doc",
        "ocr_triggered": False,
        "deduplicated": True,
    }
    assert len(bg_tasks.tasks) == 0
    ocr_updates = [
        c for c in doc_conn.execute.await_args_list if "ocr_status" in str(c.args[0])
    ]
    assert ocr_updates == []
