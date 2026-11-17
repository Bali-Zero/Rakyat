"""Regression suite for RT-07..RT-10 security fixes and Fly single-worker hardening."""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import backend.app.routers.accounting as accounting_router
import backend.app.routers.admin_drive_refresh as admin_drive_refresh_router
import backend.app.routers.admin_drive_setup as admin_drive_setup_router
import backend.app.routers.audio as audio_router
import backend.app.routers.documents_proxy as documents_proxy_router
import backend.app.routers.legal_ingest as legal_ingest_router
import backend.app.routers.media as media_router
import backend.app.routers.sheets as sheets_router
import backend.app.routers.team_drive as team_drive_router
import backend.app.routers.zoho_email as zoho_email_router
import backend.services.integrations.sheets_service as sheets_service_module
import backend.services.portal.document_processing as document_processing_module
import backend.services.workflow.chains.intel as intel_chain

_ADMIN_USER = {"role": "admin", "user_id": "admin-1"}
_STAFF_USER = {"role": "team", "user_id": "staff-1"}


class _FakeUpload:
    """Minimal UploadFile double for unit-testing size/name guards without Starlette buffering."""

    def __init__(
        self,
        filename: str | None,
        data: bytes = b"payload",
        *,
        size: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.filename = filename
        self._data = data
        self.size = size
        self.content_type = content_type
        self.file = io.BytesIO(data)
        self.read_calls: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if size is None or size < 0:
            return self._data
        return self._data[:size]


class _FakeAcquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.conn = AsyncMock()
        self.conn.fetchrow.return_value = row

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


# ---------------------------------------------------------------------------
# RT-08: legal_ingest path confinement & upload limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["../evil.pdf", "../../etc/passwd.pdf", "/etc/passwd.pdf", "a\\b.pdf"])
@pytest.mark.asyncio
async def test_rt08_legal_ingest_rejects_path_traversal_400(bad_name: str) -> None:
    upload = _FakeUpload(bad_name, b"%PDF-1.4")
    with pytest.raises(HTTPException) as exc:
        await legal_ingest_router.upload_legal_document(
            file=upload,  # type: ignore[arg-type]
            current_user=dict(_ADMIN_USER),
        )
    assert exc.value.status_code == 400
    assert upload.read_calls == []


@pytest.mark.asyncio
async def test_rt08_legal_ingest_rejects_oversized_upload_413() -> None:
    declared_oversize = _FakeUpload(
        "big.pdf",
        b"%PDF",
        size=legal_ingest_router._MAX_LEGAL_UPLOAD_BYTES + 1,
        content_type="application/pdf",
    )
    with pytest.raises(HTTPException) as exc_declared:
        await legal_ingest_router.upload_legal_document(
            file=declared_oversize,  # type: ignore[arg-type]
            current_user=dict(_ADMIN_USER),
        )
    assert exc_declared.value.status_code == 413
    assert declared_oversize.read_calls == []

    stream_oversize = _FakeUpload(
        "big_stream.pdf",
        b"x" * (legal_ingest_router._MAX_LEGAL_UPLOAD_BYTES + 1),
        size=None,
        content_type="application/pdf",
    )
    with pytest.raises(HTTPException) as exc_stream:
        await legal_ingest_router.upload_legal_document(
            file=stream_oversize,  # type: ignore[arg-type]
            current_user=dict(_ADMIN_USER),
        )
    assert exc_stream.value.status_code == 413
    assert stream_oversize.read_calls == [legal_ingest_router._MAX_LEGAL_UPLOAD_BYTES + 1]


# ---------------------------------------------------------------------------
# RT-09: admin_drive_refresh & admin_drive_setup gating + zero token_preview
# ---------------------------------------------------------------------------


def _build_admin_drive_test_client(router_obj: Any, user: Any) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request: Request, call_next: Any) -> Any:
        request.state.user = user
        return await call_next(request)

    app.include_router(router_obj)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(("user", "expected"), [(None, 401), (_STAFF_USER, 403)])
async def test_rt09_admin_drive_refresh_requires_admin(user: Any, expected: int) -> None:
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=None)), state=SimpleNamespace(user=user))
    with pytest.raises(HTTPException) as exc:
        await admin_drive_refresh_router.drive_refresh(req)  # type: ignore[arg-type]
    assert exc.value.status_code == expected

    client = _build_admin_drive_test_client(admin_drive_refresh_router.router, user)
    resp = client.post("/api/admin/drive/refresh")
    assert resp.status_code == expected


