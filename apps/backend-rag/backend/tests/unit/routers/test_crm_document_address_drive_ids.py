"""The Drive ids a client document's address fields name are checked like its file id.

The Drive proxy reads the run that follows `/d/` or `?id=` / `&id=` in a stored
`file_url` or `google_drive_file_url` as that row's Drive file. Every write route of
`crm_enhanced_documents.py` (create, bulk, archive, PATCH) runs the client ownership
check on each such run before the row is written.
"""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import HTTPException

from backend.app.routers.crm_enhanced import DocumentCreate, DocumentUpdate
from backend.app.routers.crm_enhanced_documents import (
    ClientArchiveDocumentCreate,
    create_document,
    create_documents_bulk,
    register_archive_document,
    update_document,
)

MODULE = "backend.app.routers.crm_enhanced_documents"
OWN = "ownDriveFile0000000001"
FOREIGN = "foreignDriveFile000002"
ADMIN = {"id": "user-1", "email": "admin@example.test", "role": "admin"}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def pool():
    pool = MagicMock()
    conn = AsyncMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    conn.transaction = MagicMock(return_value=_Tx())
    pool.conn = conn
    return pool


def _guard():
    async def guard(file_id, client_id, conn):
        if file_id != OWN:
            raise HTTPException(
                status_code=403, detail="Drive file is not in this client's own folder tree"
            )

    return AsyncMock(side_effect=guard)


@pytest.mark.parametrize("field", ["file_url", "google_drive_file_url"])
@pytest.mark.asyncio
async def test_create_refuses_a_foreign_id_in_an_address_field(pool, field):
    data = DocumentCreate(
        document_type="passport",
        **{field: f"https://drive.google.com/file/d/{FOREIGN}/view"},
    )
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        pytest.raises(HTTPException) as exc,
    ):
        await create_document(
            client_id=1,
            data=data,
            background_tasks=MagicMock(),
            pool=pool,
            current_user=ADMIN,
        )

    assert exc.value.status_code == 403
    assert guard.await_args_list == [call(FOREIGN, 1, pool.conn)]
    pool.conn.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_create_refuses_a_foreign_address_beside_an_own_file_id(pool):
    data = DocumentCreate(
        document_type="passport",
        file_id=OWN,
        google_drive_file_url=f"https://drive.google.com/file/d/{OWN}/view",
        file_url=f"https://drive.google.com/open?id={FOREIGN}",
    )
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        pytest.raises(HTTPException) as exc,
    ):
        await create_document(
            client_id=1,
            data=data,
            background_tasks=MagicMock(),
            pool=pool,
            current_user=ADMIN,
        )

    assert exc.value.status_code == 403
    assert guard.await_args_list == [call(OWN, 1, pool.conn), call(FOREIGN, 1, pool.conn)]
    pool.conn.fetchval.assert_not_called()


@pytest.mark.parametrize(
    ("file_id", "google_drive_file_url", "file_url", "checked"),
    [
        (OWN, f"https://drive.google.com/file/d/{OWN}/view", None, [OWN]),
        (None, None, "https://files.example.invalid/report.pdf", []),
    ],
)
@pytest.mark.asyncio
async def test_create_with_own_or_no_drive_addresses_is_written(
    pool, file_id, google_drive_file_url, file_url, checked
):
    data = DocumentCreate(
        document_type="other",
        file_id=file_id,
        google_drive_file_url=google_drive_file_url,
        file_url=file_url,
    )
    pool.conn.fetchval.return_value = 42
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        patch(f"{MODULE}.invalidate_cache", new=AsyncMock()),
        patch(f"{MODULE}._dispatch_ocr_by_folder", new=AsyncMock()),
        patch(
            "backend.services.portal.portal_notification_service.PortalNotificationService",
            side_effect=Exception("skip"),
        ),
    ):
        result = await create_document(
            client_id=1,
            data=data,
            background_tasks=MagicMock(),
            pool=pool,
            current_user=ADMIN,
        )

    assert result["success"] is True
    assert result["id"] == 42
    assert guard.await_args_list == [call(drive_id, 1, pool.conn) for drive_id in checked]


@pytest.mark.asyncio
async def test_bulk_fails_only_the_item_whose_address_names_a_foreign_file(pool):
    documents = [
        DocumentCreate(
            document_type="other",
            google_drive_file_url=f"https://drive.google.com/file/d/{OWN}/view",
        ),
        DocumentCreate(
            document_type="other",
            file_url=f"https://drive.google.com/open?id={FOREIGN}",
        ),
    ]
    pool.conn.fetchval.return_value = 7
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        patch(f"{MODULE}.invalidate_cache", new=AsyncMock()),
    ):
        result = await create_documents_bulk(
            client_id=1,
            documents=documents,
            background_tasks=MagicMock(),
            pool=pool,
            current_user=ADMIN,
        )

    assert result["inserted"] == 1
    assert result["document_ids"] == [7]
    assert result["failed"] == 1
    assert pool.conn.fetchval.await_count == 1
    assert guard.await_args_list == [call(OWN, 1, pool.conn), call(FOREIGN, 1, pool.conn)]


