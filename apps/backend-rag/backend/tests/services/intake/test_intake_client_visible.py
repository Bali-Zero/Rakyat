"""P3 - ``client_visible`` is an explicit optional field of the intake writer and delivery.

The client-document-recovery producer tags its rows ``recovery-v1``; those documents are
born STAFF-ONLY (``documents.client_visible = false``) in the SAME INSERT - locally in the
writer, and on the CRM through the delivery body - and a staff toggle opens them after
review. GUILT: the tag is hidden, from the plan through a real upload request to both INSERTs.
INNOCENCE: any other tag keeps the old default - the legacy statements and the old body are
pinned below, whitespace aside.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import backend.app.routers.crm_enhanced_documents as docs_router
from backend.app.core.config import settings
from backend.app.dependencies import get_database_pool
from backend.services.crm.document_categorizer import CATEGORY_TO_FOLDER
from backend.services.intake import crm_delivery, crm_push
from backend.services.intake import writer as intake_writer
from backend.services.intake.enqueue import enqueue

DSN = os.environ.get("INTAKE_TEST_DSN", "postgresql://localhost:5432/nuzantara_test")

# The two statements write_client_document issued before this change ("old default").
_HEAD = (
    "INSERT INTO documents ( client_id, document_type, document_category, file_name, file_id, "
    "file_url, google_drive_file_url, notes, family_member_id, practice_id, status, "
    "storage_type, {ocr}intake_idempotency_key, intake_proposal_id ) VALUES "
    "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'received','google_drive',{vals}) "
    "ON CONFLICT (client_id, intake_idempotency_key) WHERE intake_idempotency_key IS NOT NULL "
    "DO UPDATE SET updated_at = now() RETURNING id"
)
LEGACY = {
    True: _HEAD.format(ocr="ocr_status, ", vals="$11,$12,$13"),
    False: _HEAD.format(ocr="", vals="$11,$12"),
}


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("recovery-v1", False),
        ("recovery-v1:cdr-batch-20261103", False),
        # innocence (guard family #3): exact-or-separator, never a bare prefix
        ("recovery-v10", None),
        ("recovery-v1-other", None),
        ("RECOVERY-V1", None),
        ("intake-v1", None),
        ("", None),
        (None, None),
    ],
)
def test_explicit_client_visible_policy(tag: str | None, expected: bool | None):
    assert intake_writer.explicit_client_visible(tag) is expected


# --------------------------------------------------------------------------- #
# write_client_document: the INSERT statement, DB-free
# --------------------------------------------------------------------------- #
class _InsertRecorder:
    def __init__(self, has_ocr: bool) -> None:
        self.has_ocr, self.sql, self.args = has_ocr, "", ()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "information_schema.columns" in sql:
            return 1 if self.has_ocr else None
        self.sql, self.args = " ".join(sql.split()), args
        return 4242


def _payload(**extra: Any) -> dict[str, Any]:
    return {
        "document_type": "visa",
        "document_category": "immigration",
        "file_name": "visa__abc123def456.pdf",
        "notes": "intake:cdr:abc123def456",
        "ocr_status": "completed",
        **extra,
    }


@pytest.mark.parametrize("has_ocr", [True, False])
@pytest.mark.parametrize("visible", [None, False, True])
async def test_insert_is_the_legacy_statement_plus_at_most_one_column(has_ocr, visible):
    conn = _InsertRecorder(has_ocr)
    extra = {} if visible is None else {"client_visible": visible}
    await intake_writer.write_client_document(
        conn,
        501,
        _payload(**extra),
        proposal_id=77,
        idempotency_key="ik:test",  # type: ignore[arg-type]
    )

    legacy, n_legacy = LEGACY[has_ocr], 13 if has_ocr else 12
    if visible is None:  # innocence: the old statement, the old arguments, the column not named
        assert conn.sql == legacy and len(conn.args) == n_legacy
        return
    expected = legacy.replace("intake_proposal_id )", "intake_proposal_id, client_visible )")
    expected = expected.replace(f"${n_legacy})", f"${n_legacy}, ${n_legacy + 1})")
    assert conn.sql == expected
    assert conn.args[-3:] == ("ik:test", 77, visible)  # a falsy False is written, not dropped


# --------------------------------------------------------------------------- #
# real SQL against the local nuzantara_test book
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    try:
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    except Exception:
        pytest.skip(f"local intake DB not reachable at {DSN}")
    yield p
    await p.close()


async def _new_client(pool: asyncpg.Pool, **columns: str) -> int:
    names = ", ".join(["full_name", *columns])
    marks = ", ".join(f"${i + 1}" for i in range(len(columns) + 1))
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"INSERT INTO clients ({names}) VALUES ({marks}) RETURNING id",
            f"cv-test-{uuid.uuid4().hex[:8]}",
            *columns.values(),
        )


async def _drop_client(pool: asyncpg.Pool, cid: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE client_id = $1", cid)
        await conn.execute("DELETE FROM clients WHERE id = $1", cid)


async def test_real_insert_default_explicit_and_recommit(pool):
    cid = await _new_client(pool)
    try:

        async def commit(key: str, **extra: Any) -> int:
            async with pool.acquire() as conn:
                return await intake_writer.write_client_document(
                    conn, cid, _payload(**extra), proposal_id=987001, idempotency_key=key
                )

        async def visible(doc_id: int) -> bool | None:
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT client_visible FROM documents WHERE id = $1", doc_id
                )

        keys = [f"ik:{uuid.uuid4().hex}" for _ in range(3)]
        default_id = await commit(keys[0])
        hidden_id = await commit(keys[1], client_visible=False)
        shown_id = await commit(keys[2], client_visible=True)
        assert await visible(default_id) is True  # the old database default, column not named
        assert await visible(hidden_id) is False
        assert await visible(shown_id) is True

        # the same intake instance committed again with the opposite value never flips a row
        assert await commit(keys[1], client_visible=True) == hidden_id
        assert await commit(keys[0], client_visible=False) == default_id
        assert await visible(hidden_id) is False and await visible(default_id) is True
    finally:
        await _drop_client(pool, cid)


# --------------------------------------------------------------------------- #
# One request, end to end: writer -> delivery -> push -> the REAL upload route -> both INSERTs
# --------------------------------------------------------------------------- #
CHAIN_KEY = "chain-test-crm-write-key"


class _FakeDrive:
    """The route's Drive hop (external storage): the one collaborator replaced inside the route."""

    async def get_folder_structure(self, **_: Any) -> dict[str, Any]:
        return {"folders": [{"id": f"f-{n}", "name": n} for n in set(CATEGORY_TO_FOLDER.values())]}

    async def create_folder(self, **_: Any) -> dict[str, Any]:
        return {"id": "created-folder"}

    async def upload_file_to_folder(self, **_: Any) -> dict[str, Any]:
        return {"id": "drive-chain-file", "webViewLink": "https://drive.example.test/d/chain-file"}


