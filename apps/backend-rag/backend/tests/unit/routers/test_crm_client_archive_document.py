"""Archive registration of an existing Drive file: no OCR, no portal message, explicit visibility."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.routers.crm_enhanced_documents import (
    ClientArchiveDocumentCreate,
    register_archive_document,
)

MOD = "backend.app.routers.crm_enhanced_documents"
ADMIN = {"email": "Zero@balizero.com", "role": "admin"}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _pool(existing_id=None):
    pool = MagicMock()
    conn = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    conn.transaction = MagicMock(return_value=_Tx())
    # first fetchval = dedup probe, second = INSERT ... RETURNING id
    conn.fetchval = AsyncMock(side_effect=[existing_id, 901])
    return pool, conn


def _body(**kw):
    base = {"file_id": "drive-1", "document_type": "passport", "client_visible": False}
    return ClientArchiveDocumentCreate(**{**base, **kw})


@pytest.fixture
def guards():
    with (
        patch(f"{MOD}.verify_client_access", new=AsyncMock()) as access,
        patch(f"{MOD}.assert_drive_file_belongs_to_client", new=AsyncMock()) as owner,
        patch(f"{MOD}.invalidate_cache", new=AsyncMock()) as cache,
        patch(f"{MOD}._dispatch_ocr_by_folder", new=AsyncMock()) as ocr,
        patch(f"{MOD}.spawn") as spawn,
        patch(
            "backend.services.portal.portal_notification_service.PortalNotificationService"
        ) as portal,
    ):
        yield MagicMock(
            access=access, owner=owner, cache=cache, ocr=ocr, spawn=spawn, portal=portal
        )


def _inserts(conn):
    return [c.args for c in conn.fetchval.await_args_list if "INSERT INTO documents" in c.args[0]]


def _sql_calls(conn):
    return [c.args[0] for c in conn.fetchval.await_args_list + conn.execute.await_args_list]


@pytest.mark.asyncio
async def test_non_admin_is_403_before_the_database(guards):
    pool, conn = _pool()
    with pytest.raises(HTTPException) as exc:
        await register_archive_document(7, _body(), pool, {"email": "x@y.z", "role": "staff"})
    assert exc.value.status_code == 403
    pool.acquire.assert_not_called()
    assert not _inserts(conn)


@pytest.mark.asyncio
async def test_new_file_inserts_once_hidden_with_no_side_effects(guards):
    pool, conn = _pool()
    result = await register_archive_document(7, _body(), pool, ADMIN)

    assert result == {"id": 901, "success": True, "deduplicated": False, "ocr_triggered": False}
    inserts = _inserts(conn)
    assert len(inserts) == 1
    sql, *args = inserts[0]
    assert "'active'" in sql and "'google_drive'" in sql
    assert args[0] == 7 and args[4] == "drive-1"
    assert args[7] == "zero@balizero.com"
    assert args[8] is False
    guards.owner.assert_awaited_once_with("drive-1", 7, conn)
    guards.ocr.assert_not_called()
    guards.spawn.assert_not_called()
    guards.portal.assert_not_called()
    assert not any("clients" in s.lower().replace("client_id", "") for s in _sql_calls(conn))
    assert not any("UPDATE" in s for s in _sql_calls(conn))
    guards.cache.assert_awaited_once_with("zantara:crm_clients_stats:*")


@pytest.mark.asyncio
async def test_explicit_visible_true_is_written_as_true(guards):
    pool, conn = _pool()
    await register_archive_document(7, _body(client_visible=True), pool, ADMIN)
    assert _inserts(conn)[0][9] is True


@pytest.mark.asyncio
async def test_existing_row_is_returned_without_insert(guards):
    pool, conn = _pool(existing_id=55)
    result = await register_archive_document(7, _body(), pool, ADMIN)

    assert result == {"id": 55, "success": True, "deduplicated": True, "ocr_triggered": False}
    assert not _inserts(conn)
    lock = conn.execute.await_args_list[0].args
    assert "pg_advisory_xact_lock(742021" in lock[0] and lock[1] == "7:drive-1"
    guards.cache.assert_not_called()


@pytest.mark.asyncio
async def test_ownership_guard_403_propagates_without_insert(guards):
    pool, conn = _pool()
    guards.owner.side_effect = HTTPException(status_code=403, detail="foreign file")
    with pytest.raises(HTTPException) as exc:
        await register_archive_document(7, _body(), pool, ADMIN)
    assert exc.value.status_code == 403
    assert not _inserts(conn)
    conn.fetchval.assert_not_called()


def test_client_visible_is_required():
    with pytest.raises(ValidationError):
        ClientArchiveDocumentCreate(file_id="f", document_type="passport")


@pytest.mark.parametrize("bad", ["false", "true", 0, 1, None])
def test_client_visible_is_strict_bool(bad):
    with pytest.raises(ValidationError):
        ClientArchiveDocumentCreate(file_id="f", document_type="passport", client_visible=bad)