@pytest.mark.asyncio
async def test_archive_refuses_a_foreign_id_in_its_address(pool):
    data = ClientArchiveDocumentCreate(
        file_id=OWN,
        document_type="passport",
        google_drive_file_url=f"https://drive.google.com/open?id={FOREIGN}",
        client_visible=False,
    )
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        pytest.raises(HTTPException) as exc,
    ):
        await register_archive_document(7, data, pool, ADMIN)

    assert exc.value.status_code == 403
    assert guard.await_args_list == [call(OWN, 7, pool.conn), call(FOREIGN, 7, pool.conn)]
    pool.conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_archive_with_its_own_address_is_registered(pool):
    data = ClientArchiveDocumentCreate(
        file_id=OWN,
        document_type="passport",
        google_drive_file_url=f"https://drive.google.com/file/d/{OWN}/view",
        client_visible=False,
    )
    pool.conn.fetchval.return_value = 11
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
    ):
        result = await register_archive_document(7, data, pool, ADMIN)

    assert result["id"] == 11
    assert guard.await_args_list == [call(OWN, 7, pool.conn)]


@pytest.mark.parametrize("field", ["file_url", "google_drive_file_url"])
@pytest.mark.asyncio
async def test_patch_refuses_an_address_that_names_a_foreign_file(pool, field):
    data = DocumentUpdate(**{field: f"https://drive.google.com/file/d/{FOREIGN}/edit"})
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        pytest.raises(HTTPException) as exc,
    ):
        await update_document(client_id=1, doc_id=10, data=data, pool=pool, current_user=ADMIN)

    assert exc.value.status_code == 403
    assert guard.await_args_list == [call(FOREIGN, 1, pool.conn)]
    pool.conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_patch_of_an_address_without_a_drive_id_is_written(pool):
    data = DocumentUpdate(file_url="https://files.example.invalid/scan.pdf")
    pool.conn.execute.return_value = "UPDATE 1"
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        patch(f"{MODULE}.invalidate_cache", new=AsyncMock()),
    ):
        result = await update_document(
            client_id=1, doc_id=10, data=data, pool=pool, current_user=ADMIN
        )

    assert result["success"] is True
    guard.assert_not_awaited()
    pool.conn.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "address",
    [
        f"https://drive.google.com/file/d/{OWN}/view?usp=sharing&ouid=123456789012345678901",
        f"https://docs.google.com/spreadsheets/d/{OWN}/edit#gid=1234567890123",
    ],
)
@pytest.mark.asyncio
async def test_a_copy_link_and_a_sheets_address_are_written_with_one_check(pool, address):
    data = DocumentCreate(document_type="other", file_id=OWN, google_drive_file_url=address)
    pool.conn.fetchval.return_value = 42
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        patch(f"{MODULE}.invalidate_cache", new=AsyncMock()),
        patch(f"{MODULE}._dispatch_ocr_by_folder", new=AsyncMock()),
        patch(
            "backend.services.portal.portal_notification_service.PortalNotificationService",
            side_effect=Exception("skip"),
        ),
    ):
        result = await create_document(
            client_id=1, data=data, background_tasks=MagicMock(), pool=pool, current_user=ADMIN
        )

    assert result["success"] is True
    assert guard.await_args_list == [call(OWN, 1, pool.conn)]


@pytest.mark.parametrize("digits", ["12345678901", "98765432109"])
@pytest.mark.asyncio
async def test_an_all_digit_run_is_checked_like_any_run(pool, digits):
    data = DocumentCreate(
        document_type="other", file_url=f"https://x.example.test/open?id={digits}"
    )
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        pytest.raises(HTTPException) as exc,
    ):
        await create_document(
            client_id=1, data=data, background_tasks=MagicMock(), pool=pool, current_user=ADMIN
        )

    assert exc.value.status_code == 403
    assert guard.await_args_list == [call(digits, 1, pool.conn)]


@pytest.mark.asyncio
async def test_archive_with_an_empty_file_id_is_refused_and_writes_nothing(pool):
    data = ClientArchiveDocumentCreate(file_id="", document_type="passport", client_visible=False)
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        pytest.raises(HTTPException) as exc,
    ):
        await register_archive_document(7, data, pool, ADMIN)

    assert exc.value.status_code == 400
    pool.conn.execute.assert_not_called()
    pool.conn.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_archive_with_an_empty_file_id_and_an_address_is_refused_too(pool):
    data = ClientArchiveDocumentCreate(
        file_id="",
        document_type="passport",
        google_drive_file_url=f"https://drive.google.com/file/d/{OWN}/view",
        client_visible=False,
    )
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        pytest.raises(HTTPException),
    ):
        await register_archive_document(7, data, pool, ADMIN)

    assert guard.await_args_list[0] == call("", 7, pool.conn)
    pool.conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_patch_checks_a_run_the_addresses_name_beside_the_file_id(pool):
    data = DocumentUpdate(
        file_id=OWN, file_url=f"https://drive.google.com/open?id={FOREIGN}&ouid=1"
    )
    guard = _guard()
    with (
        patch(f"{MODULE}.verify_client_access", new=AsyncMock()),
        patch(f"{MODULE}.assert_drive_file_belongs_to_client", new=guard),
        pytest.raises(HTTPException) as exc,
    ):
        await update_document(client_id=1, doc_id=10, data=data, pool=pool, current_user=ADMIN)

    assert exc.value.status_code == 403
    assert guard.await_args_list == [call(OWN, 1, pool.conn), call(FOREIGN, 1, pool.conn)]
    pool.conn.execute.assert_not_called()
