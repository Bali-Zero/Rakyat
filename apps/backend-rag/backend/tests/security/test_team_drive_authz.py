"""Authorization on /api/drive (Team Drive), which acts as the Service Account.

Before: every route took only `get_current_user`, so a client-portal or
partner JWT reached the whole router, and any caller could share any Drive
file with any address (`POST /files/{id}/permissions`) or delete it for good.

After: the router requires a person on the team; sharing changes and permanent
deletion require the Board (or a super admin).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.routers.team_drive as team_drive
from backend.app.dependencies import get_current_user, get_database_pool


class _FakeDrive:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_configured(self) -> bool:
        return True

    async def add_permission(self, user_email: str, file_id: str, request: Any) -> dict[str, Any]:
        self.calls.append("add_permission")
        return {
            "id": "p1",
            "email": request.email,
            "name": "",
            "role": request.role,
            "type": "user",
        }

    async def update_permission(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("update_permission")
        return {"id": "p1", "email": "x@y.z", "name": "", "role": "reader", "type": "user"}

    async def remove_permission(self, **kwargs: Any) -> None:
        self.calls.append("remove_permission")

    async def delete_file(self, **kwargs: Any) -> None:
        self.calls.append(f"delete_file:{kwargs.get('permanent')}")


@pytest.fixture
def harness(monkeypatch):
    drive = _FakeDrive()
    state: dict[str, Any] = {"user": None, "board": False}

    async def _is_board(_email: str, _pool: Any) -> bool:
        return state["board"]

    monkeypatch.setattr(team_drive, "check_is_board", _is_board)
    monkeypatch.setattr(team_drive, "is_super_admin", lambda _u: False)
    monkeypatch.setattr(team_drive, "invalidate_cache", _noop_async)

    app = FastAPI()
    app.include_router(team_drive.router)
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    app.dependency_overrides[get_database_pool] = lambda: object()
    app.dependency_overrides[team_drive.get_drive] = lambda: drive
    return TestClient(app), drive, state


async def _noop_async(*_a: Any, **_k: Any) -> None:
    return None


def _user(role: str) -> dict[str, Any]:
    return {"email": f"{role.lower()}@example.com", "user_id": "1", "role": role, "permissions": []}


SHARE = (
    "POST",
    "/api/drive/files/F1/permissions",
    {"email": "uxxxxxx8@gmail.com", "role": "writer"},
)


@pytest.mark.parametrize("role", ["client", "partner", "monitoring", ""])
def test_non_team_callers_are_refused_on_the_router(harness, role) -> None:
    client, drive, state = harness
    state["user"] = _user(role)
    method, path, body = SHARE
    assert client.request(method, path, json=body).status_code == 403
    assert client.delete("/api/drive/files/F1").status_code == 403
    assert drive.calls == []


def test_team_member_cannot_change_sharing_or_delete_permanently(harness) -> None:
    client, drive, state = harness
    state["user"] = _user("Consultant")
    method, path, body = SHARE
    assert client.request(method, path, json=body).status_code == 403
    assert (
        client.patch("/api/drive/files/F1/permissions/P1", json={"role": "writer"}).status_code
        == 403
    )
    assert client.delete("/api/drive/files/F1/permissions/P1").status_code == 403
    assert client.delete("/api/drive/files/F1?permanent=true").status_code == 403
    assert drive.calls == []


def test_team_member_can_still_trash(harness) -> None:
    client, drive, state = harness
    state["user"] = _user("Consultant")
    assert client.delete("/api/drive/files/F1").status_code == 200
    assert drive.calls == ["delete_file:False"]


def test_board_member_can_manage_sharing_and_delete_permanently(harness) -> None:
    client, drive, state = harness
    state["user"] = _user("Consultant")
    state["board"] = True
    method, path, body = SHARE
    assert client.request(method, path, json=body).status_code == 200
    assert client.delete("/api/drive/files/F1/permissions/P1").status_code == 200
    assert client.delete("/api/drive/files/F1?permanent=true").status_code == 200
    assert drive.calls == ["add_permission", "remove_permission", "delete_file:True"]