@pytest_asyncio.fixture
async def fly_book(pool: asyncpg.Pool) -> AsyncIterator[tuple[asyncpg.Pool, list[str]]]:
    """The CRM backend's own ``documents`` table, and the SQL the route sends to it.

    Production's table carries content_hash, subfolder and ocr_status, which the upload route
    reads and writes; the intake book (and CI's bootstrap) has none of them. A TEMP copy on a
    one-connection pool gives the real route its real INSERT without touching the shared schema.
    """
    fly = await asyncpg.create_pool(DSN, min_size=1, max_size=1)
    statements: list[str] = []
    async with fly.acquire() as conn:
        await conn.execute("CREATE TEMP TABLE documents (LIKE public.documents INCLUDING ALL)")
        await conn.execute(
            "ALTER TABLE pg_temp.documents ADD COLUMN content_hash text,"
            " ADD COLUMN subfolder text, ADD COLUMN ocr_status text"
        )
        conn.add_query_logger(lambda q: statements.append(" ".join(q.query.split())))
    yield fly, statements
    await fly.close()


@pytest.mark.parametrize(
    ("tag", "drop_payload_key", "staff_only"),
    [
        ("recovery-v1", False, True),
        # a plan assembled without the payload key still delivers staff-only
        ("recovery-v1", True, True),
        # innocence: an ordinary tag names the column in neither INSERT; the default applies
        ("intake-v1", False, False),
    ],
)
async def test_client_visible_follows_one_request_from_the_writer_to_both_inserts(
    pool, fly_book, tmp_path, monkeypatch, tag, drop_payload_key, staff_only
):
    """No stubbed hop between push and the upload: push's HTTP request is served by the real route
    (FastAPI, the real request model and service-key dependency) and ends in a real INSERT.

    Replaced inside the route besides Drive: Redis, the OCR background task and the phone advisory
    lock (this one Postgres plays Pro and Fly; in production the route's lock lives in Fly's own
    database and never waits on the delivery's). The phone resolver, which only maps the client to
    its Fly id, answers with the client itself.
    """
    fly_pool, fly_sql = fly_book
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "1")
    monkeypatch.setenv("INTAKE_CRM_PUSH_BASE_URL", "http://fly.test")
    monkeypatch.setattr(settings, "wa_mirror_crm_write_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", CHAIN_KEY, raising=False)
    for name, stand_in in {
        "ServiceAccountDriveService": _FakeDrive,
        "invalidate_cache": AsyncMock(),
        "_dispatch_ocr_by_folder": AsyncMock(),
        "lock_cores": AsyncMock(),
    }.items():
        monkeypatch.setattr(docs_router, name, stand_in)
    app = FastAPI()
    app.include_router(docs_router.router)
    app.dependency_overrides[get_database_pool] = lambda: fly_pool
    upload_route = httpx.ASGITransport(app=app)

    phone = "62" + str(uuid.uuid4().int)[:9]
    cid = await _new_client(pool, phone_normalized=phone, google_drive_folder_id="root-folder")
    blob = tmp_path / "scan.pdf"
    blob.write_bytes(f"%PDF-1.4 {uuid.uuid4()}".encode())
    queued = await enqueue(
        pool,
        source="drive",
        source_ref=f"cdr:{uuid.uuid4().hex[:12]}",
        blob_path=str(blob),
        pipeline_version=tag,
    )

    async def fly(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/upsert-by-phone"):
            return httpx.Response(200, json={"client_id": cid, "matched_count": 1})
        return await upload_route.handle_async_request(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(fly))
    monkeypatch.setattr(crm_push, "_client", http)
    try:
        async with pool.acquire() as conn:
            proposal_id = await conn.fetchval(
                "INSERT INTO document_routing_proposal (queue_id, doc_index, pipeline_version,"
                " routing_key, entity_resolution, routing, commit_gate, status)"
                " VALUES ($1, 0, $2, $3, $4::jsonb, $5::jsonb, '{}'::jsonb, 'review_pending')"
                " RETURNING id",
                queued.queue_id,
                tag,
                f"cv-{uuid.uuid4().hex}",
                json.dumps({"decision": "AUTO_ATTACH", "candidates": []}),
                json.dumps({"client_id": cid, "doc_type": "visa", "fields": {}}),
            )
            proposal = await conn.fetchrow(
                "SELECT * FROM document_routing_proposal WHERE id = $1", proposal_id
            )
            plan = await intake_writer.plan_commit(proposal, conn, committed_by="cv-chain")
            async with conn.transaction():
                result = await intake_writer.execute_commit(
                    plan,
                    conn,
                    dry_run=False,
                    advance_from="review_pending",
                    advance_to="auto_routed",
                )
        assert result.outcome == "committed"
        if drop_payload_key:
            plan.payload.pop("client_visible")

        out = await crm_delivery.deliver_committed_to_crm(
            pool=pool,
            queue_id=queued.queue_id,
            plan=plan,
            result=result,
            crm_write_key=CHAIN_KEY,
        )

        assert out["status"] == "pushed", out
        fly_rows = await fly_pool.fetch(
            "SELECT id, client_visible FROM documents WHERE client_id = $1", cid
        )
        assert [(r["id"], r["client_visible"]) for r in fly_rows] == [
            (out["fly_doc_id"], not staff_only)
        ]
        inserts = [s for s in fly_sql if s.startswith("INSERT INTO documents")]
        assert len(inserts) == 1
        assert ("client_visible" in inserts[0]) is staff_only
        local = await pool.fetchval(
            "SELECT client_visible FROM documents WHERE id = $1", result.doc_id
        )
        assert local is (not staff_only)
    finally:
        await http.aclose()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM intake_queue WHERE id = $1", queued.queue_id)
            await conn.execute("DELETE FROM document_instances WHERE id = $1", queued.instance_id)
        await _drop_client(pool, cid)
