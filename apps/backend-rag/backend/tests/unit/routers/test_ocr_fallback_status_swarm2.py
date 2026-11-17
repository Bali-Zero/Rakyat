"""Unit tests for Cloud Vision OCR fallback and terminal documents.ocr_status (Swarm 2 Track D)."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn

    pool.acquire = acquire
    return pool


class TestNpwpNibCloudVisionFallback:
    @pytest.mark.asyncio
    async def test_extract_npwp_falls_back_to_gemini_when_ollama_unreachable(self) -> None:
        from backend.app.routers.crm_clients_documents import NpwpExtractRequest, extract_npwp

        conn = AsyncMock()
        pool = _make_pool(conn)
        req = NpwpExtractRequest(
            client_id=10,
            file=base64.b64encode(b"%PDF-1.4-fake").decode(),
            file_name="npwp_company.pdf",
        )

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=OSError("Connection refused"))
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        fake_genai = MagicMock()
        fake_genai.is_available = True
        fake_genai.generate_content = AsyncMock(
            return_value={
                "text": '{"npwp": "00-001", "address": "Synthetic Street", "city": "Denpasar"}'
            }
        )

        with (
            patch(
                "backend.app.routers.crm_clients_documents.verify_client_access",
                new=AsyncMock(),
            ),
            patch("httpx.AsyncClient", return_value=mock_http),
            patch(
                "backend.services.multimodal.cloud_vision_gate.cloud_vision_allowed",
                return_value=True,
            ),
            patch("backend.llm.genai_client.GENAI_AVAILABLE", True),
            patch("backend.llm.genai_client.get_genai_client", return_value=fake_genai),
            patch("backend.app.routers.crm_clients_documents.invalidate_cache", new=AsyncMock()),
        ):
            res = await extract_npwp(
                request=req,
                current_user={"email": "actor-alpha", "role": "team"},
                db_pool=pool,
            )

        assert res.success is True
        assert res.npwp == "00001"
        assert res.city == "Denpasar"
        call_contents = fake_genai.generate_content.await_args.kwargs["contents"]
        assert call_contents[1]["inline_data"]["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_extract_npwp_respects_cloud_vision_block_gate(self) -> None:
        from backend.app.routers.crm_clients_documents import NpwpExtractRequest, extract_npwp

        conn = AsyncMock()
        pool = _make_pool(conn)
        req = NpwpExtractRequest(
            client_id=10,
            file=base64.b64encode(b"fake-jpg").decode(),
            file_name="npwp.jpg",
        )

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=OSError("Ollama offline"))
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        blocked_spy = MagicMock()
        with (
            patch(
                "backend.app.routers.crm_clients_documents.verify_client_access",
                new=AsyncMock(),
            ),
            patch("httpx.AsyncClient", return_value=mock_http),
            patch(
                "backend.services.multimodal.cloud_vision_gate.cloud_vision_allowed",
                return_value=False,
            ),
            patch(
                "backend.services.multimodal.cloud_vision_gate.note_cloud_ocr_blocked",
                blocked_spy,
            ),
        ):
            res = await extract_npwp(
                request=req,
                current_user={"email": "actor-alpha", "role": "team"},
                db_pool=pool,
            )

        assert res.success is False
        assert "OCR_ALLOW_CLOUD_VISION=false" in (res.message or "")
        blocked_spy.assert_called_once_with("crm_clients_documents.extract_npwp")

    @pytest.mark.asyncio
    async def test_extract_nib_falls_back_to_gemini_on_ollama_503(self) -> None:
        from backend.app.routers.crm_clients_documents import NibExtractRequest, extract_nib

        conn = AsyncMock()
        pool = _make_pool(conn)
        req = NibExtractRequest(
            client_id=11,
            file=base64.b64encode(b"fake-png").decode(),
            file_name="nib.png",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        fake_genai = MagicMock()
        fake_genai.is_available = True
        fake_genai.generate_content = AsyncMock(
            return_value={
                "text": '{"nib": "009901", "company_name": "PT Synthetic", "kbli_code": "56101"}'
            }
        )

        with (
            patch(
                "backend.app.routers.crm_clients_documents.verify_client_access",
                new=AsyncMock(),
            ),
            patch("httpx.AsyncClient", return_value=mock_http),
            patch(
                "backend.services.multimodal.cloud_vision_gate.cloud_vision_allowed",
                return_value=True,
            ),
            patch("backend.llm.genai_client.GENAI_AVAILABLE", True),
            patch("backend.llm.genai_client.get_genai_client", return_value=fake_genai),
            patch("backend.app.routers.crm_clients_documents.invalidate_cache", new=AsyncMock()),
        ):
            res = await extract_nib(
                request=req,
                current_user={"email": "actor-alpha", "role": "team"},
                db_pool=pool,
            )

        assert res.success is True
        assert res.nib == "009901"
        assert res.kbli_code == "56101"
        call_contents = fake_genai.generate_content.await_args.kwargs["contents"]
        assert call_contents[1]["inline_data"]["mime_type"] == "image/png"


class TestTerminalOcrStatusTransitions:
    @pytest.mark.asyncio
    async def test_auto_ocr_passport_updates_document_status_completed_and_failed(
        self,
    ) -> None:
        from backend.app.routers import crm_enhanced

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"full_name": "SYNTHETIC PERSON"})
        conn.execute = AsyncMock()
        pool = _make_pool(conn)

        with (
            patch.object(
                crm_enhanced,
                "_download_drive_file",
                new=AsyncMock(return_value=(b"img", "image/jpeg")),
            ),
            patch.object(
                crm_enhanced,
                "_gemini_ocr",
                new=AsyncMock(
                    return_value='{"passport_number": "X0001", "full_name": "SYNTHETIC PERSON"}'
                ),
            ),
        ):
            ok = await crm_enhanced._auto_ocr_passport(pool, 10, "file-1", doc_id=901)

        assert ok["success"] is True
        doc_updates = [
            c.args for c in conn.execute.await_args_list if "UPDATE documents" in c.args[0]
        ]
        assert any("ocr_status = 'completed'" in sql and args[-1] == 901 for sql, *args in doc_updates)

        conn2 = AsyncMock()
        conn2.fetchrow = AsyncMock(return_value=None)
        conn2.execute = AsyncMock()
        pool2 = _make_pool(conn2)
        fail = await crm_enhanced._auto_ocr_passport(pool2, 10, "file-1", doc_id=902)
        assert fail["success"] is False
        sql, status_arg, doc_arg = conn2.execute.await_args.args
        assert "UPDATE documents SET ocr_status = $1" in sql
        assert status_arg == "failed"
        assert doc_arg == 902

    @pytest.mark.asyncio
    async def test_dispatcher_marks_skipped_on_unhandled_type_and_failed_on_error(
        self,
    ) -> None:
        from backend.services.documents.ocr_dispatcher_service import dispatch_ocr_by_folder

        conn_skip = AsyncMock()
        conn_skip.execute = AsyncMock()
        pool_skip = _make_pool(conn_skip)

        with patch(
            "backend.app.routers.crm_enhanced._auto_classify_content",
            new=AsyncMock(return_value={"document_type": "akta", "confidence": 0.95}),
        ):
            res_skip = await dispatch_ocr_by_folder(
                db_pool=pool_skip,
                client_id=10,
                file_id="f-1",
                folder_name="99_Misc",
                filename="deed.pdf",
                doc_id=701,
                document_type="akta",
            )

        assert res_skip["dispatched"] is False
        sql_s, st_s, id_s = conn_skip.execute.await_args.args
        assert "UPDATE documents SET ocr_status = $1" in sql_s
        assert st_s == "skipped"
        assert id_s == 701

        conn_fail = AsyncMock()
        conn_fail.execute = AsyncMock()
        pool_fail = _make_pool(conn_fail)

        with patch(
            "backend.app.routers.crm_enhanced._auto_ocr_passport",
            new=AsyncMock(return_value={"success": False, "error": "boom"}),
        ):
            res_fail = await dispatch_ocr_by_folder(
                db_pool=pool_fail,
                client_id=10,
                file_id="f-2",
                folder_name="00_Profile",
                filename="passport_scan.pdf",
                doc_id=702,
            )

        assert res_fail["dispatched"] is True
        sql_f, st_f, id_f = conn_fail.execute.await_args.args
        assert st_f == "failed"
        assert id_f == 702

    @pytest.mark.asyncio
    async def test_upload_document_base64_marks_skipped_when_no_drive_file(self) -> None:
        from backend.app.routers import crm_enhanced_documents

        conn = AsyncMock()
        tx_cm = MagicMock()
        tx_cm.__aenter__ = AsyncMock(return_value=None)
        tx_cm.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=tx_cm)
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 10,
                "full_name": "Synthetic Client",
                "google_drive_folder_id": "root-folder",
                "client_type": "individual",
                "phone": None,
                "phone_normalized": None,
            }
        )
        conn.fetchval = AsyncMock(return_value=555)
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        pool = _make_pool(conn)
        bg = MagicMock()

        drive_service = AsyncMock()
        drive_service.get_file_metadata.return_value = {
            "id": "root-folder",
            "name": "Root",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "parents": ["individuals"],
        }
        drive_service.resolve_canonical_folder.return_value = "sub-1"
        drive_service.upload_file_to_folder.return_value = {
            "id": None,
            "webViewLink": None,
        }

        req = crm_enhanced_documents.DocumentUploadBase64(
            file=base64.b64encode(b"hello").decode(),
            file_name="note.pdf",
            document_type="other",
            document_category="other",
        )

        with (
            patch.object(
                crm_enhanced_documents,
                "_active_duplicate_document_id",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                crm_enhanced_documents,
                "_documents_columns",
                new=AsyncMock(
                    return_value={
                        "client_id",
                        "document_type",
                        "document_category",
                        "file_name",
                        "status",
                        "storage_type",
                    }
                ),
            ),
            patch.object(
                crm_enhanced_documents,
                "physical_root_layout",
                return_value={},
            ),
            patch.object(
                crm_enhanced_documents,
                "folder_for_category",
                return_value=None,
            ),
            patch.object(
                crm_enhanced_documents,
                "ServiceAccountDriveService",
                return_value=drive_service,
            ),
            patch(
                "backend.app.routers.crm_enhanced_documents.invalidate_cache",
                new=AsyncMock(),
            ),
        ):
            res = await crm_enhanced_documents.upload_document_base64(
                client_id=10,
                data=req,
                background_tasks=bg,
                pool=pool,
                current_user={"email": "actor-alpha", "role": "team"},
                access_already_verified=True,
            )

        assert res["ocr_triggered"] is False
        bg.add_task.assert_not_called()
        assert any(
            "ocr_status = 'skipped'" in c.args[0] and c.args[1] == 555
            for c in conn.execute.await_args_list
        )
