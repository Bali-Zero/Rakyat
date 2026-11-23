"""The partner list's owner scope is decided once, by the administrator test.

The route runs here over the real service, the real repository and the real table, on the
test's own event loop. The administrator allowlist is set through the settings value the
administrator test reads. Invented rows only.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import backend.app.routers.partners as partners_module
from backend.app.core.config import settings
from backend.app.dependencies import get_current_user, get_database_pool
from backend.services.crm.partners.service import PartnersService

ADMIN_ADDRESS = "allowlisted.admin@example.test"
NAMES = ("Zz Admin Owned", "Zz Staff Owned", "Zz Unassigned", "Zz Team Owned")


class _OneConnection:
    """A pool that lends the test's own connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _OneConnection:
        return self

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def _listed(db_conn: Any, user: dict[str, Any], query: str = "") -> list[str]:
    application = FastAPI()
    application.include_router(partners_module.router)
    application.dependency_overrides[get_current_user] = lambda: user
    application.dependency_overrides[get_database_pool] = lambda: _OneConnection(db_conn)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://kita.test"
    ) as client:
        response = await client.get(f"/api/partners?page_size=200&{query}")
    assert response.status_code == 200, response.text
    return sorted(
        row["full_name"] for row in response.json()["partners"] if row["full_name"] in NAMES
    )


@pytest.fixture
async def people(db_conn: Any, partner_factory: Any, user_factory: Any, monkeypatch) -> dict:
    monkeypatch.setattr(settings, "admin_emails", ADMIN_ADDRESS)
    admin = await user_factory(email=ADMIN_ADDRESS, role="team")
    staff = await user_factory(role="Tax Lead")
    plain_team = await user_factory(role="team")
    await partner_factory(full_name=NAMES[0], assigned_to=str(admin))
    await partner_factory(full_name=NAMES[1], assigned_to=str(staff))
    await partner_factory(full_name=NAMES[2])
    await partner_factory(full_name=NAMES[3], assigned_to=str(plain_team))
    return {
        "admin_team": {"user_id": str(admin), "email": ADMIN_ADDRESS, "role": "team"},
        "admin_role": {"user_id": "admin-1", "email": "role.admin@example.test", "role": "admin"},
        "staff": {"user_id": str(staff), "email": str(staff), "role": "Tax Lead"},
        "staff_id": str(staff),
        "plain_team": {"user_id": str(plain_team), "email": str(plain_team), "role": "team"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["admin_team", "admin_role"])
async def test_an_administrator_of_any_role_keeps_the_filter_they_name(
    db_conn: Any, people: dict, who: str
) -> None:
    user = people[who]
    assert await _listed(db_conn, user) == sorted(NAMES)
    assert await _listed(db_conn, user, f"assigned_to={people['staff_id']}") == [NAMES[1]]
    assert await _listed(db_conn, user, "orphaned=true") == [NAMES[2]]


@pytest.mark.asyncio
async def test_a_job_title_staff_member_lists_only_their_own_partners(
    db_conn: Any, people: dict
) -> None:
    staff = people["staff"]
    assert await _listed(db_conn, staff) == [NAMES[1]]
    assert await _listed(db_conn, staff, f"assigned_to={people['admin_team']['user_id']}") == [
        NAMES[1]
    ]
    assert await _listed(db_conn, staff, "orphaned=true") == []


@pytest.mark.asyncio
async def test_a_non_admin_with_role_team_lists_only_their_own_partners(
    db_conn: Any, people: dict
) -> None:
    assert await _listed(db_conn, people["plain_team"]) == [NAMES[3]]


@pytest.mark.asyncio
async def test_the_service_cannot_be_asked_without_a_scope(db_conn: Any) -> None:
    with pytest.raises(TypeError):
        await PartnersService(db_conn).list_partners()
