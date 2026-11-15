"""Approver scope on the leave list and the HR-admin wall around financial data (reorganisation 2026-11-09).

An approver who is not an HR admin sees their own requests plus those of their direct
reports (hr_leave_routing), may review exactly those, and gets no HR admin rights.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.routers import hr as hr_router
from backend.app.routers.hr import (
    _require_can_review_leave,
    list_employees,
    list_leave_requests,
    list_payroll_periods,
)
from backend.app.services.hr.hr_leave_routing import reports_of
from backend.app.utils.hr_utils import is_hr_admin

ANGEL = "angel.tax@balizero.com"
ADIT = "adit@balizero.com"
ARI = "ari.firda@balizero.com"
ASYA = "asya@balizero.com"
KADEK = "kadek.tax@balizero.com"
KRISNA = "krisna@balizero.com"
RUSLANA = "ruslana@balizero.com"
RINA = "rina@balizero.com"
RIO = "rio@balizero.com"
DEA = "dea@balizero.com"
CANDRA = "candra@balizero.com"
ALL_EMAILS = (ANGEL, ADIT, ARI, ASYA, KADEK, KRISNA, RUSLANA, RINA, RIO, DEA, CANDRA)


def _row(rid: int, email: str, status: str = "pending") -> dict[str, Any]:
    return {"id": rid, "employee_email": email, "employee_name": "x", "status": status}


def _service(rows: list[dict[str, Any]]) -> Any:
    """Fake service: the scoped query is emulated from the arguments it receives."""
    service = AsyncMock()
    service.get_employee_by_email = AsyncMock(side_effect=lambda e: {"id": ALL_EMAILS.index(e) + 1})

    async def scoped(own_id, emails, status=None):
        lowered = {e.lower() for e in emails}
        return [
            r
            for r in rows
            if (own_id is not None and ALL_EMAILS.index(r["employee_email"]) + 1 == own_id)
            or r["employee_email"] in lowered
        ]

    async def unscoped(employee_id, status=None):
        if employee_id is None:
            return list(rows)
        return [r for r in rows if ALL_EMAILS.index(r["employee_email"]) + 1 == employee_id]

    service.list_leave_requests_scoped = AsyncMock(side_effect=scoped)
    service.list_leave_requests = AsyncMock(side_effect=unscoped)
    return service


ROWS = [
    _row(1, KADEK),
    _row(2, KRISNA),
    _row(3, ARI),
    _row(4, ASYA),
    _row(5, ADIT),
    _row(6, ANGEL),
]


async def _list(email: str, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    service = _service(ROWS if rows is None else rows)
    with patch.object(hr_router, "_get_hr_service", return_value=service):
        out = await list_leave_requests(
            employee_id=None,
            status=None,
            current_user={"email": email, "role": "member"},
            db_pool=MagicMock(),
        )
    return out["requests"]


# One pending request per person, ids follow ALL_EMAILS order (Angel=1 ... Candra=11).
ROWS_ALL = [_row(i + 1, e) for i, e in enumerate(ALL_EMAILS)]
ID_OF = {e: i + 1 for i, e in enumerate(ALL_EMAILS)}


class TestReportsOf:
    def test_the_two_approvers_and_their_reports(self) -> None:
        assert KADEK in reports_of(ANGEL)
        assert ANGEL not in reports_of(ANGEL)
        assert {ARI, ASYA, KRISNA} <= reports_of(ADIT)
        assert KADEK not in reports_of(ADIT)

    def test_a_plain_member_approves_nobody(self) -> None:
        assert reports_of(KADEK) == frozenset()
        assert reports_of("") == frozenset()


class TestApproverListScope:
    @pytest.mark.asyncio
    async def test_tax_supervisor_sees_tax_reports_and_can_review_them(self) -> None:
        rows = await _list(ANGEL)
        by_id = {r["id"]: r for r in rows}
        assert set(by_id) == {1, 6}  # Kadek's request and his own
        assert by_id[1]["can_review"] is True and by_id[1]["is_mine"] is False
        assert by_id[6]["can_review"] is False and by_id[6]["is_mine"] is True

    @pytest.mark.asyncio
    async def test_tax_supervisor_never_sees_setup_requests_or_the_direktur(self) -> None:
        ids = {r["id"] for r in await _list(ANGEL)}
        assert ids.isdisjoint({2, 3, 4, 5})

    @pytest.mark.asyncio
    async def test_direktur_is_an_hr_admin_and_sees_everyone_but_never_reviews_his_own(
        self,
    ) -> None:
        # Owner decision 2026-11-10: the Direktur is an HR admin, so the admin view applies.
        rows = await _list(ADIT)
        by_id = {r["id"]: r for r in rows}
        assert set(by_id) == {1, 2, 3, 4, 5, 6}
        assert all(by_id[i]["can_review"] for i in (1, 2, 3, 4, 6))
        assert by_id[5]["can_review"] is False  # his own

    @pytest.mark.asyncio
    async def test_a_plain_member_sees_only_their_own_and_reviews_nothing(self) -> None:
        rows = await _list(KADEK)
        assert [r["id"] for r in rows] == [1]
        assert rows[0]["can_review"] is False

    @pytest.mark.asyncio
    async def test_a_decided_request_is_not_reviewable(self) -> None:
        service = _service([_row(1, KADEK, status="approved")])
        with patch.object(hr_router, "_get_hr_service", return_value=service):
            out = await list_leave_requests(
                employee_id=None,
                status=None,
                current_user={"email": ANGEL, "role": "member"},
                db_pool=MagicMock(),
            )
        assert out["requests"][0]["can_review"] is False

    @pytest.mark.asyncio
    async def test_hr_admin_view_is_unchanged_everything_visible(self) -> None:
        rows = await _list("zero@balizero.com")
        assert {r["id"] for r in rows} == {1, 2, 3, 4, 5, 6}


class TestRuslanaLineListScope:
    """Owner order 2026-11-15: Ruslana approves Rina, Rio, Dea and Candra; the department heads lose Rio, Dea, Candra."""

    def test_ruslana_reports_are_exactly_her_four(self) -> None:
        assert reports_of(RUSLANA) == frozenset({RINA, RIO, DEA, CANDRA})

    def test_department_heads_no_longer_have_the_three(self) -> None:
        assert not reports_of(ANGEL) & {DEA, CANDRA}
        assert RIO not in reports_of(ADIT)

    @pytest.mark.asyncio
    async def test_ruslana_sees_her_four_reports_and_her_own_nothing_else(self) -> None:
        rows = await _list(RUSLANA, ROWS_ALL)
        by_id = {r["id"]: r for r in rows}
        assert set(by_id) == {ID_OF[e] for e in (RUSLANA, RINA, RIO, DEA, CANDRA)}
        for e in (RINA, RIO, DEA, CANDRA):
            assert by_id[ID_OF[e]]["can_review"] is True
        assert by_id[ID_OF[RUSLANA]]["can_review"] is False  # her own

    @pytest.mark.asyncio
    async def test_tax_supervisor_scope_no_longer_contains_dea_or_candra(self) -> None:
        by_id = {r["id"]: r for r in await _list(ANGEL, ROWS_ALL)}
        assert set(by_id) == {ID_OF[ANGEL], ID_OF[KADEK]}
        assert ID_OF[DEA] not in by_id and ID_OF[CANDRA] not in by_id


class TestReviewEndpointsStayScoped:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "approver,requester",
        [(ANGEL, ADIT), (ANGEL, KRISNA), (KADEK, ARI)],
    )
    async def test_forged_request_id_outside_the_reports_is_refused(
        self, approver: str, requester: str
    ) -> None:
        service = AsyncMock()
        service.get_leave_request = AsyncMock(
            return_value={"id": 9, "requester_email": requester, "status": "pending"}
        )
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(service, {"email": approver, "role": "member"}, 9)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("approver", [ANGEL, ADIT])
    async def test_approver_cannot_review_their_own_request(self, approver: str) -> None:
        service = AsyncMock()
        service.get_leave_request = AsyncMock(
            return_value={"id": 9, "requester_email": approver, "status": "pending"}
        )
        with pytest.raises(HTTPException) as exc:
            await _require_can_review_leave(service, {"email": approver, "role": "member"}, 9)
        assert exc.value.status_code == 403


class TestApproversAreNotHrAdmins:
    @pytest.mark.parametrize("email", [ANGEL, ARI])
    def test_no_hr_admin_flag(self, email: str) -> None:
        assert is_hr_admin({"email": email, "role": "member"}) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("email", [ANGEL, KRISNA])
    async def test_financial_endpoints_refuse_an_approver(self, email: str) -> None:
        user = {"email": email, "role": "member"}
        for call in (
            lambda: list_employees(current_user=user, db_pool=MagicMock()),
            lambda: list_payroll_periods(current_user=user, db_pool=MagicMock()),
        ):
            with pytest.raises(HTTPException) as exc:
                await call()
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_the_list_payload_carries_no_financial_field(self) -> None:
        rows = await _list(ANGEL)
        banned = ("salary", "bank", "npwp", "payslip", "bonus", "ptkp", "bpjs")
        for r in rows:
            assert not [k for k in r if any(b in k.lower() for b in banned)], r.keys()


class TestDirekturHrAdminEndpoints:
    """Owner decision 2026-11-10: adit@ reaches the HR-admin endpoints; a setup address still does not."""

    @pytest.mark.asyncio
    async def test_direktur_reaches_the_employees_endpoint(self) -> None:
        service = AsyncMock()
        service.list_employees = AsyncMock(return_value=[])
        with patch.object(hr_router, "_get_hr_service", return_value=service):
            out = await list_employees(
                current_user={"email": ADIT, "role": "member"}, db_pool=MagicMock()
            )
        assert out["count"] == 0

    @pytest.mark.asyncio
    async def test_a_setup_member_is_still_refused(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await list_employees(
                current_user={"email": KRISNA, "role": "member"}, db_pool=MagicMock()
            )
        assert exc.value.status_code == 403
