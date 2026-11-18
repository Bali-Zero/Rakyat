"""Owner decision 2026-11-18: Dea and Candra join the LKPM team.

Real `_require_lkpm_staff` (not stubbed), in-memory service double, synthetic
identities only. Pins the staff gate, the history endpoint and the assign body.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from backend.app.routers import lkpm as lkpm_router

_NEW_MEMBERS = ["dea@balizero.com", "candra@balizero.com"]


def _identity(email: str, role: str = "Tax Consultant") -> dict:
    return {"role": role, "email": email, "user_id": "u-1"}


def _app(monkeypatch: pytest.MonkeyPatch, user: dict) -> FastAPI:
    service = SimpleNamespace(get_history=AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(lkpm_router.router)
    app.dependency_overrides[lkpm_router.get_current_user] = lambda: user
    app.dependency_overrides[lkpm_router.get_database_pool] = lambda: object()
    monkeypatch.setattr(lkpm_router, "_get_service", lambda _pool: service)
    return app


async def _get_history(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get("/api/v1/lkpm/history/1")


@pytest.mark.parametrize("email", _NEW_MEMBERS)
def test_new_members_are_lkpm_staff(email: str) -> None:
    assert lkpm_router._is_lkpm_staff(_identity(email))
    assert lkpm_router._is_lkpm_staff(_identity(email.upper()))


@pytest.mark.parametrize("email", _NEW_MEMBERS)
@pytest.mark.asyncio
async def test_new_members_get_200_on_history(monkeypatch: pytest.MonkeyPatch, email: str) -> None:
    resp = await _get_history(_app(monkeypatch, _identity(email)))
    assert resp.status_code == 200
    assert resp.json()["client_id"] == 1


@pytest.mark.parametrize("email", _NEW_MEMBERS)
def test_assign_body_accepts_new_members(email: str) -> None:
    assert lkpm_router.LKPMAssignBody(lkpm_assigned_to=email).lkpm_assigned_to == email


def test_assign_body_still_rejects_an_unrelated_address() -> None:
    with pytest.raises(ValidationError):
        lkpm_router.LKPMAssignBody(lkpm_assigned_to="someone.else@balizero.com")


@pytest.mark.asyncio
async def test_unrelated_staff_email_still_403(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _get_history(_app(monkeypatch, _identity("someone.else@balizero.com")))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_portal_client_still_gets_concealed_404(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _get_history(_app(monkeypatch, _identity("client@example.test", role="client")))
    assert resp.status_code == 404
