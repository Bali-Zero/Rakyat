"""Synthetic checks for the local-to-canonical practice identity boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from backend.app.routers import crm_enhanced_documents as documents
from backend.app.setup.exception_handlers import (
    http_exception_handler,
    validation_exception_handler,
)
from backend.services.intake import crm_delivery, crm_push

PRACTICE_UUID = UUID("11111111-2222-4333-8444-555555555555")
OTHER_UUID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


@pytest.fixture
def upload_context():
    state = {"owner": 11, "move_before_insert": False}
    client = {
        "id": 11,
        "full_name": "Synthetic Client",
        "client_type": "individual",
        "google_drive_folder_id": "synthetic-root",
        "phone": None,
        "phone_normalized": None,
        "whatsapp": None,
    }
    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    async def fetchrow(sql, *args):
        if "FROM clients" in sql:
            return client
        if "FROM practices" in sql:
            if "FOR SHARE" in sql and state["move_before_insert"]:
                state["owner"] = 22
            key = str(PRACTICE_UUID) if "uuid =" in sql else 901
            if args[0] == key and args[1] == state["owner"]:
                return {"id": 901}
            return None
        raise AssertionError(f"unexpected query {sql}")

    conn.fetchrow.side_effect = fetchrow
    conn.fetchval.return_value = 7001
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    drive = AsyncMock()
    drive.get_folder_structure.return_value = {
        "folders": [{"id": "synthetic-folder", "name": "00_Profile"}]
    }
    drive.upload_file_to_folder.return_value = {
        "id": "synthetic-file",
        "webViewLink": "https://drive.test/synthetic-file",
    }
    duplicate = AsyncMock(return_value=None)
    with (
        patch.object(documents, "verify_client_access", new=AsyncMock()),
        patch.object(documents, "invalidate_cache", new=AsyncMock()),
        patch.object(documents, "_active_duplicate_document_id", new=duplicate),
        patch.object(documents, "ServiceAccountDriveService", return_value=drive) as factory,
    ):
        yield SimpleNamespace(
            state=state,
            client=client,
            conn=conn,
            pool=pool,
            drive=drive,
            factory=factory,
            duplicate=duplicate,
        )


async def upload(context, practice_id=None, **extra):
    return await documents.upload_document_base64(
        client_id=11,
        data=documents.DocumentUploadBase64(
            file="ZmlsZQ==",
            file_name="synthetic.pdf",
            document_type="passport",
            document_category="personal",
            practice_id=practice_id,
            **extra,
        ),
        pool=context.pool,
        current_user={"email": "operator@example.test", "role": "admin"},
        background_tasks=BackgroundTasks(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("practice_id", [str(PRACTICE_UUID), 901, None])
async def test_upload_resolves_canonical_practice_before_insert(upload_context, practice_id):
    result = await upload(upload_context, practice_id)
    assert result["success"] is True
    insert = next(
        c
        for c in upload_context.conn.fetchval.await_args_list
        if "INSERT INTO documents" in c.args[0]
    )
    expected = 901 if practice_id is not None else None
    assert insert.args[13] == expected
    assert upload_context.duplicate.await_args.kwargs["practice_id"] == expected
    practice_reads = [
        c for c in upload_context.conn.fetchrow.await_args_list if "FROM practices" in c.args[0]
    ]
    assert len(practice_reads) == (2 if practice_id is not None else 0)
    if practice_reads:
        assert "FOR SHARE" in practice_reads[-1].args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("practice_id", [901, str(PRACTICE_UUID), str(OTHER_UUID)])
async def test_upload_refuses_foreign_or_unknown_practice_before_drive(upload_context, practice_id):
    upload_context.state["owner"] = 22
    with pytest.raises(HTTPException) as exc:
        await upload(upload_context, practice_id)
    assert exc.value.status_code == 409
    assert exc.value.detail == "practice_identity_unresolved"
    upload_context.factory.assert_not_called()
    upload_context.conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_numeric_bridge_practice_fails_closed(upload_context):
    with pytest.raises(HTTPException) as exc:
        await upload(upload_context, 901, expected_phone_core="81234567890")
    assert exc.value.status_code == 409
    assert exc.value.detail == "practice_identity_unresolved"
    upload_context.factory.assert_not_called()


@pytest.mark.asyncio
async def test_practice_owner_move_during_upload_cannot_insert(upload_context):
    upload_context.state["move_before_insert"] = True
    with pytest.raises(HTTPException) as exc:
        await upload(upload_context, str(PRACTICE_UUID))
    assert exc.value.status_code == 409
    assert exc.value.detail == "practice_identity_unresolved"
    upload_context.drive.upload_file_to_folder.assert_awaited_once()
    upload_context.conn.fetchval.assert_not_awaited()


def test_uuid_wire_value_is_rejected_by_the_legacy_integer_contract():
    class LegacyUpload(BaseModel):
        practice_id: int | None = None

    with pytest.raises(ValidationError):
        LegacyUpload(practice_id=str(PRACTICE_UUID))


@pytest.mark.parametrize("reference", [True, False, "901.0", 901.5, "901\n", PRACTICE_UUID.hex])
def test_upload_model_refuses_ambiguous_practice_ids(reference):
    with pytest.raises(ValidationError):
        documents.DocumentUploadBase64(
            file="ZmlsZQ==",
            file_name="synthetic.pdf",
            document_type="passport",
            practice_id=reference,
        )


@pytest.mark.parametrize("reference", ["901", 901, 901.0])
def test_upload_model_preserves_exact_integer_practice_ids(reference):
    model = documents.DocumentUploadBase64(
        file="ZmlsZQ==",
        file_name="synthetic.pdf",
        document_type="passport",
        practice_id=reference,
    )
    assert type(model.practice_id) is int and model.practice_id == 901


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/internal"])
@pytest.mark.parametrize("reference", [str(OTHER_UUID), 2**31, str(2**31), str(-(2**31) - 1)])
async def test_upload_conflict_is_declared_on_both_http_entrypoints(
    upload_context, prefix, reference
):
    app = FastAPI()
    app.include_router(documents.router)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.dependency_overrides[documents.get_database_pool] = lambda: upload_context.pool
    app.dependency_overrides[documents.get_current_user] = lambda: {"role": "admin"}
    app.dependency_overrides[documents.verify_crm_write_key] = lambda: "synthetic-service"
    path = "/api/crm" + prefix + "/clients/{client_id}/documents/upload"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        invalid = await client.post(
            path.format(client_id=11),
            json={
                "file": "ZmlsZQ==",
                "file_name": "synthetic.pdf",
                "document_type": "passport",
                "practice_id": True,
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["correlation_id"] == "unknown"
        upload_context.conn.fetchrow.assert_not_awaited()
        result = await client.post(
            path.format(client_id=11),
            json={
                "file": "ZmlsZQ==",
                "file_name": "synthetic.pdf",
                "document_type": "passport",
                "practice_id": reference,
            },
        )
    assert result.status_code == 409
    assert result.json() == {"detail": "practice_identity_unresolved", "correlation_id": "unknown"}
    if reference != str(OTHER_UUID):
        assert all(
            "FROM practices" not in call.args[0]
            for call in upload_context.conn.fetchrow.await_args_list
        )
    upload_context.factory.assert_not_called()
    schema = app.openapi()
    ref = schema["paths"][path]["post"]["responses"]["409"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    conflict = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    assert set(conflict["required"]) == {"detail", "correlation_id"}
    assert all(conflict["properties"][field]["type"] == "string" for field in conflict["required"])


@pytest.mark.asyncio
async def test_bridge_request_resolves_uuid_through_the_real_upload(
    upload_context, tmp_path, monkeypatch
):
    upload_context.client.update(phone="6281234567890", phone_normalized="6281234567890")
    upload_context.conn.fetch.return_value = [{"id": 11}]
    requests = []

    async def serve(request):
        requests.append(request)
        if request.url.path.endswith("/upsert-by-phone"):
            return httpx.Response(200, json={"client_id": 11, "matched_count": 1})
        assert request.url.path == "/api/crm/clients/11/documents/upload"
        data = documents.DocumentUploadBase64.model_validate_json(request.content)
        result = await documents.upload_document_base64(
            client_id=11,
            data=data,
            pool=upload_context.pool,
            current_user={"email": "operator@example.test", "role": "admin"},
            background_tasks=BackgroundTasks(),
        )
        return httpx.Response(200, json=result)

    blob = tmp_path / "synthetic.pdf"
    blob.write_bytes(b"synthetic content")
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        monkeypatch.setattr(crm_push, "_client", client)
        result = await crm_push.push_committed_document(
            bearer_token="synthetic-jwt",
            crm_write_key="synthetic-key",
            client_id=77,
            practice_uuid=str(PRACTICE_UUID),
            file_name="synthetic.pdf",
            document_type="passport",
            document_category="personal",
            blob_path=str(blob),
            sender_phone="6281234567890",
            base_url="http://fly.test",
            client_visible=False,
        )
    assert result.ok is True
    assert len(requests) == 2
    insert = next(
        c
        for c in upload_context.conn.fetchval.await_args_list
        if "INSERT INTO documents" in c.args[0]
    )
    assert insert.args[1] == 11
    assert insert.args[13] == 901
    assert insert.args[16] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("local_uuid", [PRACTICE_UUID, None])
async def test_local_delivery_resolves_uuid_under_owner_before_push(local_uuid):
    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    conn.fetchrow.return_value = {
        "full_name": "Synthetic Client",
        "phone": None,
        "phone_normalized": None,
        "whatsapp": None,
    }
    conn.fetchval.return_value = local_uuid
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    push = AsyncMock(return_value=crm_push.CrmPushResult(ok=True, status="pushed"))
    with patch.object(crm_push, "push_committed_document", new=push):
        result = await crm_delivery._resolve_and_push_locked(
            pool=pool,
            plan=SimpleNamespace(client_id=11, practice_id=7, pipeline_version="v1"),
            payload={},
            queue_id=1,
            blob_path="synthetic.pdf",
            mime_type="application/pdf",
            bearer_token="synthetic-token",
            crm_write_key=None,
        )
    lookup = conn.fetchval.await_args
    assert "FROM practices" in lookup.args[0]
    assert "client_id = $2" in lookup.args[0]
    assert "FOR SHARE" in lookup.args[0]
    assert lookup.args[1:] == (7, 11)
    if local_uuid is None:
        assert result.status == "practice_identity_unresolved"
        push.assert_not_awaited()
    else:
        assert push.await_args.kwargs["practice_uuid"] == str(local_uuid)
        assert "practice_id" not in push.await_args.kwargs