@pytest.mark.asyncio
async def test_rt09_admin_drive_refresh_strips_token_preview() -> None:
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db_pool=_FakePool({"refresh_token": "rtok-1"}))),
        state=SimpleNamespace(user=dict(_ADMIN_USER)),
    )
    fake_drive = MagicMock()
    fake_drive._refresh_token = AsyncMock(return_value="ya29.mocked_oauth_token_never_leak")
    with patch(
        "backend.services.integrations.google_drive_service.GoogleDriveService",
        return_value=fake_drive,
    ):
        result = await admin_drive_refresh_router.drive_refresh(
            req,  # type: ignore[arg-type]
            current_user=dict(_ADMIN_USER),
        )
    assert result["status"] == "success"
    assert "token_preview" not in result
    assert "ya29." not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(("user", "expected"), [(None, 401), (_STAFF_USER, 403)])
async def test_rt09_admin_drive_setup_routes_require_admin(user: Any, expected: int) -> None:
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=None)), state=SimpleNamespace(user=user))
    for fn in (
        admin_drive_setup_router.use_service_account,
        admin_drive_setup_router.service_account_status,
        admin_drive_setup_router.test_list_files,
    ):
        with pytest.raises(HTTPException) as exc:
            await fn(req)  # type: ignore[arg-type]
        assert exc.value.status_code == expected

    client = _build_admin_drive_test_client(admin_drive_setup_router.router, user)
    assert client.post("/api/admin/drive/use-service-account").status_code == expected
    assert client.get("/api/admin/drive/service-account-status").status_code == expected
    assert client.get("/api/admin/drive/test-list-files").status_code == expected


@pytest.mark.asyncio
async def test_rt09_admin_drive_setup_offloads_sa_probe_to_thread(monkeypatch: Any) -> None:
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db_pool=object())),
        state=SimpleNamespace(user=dict(_ADMIN_USER)),
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/fake-sa.json")
    thread_calls: list[Any] = []

    async def _fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        thread_calls.append((fn, args))
        return {"emailAddress": "sa-identity", "displayName": "SA"}

    with (
        patch("os.path.exists", return_value=True),
        patch("backend.app.routers.admin_drive_setup.asyncio.to_thread", side_effect=_fake_to_thread),
    ):
        res = await admin_drive_setup_router.use_service_account(
            req,  # type: ignore[arg-type]
            current_user=dict(_ADMIN_USER),
        )
    assert res["status"] == "success"
    assert thread_calls and thread_calls[0][0] is admin_drive_setup_router._probe_service_account_sync


