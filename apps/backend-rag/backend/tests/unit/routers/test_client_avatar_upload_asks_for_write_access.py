"""Replacing a client's avatar is a write: it goes through the write rule of the client card.

The access check is the real one here (the older avatar tests replace it with a stub); only the
database row it reads, the pool validation and the object store are faked.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile

import backend.app.routers.crm_clients as clients_module
import backend.app.utils.crm_utils as crm_utils
import backend.services.canva_renderer_v2._tigris as tigris
from backend.app.routers.crm_clients import upload_client_avatar

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
def store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    fake.put_object = MagicMock(return_value={})
    monkeypatch.setattr(tigris, "get_s3_client", lambda: fake)
    monkeypatch.setattr(tigris, "BUCKET", "test-bucket")
    monkeypatch.setattr(tigris, "PUBLIC_HOST", "test-bucket.example.test")
    return fake


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    made = AsyncMock()
    made.fetchrow = AsyncMock(
        return_value={"id": _CLIENT, "assigned_to": _OWNER["email"], "created_by": None}
    )
    made.execute = AsyncMock(return_value=None)
    monkeypatch.setattr(crm_utils, "ensure_client_pools_validated", AsyncMock())

    async def no_invalidation(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(clients_module, "invalidate_cache", no_invalidation)
    return made


def _png() -> UploadFile:
    upload = UploadFile(filename="a.png", file=io.BytesIO(b"\x89PNG\r\n\x1a\nsynthetic"))
    object.__setattr__(upload, "headers", {"content-type": "image/png"})
    return upload


async def _upload_as(user: dict[str, str], conn: AsyncMock) -> dict[str, Any]:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquired(conn))
    return await upload_client_avatar(
        client_id=_CLIENT, file=_png(), db_pool=pool, current_user=user
    )


@pytest.mark.asyncio
async def test_a_colleague_who_does_not_hold_the_client_is_refused(
    conn: AsyncMock, store: MagicMock
) -> None:
    with pytest.raises(HTTPException) as refused:
        await _upload_as(_COLLEAGUE, conn)

    assert refused.value.status_code == 403
    store.put_object.assert_not_called()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [_OWNER, _ADMIN], ids=["the assigned owner", "an admin"])
async def test_who_may_edit_the_client_may_replace_its_avatar(
    conn: AsyncMock, store: MagicMock, user: dict[str, str]
) -> None:
    result = await _upload_as(user, conn)

    assert result["success"] is True
    store.put_object.assert_called_once()
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unknown_client_is_not_found(conn: AsyncMock, store: MagicMock) -> None:
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as missing:
        await _upload_as(_OWNER, conn)

    assert missing.value.status_code == 404
    store.put_object.assert_not_called()
