"""Creating a client's Drive folder is a write: it goes through the write rule of the card.

The access check is the real one; the database rows it and the route read, the pool
validation and the Drive service are faked. Invented values only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import backend.app.routers.crm_clients as clients_module
import backend.app.utils.crm_utils as crm_utils
import backend.services.integrations.service_account_drive_service as drive_module
from backend.app.routers.crm_clients import ensure_drive_folder

_OWNER = {"email": "owner.one@example.test", "role": "consultant"}
_COLLEAGUE = {"email": "colleague.two@example.test", "role": "consultant"}
_ADMIN = {"email": "admin.three@example.test", "role": "admin"}
_CLIENT = 900001


class _Acquired:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The Drive service the route builds; returns its one call."""
    ensure = AsyncMock(
        return_value={
            "success": True,
            "created": True,
            "root_folder_id": "folder-synthetic",
            "root_folder_url": "https://drive.example.test/folder-synthetic",
            "subfolders": {},
        }
    )
    service = MagicMock()
    service.ensure_client_folder = ensure
    monkeypatch.setattr(drive_module, "ServiceAccountDriveService", lambda: service)
    return ensure


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    async def fetchrow(sql: str, *_: object) -> dict[str, Any]:
        if "assigned_to" in sql:
            return {"id": _CLIENT, "assigned_to": _OWNER["email"], "created_by": None}
        return {
            "id": _CLIENT,
            "full_name": "Zz Client One",
            "client_type": "individual",
            "google_drive_folder_id": None,
        }

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    monkeypatch.setattr(crm_utils, "ensure_client_pools_validated", AsyncMock())

    async def no_invalidation(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(clients_module, "invalidate_cache", no_invalidation)
    made = MagicMock()
    made.acquire = MagicMock(return_value=_Acquired(conn))
    return made


async def _ensure_as(user: dict[str, str] | None, pool: MagicMock) -> dict[str, Any]:
    return await ensure_drive_folder(
        request=MagicMock(), client_id=_CLIENT, db_pool=pool, current_user=user
    )


@pytest.mark.asyncio
async def test_a_colleague_who_does_not_hold_the_client_creates_no_folder(
    pool: MagicMock, drive: AsyncMock
) -> None:
    with pytest.raises(HTTPException) as refused:
        await _ensure_as(_COLLEAGUE, pool)

    assert refused.value.status_code == 403
    drive.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [_OWNER, _ADMIN], ids=["the assigned owner", "an admin"])
async def test_who_may_edit_the_client_may_create_its_folder(
    pool: MagicMock, drive: AsyncMock, user: dict[str, str]
) -> None:
    result = await _ensure_as(user, pool)

    assert result["created"] is True and result["folder_id"] == "folder-synthetic"
    drive.assert_awaited_once()
    assert drive.await_args.kwargs["client_id"] == _CLIENT


@pytest.mark.asyncio
async def test_the_internal_caller_is_not_asked_for_a_user(
    pool: MagicMock, drive: AsyncMock
) -> None:
    """A service call authenticated by its key carries no user: it passes as before."""
    result = await _ensure_as(None, pool)

    assert result["created"] is True
    drive.assert_awaited_once()
