"""Tests for HR leave RBAC helper — supervisor delegation + self-approval ban."""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.app.routers.hr import _require_can_review_leave
from backend.app.utils.hr_utils import is_hr_admin


def _mock_service_returning_request(requester_email: str) -> Any:
    service = AsyncMock()
    service.get_leave_request = AsyncMock(
        return_value={
            "id": 1,
            "requester_email": requester_email,
            "requester_name": "Test Employee",
            "status": "pending",
        }
    )
    return service


class TestRequireCanReviewLeave:
    # ─── HR admins (universal access) ────────────────────────────────
    @pytest.mark.asyncio
    async def test_zero_approves_anyone(self) -> None:
        service = _mock_service_returning_request("kadek.tax@balizero.com")
        req = await _require_can_review_leave(
            service,
            {"email": "zero@balizero.com", "role": "member"},
            1,
        )
        assert req["id"] == 1

    @pytest.mark.asyncio
    async def test_asya_approves_anyone(self) -> None:
        service = _mock_service_returning_request("kadek.tax@balizero.com")
        req = await _require_can_review_leave(
            service,
            {"email": "asya@balizero.com", "role": "member"},
            1,
        )
        assert req["id"] == 1

    @pytest.mark.asyncio
    async def test_ruslana_approves_her_direct_report_without_being_an_hr_admin(self) -> None:
        # Owner decision 2026-11-12: Ruslana is no longer an HR admin; her delegated
        # approval (hr_leave_routing: Rina -> Ruslana) is what lets her review Rina's leave.
        user = {"email": "ruslana@balizero.com", "role": "member"}
        assert is_hr_admin(user) is False
        service = _mock_service_returning_request("rina@balizero.com")
        req = await _require_can_review_leave(service, user, 1)
        assert req["id"] == 1

    @pytest.mark.asyncio
    async def test_ruslana_approves_dea_since_2026_11_15(self) -> None:
        service = _mock_service_returning_request("dea@balizero.com")
        req = await _require_can_review_leave(
            service, {"email": "ruslana@balizero.com", "role": "member"}, 1
        )
        assert req["id"] == 1

    @pytest.mark.asyncio
    async def test_ruslana_can_no_longer_review_someone_else_s_leave(self) -> None:
        service = _mock_service_returning_request("krisna@balizero.com")
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(
                service, {"email": "ruslana@balizero.com", "role": "member"}, 1
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requester", ["rio@balizero.com", "candra@balizero.com"])
    async def test_ruslana_approves_rio_and_candra_since_2026_11_15(self, requester: str) -> None:
        service = _mock_service_returning_request(requester)
        req = await _require_can_review_leave(
            service, {"email": "ruslana@balizero.com", "role": "member"}, 1
        )
        assert req["id"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requester", ["dea@balizero.com", "candra@balizero.com"])
    async def test_tax_supervisor_can_no_longer_review_dea_or_candra(self, requester: str) -> None:
        user = {"email": "angel.tax@balizero.com", "role": "member"}
        assert is_hr_admin(user) is False
        service = _mock_service_returning_request(requester)
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(service, user, 1)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_member_who_is_not_ruslana_cannot_review_rio(self) -> None:
        user = {"email": "krisna@balizero.com", "role": "member"}
        assert is_hr_admin(user) is False
        service = _mock_service_returning_request("rio@balizero.com")
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(service, user, 1)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_hr_admin_adit_may_review_dea_by_owner_decision_not_a_leak(self) -> None:
        # INTENDED behaviour (owner decision): adit@ is an HR extra admin, and HR admins
        # may review ANY leave, including the ones routed to Ruslana.
        user = {"email": "adit@balizero.com", "role": "member"}
        assert is_hr_admin(user) is True
        service = _mock_service_returning_request("dea@balizero.com")
        req = await _require_can_review_leave(service, user, 1)
        assert req["id"] == 1

    # ─── Supervisor delegation ───────────────────────────────────────
    @pytest.mark.asyncio
    async def test_tax_supervisor_approves_kadek(self) -> None:
        service = _mock_service_returning_request("kadek.tax@balizero.com")
        req = await _require_can_review_leave(
            service,
            {"email": "angel.tax@balizero.com", "role": "member"},
            1,
        )
        assert req["id"] == 1

    @pytest.mark.asyncio
    async def test_direktur_approves_krisna_as_setup_lead(self) -> None:
        # Reorganisation 2026-11-09: the Direktur leads the setup team.
        service = _mock_service_returning_request("krisna@balizero.com")
        req = await _require_can_review_leave(
            service,
            {"email": "adit@balizero.com", "role": "member"},
            1,
        )
        assert req["id"] == 1

    @pytest.mark.asyncio
    async def test_kadek_cannot_approve_angel(self) -> None:
        service = _mock_service_returning_request("angel.tax@balizero.com")
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(
                service,
                {"email": "kadek.tax@balizero.com", "role": "member"},
                1,
            )
        assert exc.value.status_code == 403

    # ─── Self-approval forbidden (even for HR admins) ────────────────
    @pytest.mark.asyncio
    async def test_asya_cannot_self_approve(self) -> None:
        service = _mock_service_returning_request("asya@balizero.com")
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(
                service,
                {"email": "asya@balizero.com", "role": "member"},
                1,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_zero_cannot_self_approve(self) -> None:
        service = _mock_service_returning_request("zero@balizero.com")
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(
                service,
                {"email": "zero@balizero.com", "role": "member"},
                1,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_ruslana_cannot_self_approve(self) -> None:
        service = _mock_service_returning_request("ruslana@balizero.com")
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(
                service,
                {"email": "ruslana@balizero.com", "role": "member"},
                1,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_retired_tax_login_can_no_longer_approve(self) -> None:
        service = _mock_service_returning_request("kadek.tax@balizero.com")
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(
                service,
                {"email": "tax@balizero.com", "role": "member"},
                1,
            )
        assert exc.value.status_code == 403

    # ─── Not found ───────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self) -> None:
        service = AsyncMock()
        service.get_leave_request = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(
                service,
                {"email": "zero@balizero.com"},
                999,
            )
        assert exc.value.status_code == 404

    # ─── Email normalization ─────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_user_email_case_insensitive(self) -> None:
        service = _mock_service_returning_request("kadek.tax@balizero.com")
        req = await _require_can_review_leave(
            service,
            {"email": "  ANGEL.TAX@BALIZERO.COM  ", "role": "member"},
            1,
        )
        assert req["id"] == 1
