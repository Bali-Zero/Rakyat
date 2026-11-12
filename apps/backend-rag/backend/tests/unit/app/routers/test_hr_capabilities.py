"""The capabilities answer is produced by the predicates that guard the routes."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.routers import hr as hr_router
from backend.app.routers.hr import get_my_capabilities
from backend.app.utils.crm_utils import is_crm_admin
from backend.app.utils.hr_utils import is_hr_admin

HR_ONLY_ADMIN = "adit@balizero.com"
MEMBER = "krisna@balizero.com"


def _service(has_record: bool) -> Any:
    service = AsyncMock()
    service.get_employee_by_email = AsyncMock(return_value={"id": 1} if has_record else None)
    return service


async def _caps(email: str, has_record: bool = True, role: str = "member") -> dict[str, Any]:
    user = {"email": email, "role": role}
    with patch.object(hr_router, "_get_hr_service", return_value=_service(has_record)):
        return await get_my_capabilities(current_user=user, db_pool=MagicMock())


@pytest.mark.asyncio
async def test_member_sees_only_their_own_card():
    caps = await _caps(MEMBER)
    assert caps == {
        "hr_admin": False,
        "accounting_admin": False,
        "has_employee_record": True,
        "people_scope": None,
        "hr_edit": False,
    }


@pytest.mark.asyncio
async def test_hr_admin_gets_people_scope_all_and_edit():
    caps = await _caps(HR_ONLY_ADMIN)
    assert caps["people_scope"] == "all" and caps["hr_edit"] is True


@pytest.mark.asyncio
async def test_scoped_viewer_gets_scoped_without_edit():
    caps = await _caps("ruslana@balizero.com")
    assert caps["hr_admin"] is False
    assert caps["people_scope"] == "scoped" and caps["hr_edit"] is False


@pytest.mark.asyncio
async def test_a_client_never_gets_a_people_scope():
    caps = await _caps("ruslana@balizero.com", role="client")
    assert caps["people_scope"] is None and caps["hr_edit"] is False


@pytest.mark.asyncio
async def test_hr_only_admin_is_not_an_accounting_admin():
    caps = await _caps(HR_ONLY_ADMIN)
    assert caps["hr_admin"] is True
    assert caps["accounting_admin"] is False


@pytest.mark.asyncio
async def test_missing_employee_record_is_reported_not_raised():
    caps = await _caps(MEMBER, has_record=False)
    assert caps["has_employee_record"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "email,role", [(MEMBER, "member"), (HR_ONLY_ADMIN, "member"), (MEMBER, "admin")]
)
async def test_answer_matches_the_route_guards(email: str, role: str):
    user = {"email": email, "role": role}
    caps = await _caps(email, role=role)
    assert caps["hr_admin"] is is_hr_admin(user)
    assert caps["accounting_admin"] is is_crm_admin(user)