# ---------------------------------------------------------------------------
# RT-10: documents_proxy active MIME attachment, nosniff, header sanitization, 413
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(self, status_code: int, chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._chunks = chunks
        self.headers = headers or {}

    async def __aenter__(self) -> _FakeStreamResponse:
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeMetaResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeStreamingHttpClient:
    def __init__(self, meta_payload: dict[str, Any], chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
        self._meta_payload = meta_payload
        self._chunks = chunks
        self._headers = headers or {}
        self.stream_called = False

    async def __aenter__(self) -> _FakeStreamingHttpClient:
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    async def get(self, *_args: Any, **_kwargs: Any) -> _FakeMetaResponse:
        return _FakeMetaResponse(200, self._meta_payload)

    def stream(self, *_args: Any, **_kwargs: Any) -> _FakeStreamResponse:
        self.stream_called = True
        return _FakeStreamResponse(200, self._chunks, self._headers)


@pytest.mark.parametrize(
    "dangerous_mime",
    ["text/html", "text/html; charset=utf-8", "image/svg+xml", "application/xhtml+xml", "text/javascript"],
)
@pytest.mark.asyncio
async def test_rt10_documents_proxy_forces_attachment_and_nosniff_for_active_mimes(
    monkeypatch: Any,
    dangerous_mime: str,
) -> None:
    fake_client = _FakeStreamingHttpClient(
        {"mimeType": dangerous_mime, "name": 'evil.svg\r\nX-Injected: "1', "size": "12", "parents": []},
        [b"<svg onload=alert(1)>"],
    )
    monkeypatch.setattr(documents_proxy_router, "_assert_portal_caller_owns_file", AsyncMock())
    monkeypatch.setattr(documents_proxy_router, "_file_is_registered", AsyncMock(return_value=True))
    monkeypatch.setattr(documents_proxy_router, "_get_drive_access_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(documents_proxy_router.httpx, "AsyncClient", lambda **_k: fake_client)

    response = await documents_proxy_router.proxy_drive_file(
        "1MUzGL73pcbY",
        current_user=dict(_ADMIN_USER),
        db_pool=object(),
    )
    assert response.media_type == "application/octet-stream"
    disp = response.headers["content-disposition"]
    assert disp.startswith('attachment; filename="')
    assert "\r" not in disp and "\n" not in disp and disp.count('"') == 2
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_rt10_documents_proxy_allows_safe_pdf_inline_and_caps_25mb(monkeypatch: Any) -> None:
    monkeypatch.setattr(documents_proxy_router, "_assert_portal_caller_owns_file", AsyncMock())
    monkeypatch.setattr(documents_proxy_router, "_file_is_registered", AsyncMock(return_value=True))
    monkeypatch.setattr(documents_proxy_router, "_get_drive_access_token", AsyncMock(return_value="tok"))

    safe_client = _FakeStreamingHttpClient(
        {"mimeType": "application/pdf", "name": "visa-permit.pdf", "size": "7", "parents": []},
        [b"%PDF-1."],
    )
    monkeypatch.setattr(documents_proxy_router.httpx, "AsyncClient", lambda **_k: safe_client)
    res = await documents_proxy_router.proxy_drive_file(
        "1MUzGL73pcbY",
        current_user=dict(_ADMIN_USER),
        db_pool=object(),
    )
    assert res.media_type == "application/pdf"
    assert res.headers["content-disposition"] == 'inline; filename="visa-permit.pdf"'
    assert res.headers["x-content-type-options"] == "nosniff"

    # Metadata oversize check must reject before calling stream()
    meta_oversize_client = _FakeStreamingHttpClient(
        {
            "mimeType": "application/pdf",
            "name": "huge.pdf",
            "size": str(documents_proxy_router._MAX_PROXY_DOWNLOAD_BYTES + 1),
            "parents": [],
        },
        [b"never"],
    )
    monkeypatch.setattr(documents_proxy_router.httpx, "AsyncClient", lambda **_k: meta_oversize_client)
    with pytest.raises(HTTPException) as exc_meta:
        await documents_proxy_router.proxy_drive_file(
            "1MUzGL73pcbY",
            current_user=dict(_ADMIN_USER),
            db_pool=object(),
        )
    assert exc_meta.value.status_code == 413
    assert meta_oversize_client.stream_called is False

    # Streaming chunk overflow must abort cleanly with 413
    half_plus = (documents_proxy_router._MAX_PROXY_DOWNLOAD_BYTES // 2) + 10
    stream_oversize_client = _FakeStreamingHttpClient(
        {"mimeType": "application/pdf", "name": "stream-huge.pdf", "parents": []},
        [b"a" * half_plus, b"b" * half_plus],
    )
    monkeypatch.setattr(documents_proxy_router.httpx, "AsyncClient", lambda **_k: stream_oversize_client)
    with pytest.raises(HTTPException) as exc_stream:
        await documents_proxy_router.proxy_drive_file(
            "1MUzGL73pcbY",
            current_user=dict(_ADMIN_USER),
            db_pool=object(),
        )
    assert exc_stream.value.status_code == 413


# ---------------------------------------------------------------------------
# Fly --workers 1 Hardening: Zero /tmp SA keys, asyncio.to_thread, MAX_BYTES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_sheets_service_uses_in_memory_sa_info_singleton_and_to_thread(
    monkeypatch: Any,
) -> None:
    assert "NamedTemporaryFile" not in inspect.getsource(sheets_service_module)

    sa_payload = json.dumps({"type": "service_account", "project_id": "proj-1", "client_email": "sa-id"})
    encoded = base64.b64encode(sa_payload.encode("utf-8")).decode("utf-8")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", encoded)

    svc = sheets_service_module.SheetsService()
    mock_google_svc = MagicMock()
    mock_google_svc.spreadsheets().values().get().execute.return_value = {"values": [["ok"]]}

    with (
        patch.object(
            sheets_service_module.service_account.Credentials,
            "from_service_account_info",
            return_value=MagicMock(),
        ) as mock_from_info,
        patch.object(
            sheets_service_module.service_account.Credentials,
            "from_service_account_file",
            side_effect=AssertionError("must never write SA JSON to /tmp"),
        ),
        patch.object(sheets_service_module, "build", return_value=mock_google_svc),
        patch.object(sheets_service_module.asyncio, "to_thread", wraps=asyncio.to_thread) as spy_thread,
    ):
        rows = await svc.read_range("sheet-id", "A1:A1")

    assert rows == [["ok"]]
    mock_from_info.assert_called_once()
    assert mock_from_info.call_args.args[0]["project_id"] == "proj-1"
    spy_thread.assert_awaited_once()

    monkeypatch.setattr(sheets_service_module, "_sheets_service_singleton", None)
    assert sheets_router._get_sheets_service() is sheets_router._get_sheets_service()


@pytest.mark.asyncio
async def test_runtime_intel_and_document_processing_and_accounting_offload_to_thread(
    monkeypatch: Any,
) -> None:
    # 1. intel._assess_items offloads _score_item_with_gemini_sync
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-api-key")
    state: dict[str, Any] = {
        "limit": 5,
        "item_type": "all",
        "min_confidence": 0.7,
        "pending_items": [{"id": "item-1", "type": "article", "title": "Update"}],
        "assessments": [],
        "notified_ids": [],
        "summary": "",
        "errors": [],
    }
    with (
        patch.object(
            intel_chain,
            "_score_item_with_gemini_sync",
            return_value={"significant": True, "confidence": 0.9, "reason": "Relevant"},
        ) as mock_score,
        patch.object(intel_chain.asyncio, "to_thread", wraps=asyncio.to_thread) as spy_intel_thread,
    ):
        out = await intel_chain._assess_items(state, None)  # type: ignore[arg-type]
    assert out["assessments"][0]["significant"] is True
    mock_score.assert_called_once()
    spy_intel_thread.assert_awaited_once()

    # 2. DocumentOCR keeps every PyMuPDF (fitz) call on the event-loop thread:
    #    fitz is not thread-safe, so it must never be handed to asyncio.to_thread.
    loop_thread = threading.get_ident()
    fitz_threads: list[int] = []

    fake_page = MagicMock()
    fake_page.get_text.return_value = "Visa text"
    fake_doc = MagicMock()
    fake_doc.__len__.return_value = 1
    fake_doc.__iter__.return_value = iter([fake_page])

    def _open_text_doc(*_args: Any, **_kwargs: Any) -> MagicMock:
        fitz_threads.append(threading.get_ident())
        return fake_doc

    with (
        patch.object(document_processing_module, "PDF_VISION_AVAILABLE", False),
        patch.object(document_processing_module, "PYMUPDF_AVAILABLE", True),
        patch.object(document_processing_module, "fitz", create=True) as mock_fitz,
        patch.object(document_processing_module.asyncio, "to_thread", wraps=asyncio.to_thread) as spy_ocr_thread,
    ):
        mock_fitz.open.side_effect = _open_text_doc
        ocr_res = await document_processing_module.DocumentOCR._extract_from_pdf(b"%PDF")
    assert ocr_res["success"] is True
    assert ocr_res["text"] == "Visa text"
    spy_ocr_thread.assert_not_awaited()

    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(png, format="PNG")
    fake_pix = MagicMock()
    fake_pix.tobytes.return_value = png.getvalue()
    render_page = MagicMock()
    render_page.get_pixmap.return_value = fake_pix
    render_doc = MagicMock()
    render_doc.__len__.return_value = 2
    render_doc.load_page.return_value = render_page

    def _open_render_doc(*_args: Any, **_kwargs: Any) -> MagicMock:
        fitz_threads.append(threading.get_ident())
        return render_doc

    with (
        patch.object(document_processing_module, "PYMUPDF_AVAILABLE", True),
        patch.object(document_processing_module, "fitz", create=True) as mock_fitz,
        patch.object(
            document_processing_module.DocumentOCR,
            "_gemini_vision_ocr",
            new_callable=AsyncMock,
            return_value="Page text",
        ),
        patch.object(document_processing_module.asyncio, "to_thread", wraps=asyncio.to_thread) as spy_render_thread,
    ):
        mock_fitz.open.side_effect = _open_render_doc
        vision_res = await document_processing_module.DocumentOCR._extract_pdf_via_vision(
            b"%PDF", MagicMock()
        )
    assert vision_res["success"] is True
    assert vision_res["pages"] == 2
    assert vision_res["text"] == "Page text\nPage text"
    assert render_doc.load_page.call_count == 2
    spy_render_thread.assert_not_awaited()
    assert fitz_threads == [loop_thread, loop_thread]

    # 3. accounting.import_cashout_pdf rejects >8 MB with 413 and offloads parse_cashout_pdf_with_weeks
    oversized_pdf = _FakeUpload(
        "cashout.pdf",
        b"%PDF",
        size=accounting_router._MAX_CASHOUT_PDF_BYTES + 1,
    )
    with pytest.raises(HTTPException) as exc_acc:
        await accounting_router.import_cashout_pdf(
            file=oversized_pdf,  # type: ignore[arg-type]
            current_user=dict(_ADMIN_USER),
            db_pool=_FakePool(),  # type: ignore[arg-type]
        )
    assert exc_acc.value.status_code == 413
    assert oversized_pdf.read_calls == []

    valid_pdf = _FakeUpload("cashout.pdf", b"%PDF-1.4")
    with (
        patch.object(
            accounting_router.cashout_import_service,
            "parse_cashout_pdf_with_weeks",
            return_value=[{"client": "A"}],
        ) as mock_parse,
        patch.object(
            accounting_router.cashout_import_service,
            "persist_cashout_rows",
            new_callable=AsyncMock,
            return_value={"imported": 1, "weeks": ["6 MARCH 2026"], "deleted": 0},
        ),
        patch.object(accounting_router.asyncio, "to_thread", wraps=asyncio.to_thread) as spy_acc_thread,
    ):
        imp_res = await accounting_router.import_cashout_pdf(
            file=valid_pdf,  # type: ignore[arg-type]
            current_user=dict(_ADMIN_USER),
            db_pool=_FakePool(),  # type: ignore[arg-type]
        )
    assert imp_res["imported"] == 1
    mock_parse.assert_called_once()
    spy_acc_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_upload_endpoints_enforce_max_bytes_and_media_offload(tmp_path: Path, monkeypatch: Any) -> None:
    # 1. team_drive.upload_file -> 413
    with patch.object(team_drive_router, "check_write_permission", new_callable=AsyncMock, return_value=True):
        td_upload = _FakeUpload("huge.bin", b"x", size=team_drive_router._MAX_DRIVE_UPLOAD_BYTES + 1)
        with pytest.raises(HTTPException) as exc_td:
            await team_drive_router.upload_file(
                file=td_upload,  # type: ignore[arg-type]
                parent_id="root",
                current_user={"user_id": "staff-1"},
                drive=MagicMock(),
                pool=object(),
            )
        assert exc_td.value.status_code == 413

    # 2. zoho_email.upload_attachment -> 413
    with patch.object(zoho_email_router, "_get_email_service", return_value=MagicMock()):
        zoho_upload = _FakeUpload("huge.zip", b"x", size=zoho_email_router._MAX_ATTACHMENT_BYTES + 1)
        with pytest.raises(HTTPException) as exc_zoho:
            await zoho_email_router.upload_attachment(
                file=zoho_upload,  # type: ignore[arg-type]
                current_user={"id": "staff-1"},
                db_pool=object(),  # type: ignore[arg-type]
            )
        assert exc_zoho.value.status_code == 413

    # 3. audio.transcribe_audio -> 413
    audio_upload = _FakeUpload("huge.mp3", b"x", size=audio_router._MAX_AUDIO_UPLOAD_BYTES + 1)
    with pytest.raises(HTTPException) as exc_audio:
        await audio_router.transcribe_audio(
            file=audio_upload,  # type: ignore[arg-type]
            audio_service=MagicMock(),
            api_key_verified=True,
        )
    assert exc_audio.value.status_code == 413

    # 4. media.upload_file -> rejects active extensions with 400, >25MB stream with 413, offloads to thread
    for bad_ext in ("xss.svg", "payload.html"):
        bad_media = _FakeUpload(bad_ext, b"<svg/>", content_type="image/svg+xml")
        with pytest.raises(HTTPException) as exc_bad_ext:
            await media_router.upload_file(file=bad_media)  # type: ignore[arg-type]
        assert exc_bad_ext.value.status_code == 400

    monkeypatch.chdir(tmp_path)
    with patch.object(media_router, "_MAX_MEDIA_UPLOAD_BYTES", 10):
        oversized_stream = _FakeUpload("photo.png", b"0123456789ABCDEF", size=None, content_type="image/png")
        with pytest.raises(HTTPException) as exc_media_413:
            await media_router.upload_file(file=oversized_stream)  # type: ignore[arg-type]
        assert exc_media_413.value.status_code == 413
        assert list((tmp_path / "static" / "uploads").glob("*")) == []

    good_media = _FakeUpload("photo.png", b"pngbytes", size=8, content_type="image/png")
    with patch.object(media_router.asyncio, "to_thread", wraps=asyncio.to_thread) as spy_media_thread:
        media_res = await media_router.upload_file(file=good_media)  # type: ignore[arg-type]
    assert media_res["success"] is True
    spy_media_thread.assert_awaited_once()
