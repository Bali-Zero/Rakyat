"""Unit tests for CRM RBAC bypass closures and pool #402 coherence (Swarm 2 Track B)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


def _make_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn

    pool.acquire = acquire
    return pool


class TestAssignedToIntersectionBypassClosed:
    @pytest.mark.asyncio
    async def test_get_active_practices_disjoint_assigned_to_short_circuits(self) -> None:
        from backend.app.routers.crm_practices import get_active_practices

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"id": 99, "assigned_to": "colleague-beta"}])
        pool = _make_pool(conn)
        user = {"email": "owner-alpha", "role": "team"}

        with patch(
            "backend.app.routers.crm_practices.get_practices_user_filter",
            return_value=["owner-alpha"],
        ):
            result = await get_active_practices(
                request=MagicMock(),
                assigned_to="colleague-beta",
                db_pool=pool,
                current_user=user,
            )

        assert result == []
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_active_practices_pool_peer_case_insensitive_allowed(self) -> None:
        from backend.app.routers.crm_practices import get_active_practices

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"id": 12, "assigned_to": "peer-beta"}])
        pool = _make_pool(conn)
        user = {"email": "owner-alpha", "role": "team"}

        with patch(
            "backend.app.routers.crm_practices.get_practices_user_filter",
            return_value=["owner-alpha", "peer-beta"],
        ):
            result = await get_active_practices(
                request=MagicMock(),
                assigned_to="PEER-BETA",
                db_pool=pool,
                current_user=user,
            )

        assert len(result) == 1
        sql, owners_arg = conn.fetch.await_args.args
        assert "LOWER(assigned_to) = ANY" in sql
        assert owners_arg == ["peer-beta"]

    @pytest.mark.asyncio
    async def test_expiry_alerts_disjoint_assigned_to_short_circuits(self) -> None:
        from backend.app.routers.crm_enhanced_alerts import get_all_expiry_alerts

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"entity_id": 1}])
        pool = _make_pool(conn)
        user = {"email": "owner-alpha", "role": "team"}

        with patch(
            "backend.app.routers.crm_enhanced_alerts.get_practices_user_filter",
            return_value=["owner-alpha"],
        ):
            result = await get_all_expiry_alerts(
                alert_color=None,
                assigned_to="colleague-beta",
                limit=50,
                pool=pool,
                current_user=user,
            )

        assert result == []
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expiry_alerts_pool_peer_case_insensitive_allowed(self) -> None:
        from backend.app.routers.crm_enhanced_alerts import get_all_expiry_alerts

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"entity_id": 7, "assigned_to": "peer-beta"}])
        pool = _make_pool(conn)
        user = {"email": "owner-alpha", "role": "team"}

        with patch(
            "backend.app.routers.crm_enhanced_alerts.get_practices_user_filter",
            return_value=["owner-alpha", "peer-beta"],
        ):
            result = await get_all_expiry_alerts(
                alert_color=None,
                assigned_to="Peer-Beta",
                limit=50,
                pool=pool,
                current_user=user,
            )

        assert len(result) == 1
        sql, owners_arg, _limit = conn.fetch.await_args.args
        assert "LOWER(assigned_to) = ANY" in sql
        assert owners_arg == ["peer-beta"]


class TestCompanySyncDriveAdminGuard:
    @pytest.mark.asyncio
    async def test_sync_drive_rejects_non_admin_before_db_access(self) -> None:
        from backend.app.modules.crm.company_router import sync_company_drive_folder

        pool = MagicMock()
        pool.acquire = MagicMock(side_effect=AssertionError("DB must not be acquired"))

        with pytest.raises(HTTPException) as exc:
            await sync_company_drive_folder(
                company_id=5,
                db=pool,
                current_user={"email": "team-member", "role": "team"},
            )

        assert exc.value.status_code == 403


class TestPracticeOwnershipAndPool402:
    @pytest.mark.asyncio
    async def test_regenerate_invoice_denies_outsider_with_403(self) -> None:
        from backend.app.routers.crm_practices import regenerate_invoice

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "status": "sending_invoice",
                "created_by": "owner-alpha",
                "assigned_to": "owner-alpha",
            }
        )
        pool = _make_pool(conn)

        with patch(
            "backend.app.routers.crm_practices.caller_pool_peers",
            return_value={"outsider-gamma"},
        ):
            with pytest.raises(HTTPException) as exc:
                await regenerate_invoice(
                    request=MagicMock(),
                    practice_id=10,
                    current_user={"email": "outsider-gamma", "role": "team"},
                    db_pool=pool,
                )

        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_regenerate_invoice_allows_pool_peer(self) -> None:
        from backend.app.routers.crm_practices import regenerate_invoice

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "status": "sending_invoice",
                "created_by": "owner-alpha",
                "assigned_to": "owner-alpha",
            }
        )
        pool = _make_pool(conn)
        mock_svc = MagicMock()
        mock_svc.regenerate_invoice = AsyncMock(return_value={"success": True})

        with (
            patch(
                "backend.app.routers.crm_practices.caller_pool_peers",
                return_value={"peer-beta", "owner-alpha"},
            ),
            patch(
                "backend.app.routers.crm_practices.InvoiceAutomationService",
                return_value=mock_svc,
            ),
        ):
            res = await regenerate_invoice(
                request=MagicMock(),
                practice_id=10,
                current_user={"email": "peer-beta", "role": "team"},
                db_pool=pool,
            )

        assert res["success"] is True
        mock_svc.regenerate_invoice.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_required_documents_denies_outsider_with_403(self) -> None:
        from backend.app.routers.crm_practices import get_required_documents

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "assigned_to": "owner-alpha",
                "created_by": "owner-alpha",
                "client_lead": "owner-alpha",
            }
        )
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)

        with patch(
            "backend.app.routers.crm_practices.get_practices_user_filter",
            return_value=["outsider-gamma"],
        ):
            with pytest.raises(HTTPException) as exc:
                await get_required_documents(
                    practice_id=15,
                    current_user={"email": "outsider-gamma", "role": "team"},
                    db_pool=pool,
                )

        assert exc.value.status_code == 403
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_required_documents_allows_pool_peer(self) -> None:
        from backend.app.routers.crm_practices import get_required_documents

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "assigned_to": "OWNER-ALPHA",
                "created_by": None,
                "client_lead": None,
            }
        )
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)

        with patch(
            "backend.app.routers.crm_practices.get_practices_user_filter",
            return_value=["peer-beta", "owner-alpha"],
        ):
            rows = await get_required_documents(
                practice_id=15,
                current_user={"email": "peer-beta", "role": "team"},
                db_pool=pool,
            )

        assert rows == []
        conn.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upload_client_document_allows_pool_peer_and_checks_linked_client_sql(
        self,
    ) -> None:
        from backend.app.routers import crm_practices

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "id": 11,
                    "practice_id": 7,
                    "client_id": 42,
                    "client_name": "Client Synthetic",
                    "document_type": "passport",
                    "practice_created_by": "owner-alpha",
                    "practice_assigned_to": "owner-alpha",
                    "client_assigned_to": "owner-alpha",
                },
                {"id": None},
            ]
        )
        pool = _make_pool(conn)
        canonical_upload = AsyncMock(return_value={"success": True, "document_id": 808})
        req = crm_practices.ClientDocumentUploadRequest(
            required_doc_id=11, file="ZmlsZQ==", file_name="doc.pdf"
        )

        with (
            patch(
                "backend.app.routers.crm_practices.caller_pool_peers",
                return_value={"peer-beta", "owner-alpha"},
            ),
            patch.object(crm_practices, "upload_document_base64", canonical_upload, create=True),
            patch.object(crm_practices, "invalidate_cache", new=AsyncMock()),
            patch(
                "backend.app.routers.crm_practices.mark_required_slot_uploaded",
                new=AsyncMock(),
            ),
        ):
            res = await crm_practices.upload_client_document(
                practice_id=7,
                request=req,
                current_user={"email": "peer-beta", "role": "team"},
                db_pool=pool,
            )

        assert res["success"] is True
        second_query = conn.fetchrow.await_args_list[1].args[0]
        assert "linked_client_id" in second_query
        assert "deleted_at IS NULL" in second_query
        assert "LOWER(tm.email) = LOWER($1)" in second_query

    @pytest.mark.asyncio
    async def test_upload_client_document_denies_outsider_with_403(self) -> None:
        from backend.app.routers import crm_practices

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "id": 11,
                    "practice_id": 7,
                    "client_id": 42,
                    "client_name": "Client Synthetic",
                    "document_type": "passport",
                    "practice_created_by": "owner-alpha",
                    "practice_assigned_to": "owner-alpha",
                    "client_assigned_to": "owner-alpha",
                },
                {"id": None},
            ]
        )
        pool = _make_pool(conn)
        req = crm_practices.ClientDocumentUploadRequest(
            required_doc_id=11, file="ZmlsZQ==", file_name="doc.pdf"
        )

        with patch(
            "backend.app.routers.crm_practices.caller_pool_peers",
            return_value={"outsider-gamma"},
        ):
            with pytest.raises(HTTPException) as exc:
                await crm_practices.upload_client_document(
                    practice_id=7,
                    request=req,
                    current_user={"email": "outsider-gamma", "role": "team"},
                    db_pool=pool,
                )

        assert exc.value.status_code == 403
