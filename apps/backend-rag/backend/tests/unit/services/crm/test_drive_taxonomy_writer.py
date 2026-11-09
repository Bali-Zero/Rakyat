from unittest.mock import AsyncMock

import pytest

from backend.services.crm.drive_taxonomy import (
    canonical_folders,
    canonicalize_folder,
    folder_for_category,
    standard_subfolder_paths,
)
from backend.services.integrations.service_account_drive_service import ServiceAccountDriveService


@pytest.fixture(autouse=True)
def _physical_drive_roots(monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "gdrive_individuals_folder_id", "individuals")
    monkeypatch.setattr(settings, "gdrive_companies_folder_id", "companies")


def test_canonical_taxonomy_and_confluent_paths():
    assert canonical_folders("individual") == (
        "01_Profile",
        "02_Immigration",
        "03_Company_and_Tax",
        "04_Archive",
    )
    assert canonical_folders("company") == (
        "01_Legal_and_Licensing",
        "02_Tax_Returns_and_Payments",
        "03_Finance_and_Banking",
        "99_Archive",
    )
    assert canonicalize_folder("04_Family", "individual") == "01_Profile"
    assert (
        canonicalize_folder("03_DICHIARAZIONI_E_RICEVUTE_FISCALI", "company")
        == "02_Tax_Returns_and_Payments"
    )
    assert folder_for_category("family") == folder_for_category("personal")
    assert folder_for_category("tax") == folder_for_category("pma")
    assert folder_for_category("tax", "company") == "02_Tax_Returns_and_Payments"
    assert folder_for_category("company") == folder_for_category("pma")
    assert folder_for_category("company", "company") == folder_for_category("pma", "company")
    for kind in ("individual", "company"):
        paths = standard_subfolder_paths(kind)
        assert len(paths) == len(set(paths))
        assert tuple(p for p in paths if "/" not in p) == canonical_folders(kind)


def make_service(items):
    service = ServiceAccountDriveService.__new__(ServiceAccountDriveService)
    service.find_folder = AsyncMock(
        side_effect=lambda name, parent: next((x for x in items if x["name"] == name), None)
    )

    async def create(name, parent_id):
        item = {
            "id": "new",
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
            "trashed": False,
        }
        items.append(item)
        return item

    service.create_folder = AsyncMock(side_effect=create)
    service.get_file_metadata = AsyncMock(
        side_effect=lambda file_id: next(x for x in items if x["id"] == file_id)
    )
    return service


@pytest.mark.asyncio
async def test_canonical_preferred_and_legacy_left_alone():
    items = [
        {"id": "old", "name": "00_Profile"},
        {
            "id": "canonical",
            "name": "01_Profile",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
            "trashed": False,
        },
    ]
    service = make_service(items)
    assert (
        await service.resolve_canonical_folder("01_Profile", "root", conn=AsyncMock())
        == "canonical"
    )
    service.create_folder.assert_not_awaited()
    assert len(items) == 2


@pytest.mark.asyncio
async def test_legacy_only_creates_canonical_and_retry_reuses_it():
    items = [{"id": "old", "name": "00_Profile"}]
    service = make_service(items)
    assert await service.resolve_canonical_folder("01_Profile", "root", conn=AsyncMock()) == "new"
    assert await service.resolve_canonical_folder("01_Profile", "root", conn=AsyncMock()) == "new"
    service.create_folder.assert_awaited_once()
    assert items[0]["name"] == "00_Profile"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {
            "name": "00_Profile",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
            "trashed": False,
        },
        {
            "name": "01_Profile",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["elsewhere"],
            "trashed": False,
        },
        {
            "name": "01_Profile",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
            "trashed": True,
        },
        {
            "name": "01_Profile",
            "mimeType": "application/vnd.google-apps.shortcut",
            "parents": ["root"],
            "trashed": False,
        },
    ],
)
async def test_bad_cache_fails_closed(metadata):
    service = make_service([dict(id="cached", **metadata)])
    with pytest.raises(RuntimeError):
        await service.resolve_canonical_folder(
            "01_Profile", "root", cached_id="cached", conn=AsyncMock()
        )
    service.create_folder.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_names_fail_closed():
    service = ServiceAccountDriveService.__new__(ServiceAccountDriveService)

    class Request:
        def execute(self):
            return {"files": [{"id": "a"}, {"id": "b"}]}

    service.service = type(
        "API", (), {"files": lambda self: self, "list": lambda self, **kwargs: Request()}
    )()
    with pytest.raises(RuntimeError):
        await service.find_folder("01_Profile", "root")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["individual", "company"])
async def test_provisioning_creates_four_canonical_parents_and_retries_without_twins(kind):
    service = ServiceAccountDriveService.__new__(ServiceAccountDriveService)
    folders = []

    async def find(name, parent):
        matches = [item for item in folders if item["name"] == name and item["parents"] == [parent]]
        assert len(matches) <= 1
        return matches[0] if matches else None

    async def create(name, parent_id):
        item = {
            "id": str(len(folders) + 1),
            "name": name,
            "parents": [parent_id],
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
        }
        folders.append(item)
        return item

    service.find_folder = AsyncMock(side_effect=find)
    service.create_folder = AsyncMock(side_effect=create)
    service.get_file_metadata = AsyncMock(
        side_effect=lambda file_id: next(x for x in folders if x["id"] == file_id)
    )
    first = await service._create_standard_subfolders(
        "root", client_id=1, client_type=kind, conn=AsyncMock()
    )
    count = len(folders)
    second = await service._create_standard_subfolders(
        "root", client_id=1, client_type=kind, conn=AsyncMock()
    )
    assert first == second
    assert len(folders) == count
    assert tuple(x["name"] for x in folders if x["parents"] == ["root"]) == canonical_folders(kind)
    assert set(first) == set(standard_subfolder_paths(kind))


@pytest.mark.asyncio
async def test_portal_refuses_to_upload_when_cached_destination_cannot_be_verified():
    from backend.services.portal._mixins.documents import PortalDocumentsMixin

    portal = PortalDocumentsMixin()
    conn = AsyncMock()
    conn.fetchval.return_value = "invalid-cache"
    conn.fetchrow.return_value = {"google_drive_folder_id": "root", "client_type": "individual"}
    drive = AsyncMock()
    drive.get_file_metadata.return_value = {
        "id": "root",
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False,
        "parents": ["individuals"],
    }
    drive.resolve_canonical_folder.side_effect = RuntimeError("invalid cache")
    with pytest.raises(RuntimeError):
        await portal._resolve_client_drive_folder(
            conn, drive, 1, "Example", "01_Profile", "personal"
        )
    drive.resolve_canonical_folder.assert_awaited_once_with(
        "01_Profile", "root", "invalid-cache", conn=conn
    )
    drive.upload_file_to_folder.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_cache_does_not_hide_duplicate_destination():
    items = [
        {
            "id": "cached",
            "name": "01_Profile",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
            "trashed": False,
        }
    ]
    service = make_service(items)
    service.find_folder = AsyncMock(side_effect=RuntimeError("duplicate"))
    with pytest.raises(RuntimeError, match="duplicate"):
        await service.resolve_canonical_folder("01_Profile", "root", "cached", conn=AsyncMock())
    service.create_folder.assert_not_awaited()


@pytest.mark.asyncio
async def test_shortcut_with_canonical_name_is_rejected():
    service = ServiceAccountDriveService.__new__(ServiceAccountDriveService)

    class Request:
        def execute(self):
            return {
                "files": [{"id": "shortcut", "mimeType": "application/vnd.google-apps.shortcut"}]
            }

    service.service = type(
        "API", (), {"files": lambda self: self, "list": lambda self, **kwargs: Request()}
    )()
    with pytest.raises(RuntimeError, match="physical folder"):
        await service.find_folder("01_Profile", "root")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {
            "name": "00_Profile",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
            "trashed": False,
        },
        {
            "name": "01_Profile",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["other"],
            "trashed": False,
        },
        {
            "name": "01_Profile",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
            "trashed": True,
        },
        {
            "name": "01_Profile",
            "mimeType": "application/vnd.google-apps.shortcut",
            "parents": ["root"],
            "trashed": False,
        },
    ],
)
async def test_oauth_cached_folder_must_be_active_physical_child(bad):
    from backend.services.portal._mixins.documents import PortalDocumentsMixin

    drive = AsyncMock()
    drive.get_file.return_value = {"id": "cached", **bad}
    with pytest.raises(RuntimeError, match="Invalid OAuth"):
        await PortalDocumentsMixin()._resolve_oauth_folder(
            drive, "01_Profile", "root", "cached", conn=AsyncMock()
        )
    drive.create_folder.assert_not_awaited()


@pytest.mark.asyncio
async def test_oauth_legacy_folder_creates_canonical_and_does_not_change_legacy():
    from backend.services.portal._mixins.documents import PortalDocumentsMixin

    drive = AsyncMock()
    old = {"id": "old", "name": "00_Profile", "mimeType": "application/vnd.google-apps.folder"}
    canonical = {
        "id": "new",
        "name": "01_Profile",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["root"],
        "trashed": False,
    }
    drive.list_files.side_effect = [
        {"files": [old], "next_page_token": None},
        {"files": [old, canonical], "next_page_token": None},
    ]
    drive.get_file.return_value = canonical
    assert (
        await PortalDocumentsMixin()._resolve_oauth_folder(
            drive, "01_Profile", "root", conn=AsyncMock()
        )
        == "new"
    )
    drive.create_folder.assert_awaited_once_with(
        user_id="SYSTEM", name="01_Profile", parent_id="root"
    )
    drive.delete_file.assert_not_awaited()
    drive.move_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_oauth_detects_duplicates_across_pagination():
    from backend.services.portal._mixins.documents import PortalDocumentsMixin

    drive = AsyncMock()
    drive.list_files.side_effect = [
        {"files": [{"id": "a", "name": "01_Profile"}], "next_page_token": "next"},
        {"files": [{"id": "b", "name": "01_Profile"}], "next_page_token": None},
    ]
    with pytest.raises(RuntimeError, match="Ambiguous"):
        await PortalDocumentsMixin()._resolve_oauth_folder(
            drive, "01_Profile", "root", conn=AsyncMock()
        )
    drive.create_folder.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_sa_folder_creation_serializes_and_returns_one_id():
    import asyncio

    class LockDB:
        def __init__(self):
            self.locks = {}

        def connection(self):
            db = self

            class Conn:
                async def execute(self, sql, lock_class, key):
                    lock = db.locks.setdefault((lock_class, key), asyncio.Lock())
                    if "pg_advisory_unlock" in sql:
                        lock.release()
                    else:
                        await lock.acquire()

            return Conn()

    items = []
    service = make_service(items)
    original = service.create_folder.side_effect

    async def slow_create(name, parent_id):
        await asyncio.sleep(0.02)
        return await original(name, parent_id)

    service.create_folder.side_effect = slow_create
    db = LockDB()
    first, second = await asyncio.gather(
        service.resolve_canonical_folder("01_Profile", "root", conn=db.connection()),
        service.resolve_canonical_folder("01_Profile", "root", conn=db.connection()),
    )
    assert first == second == "new"
    service.create_folder.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_writer", ["oauth", "guardian"])
async def test_sa_and_other_writers_share_same_folder_lock(other_writer, monkeypatch):
    import asyncio

    from backend.services.crm_guardian import consolidator
    from backend.services.portal._mixins.documents import PortalDocumentsMixin

    locks = {}

    class Conn:
        async def execute(self, sql, lock_class, key):
            lock = locks.setdefault((lock_class, key), asyncio.Lock())
            if "pg_advisory_unlock" in sql:
                lock.release()
            else:
                await lock.acquire()

    items = []
    service = make_service(items)
    original = service.create_folder.side_effect

    async def slow_create(name, parent_id):
        await asyncio.sleep(0.02)
        return await original(name, parent_id)

    service.create_folder.side_effect = slow_create
    oauth = AsyncMock()
    oauth.get_file.side_effect = lambda _user, file_id: next(x for x in items if x["id"] == file_id)
    oauth.list_files.side_effect = lambda *_args, **_kwargs: {
        "files": list(items),
        "next_page_token": None,
    }
    oauth.create_folder.side_effect = lambda **kw: service.create_folder(
        kw["name"], kw["parent_id"]
    )
    monkeypatch.setattr(consolidator, "_drive_list_children", lambda _drive, _parent: list(items))

    def guardian_create(_drive, name, parent_id):
        folder = {
            "id": "new",
            "name": name,
            "parents": [parent_id],
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
        }
        items.append(folder)
        return folder

    monkeypatch.setattr(consolidator, "_drive_create_folder", guardian_create)
    first = service.resolve_canonical_folder("01_Profile", "root", conn=Conn())
    if other_writer == "oauth":
        second = PortalDocumentsMixin()._resolve_oauth_folder(
            oauth, "01_Profile", "root", conn=Conn()
        )
    else:

        async def guardian():
            folder, _ = await consolidator._guardian_resolve_folder(
                Conn(), None, "01_Profile", "root"
            )
            return folder["id"]

        second = guardian()
    assert await asyncio.gather(first, second) == ["new", "new"]
    assert len(items) == 1
    service.create_folder.assert_awaited_once()


@pytest.mark.asyncio
async def test_sa_lookup_follows_partial_pages_before_deciding_ambiguity():
    service = ServiceAccountDriveService.__new__(ServiceAccountDriveService)
    calls = []

    class Request:
        def __init__(self, params):
            self.params = params

        def execute(self):
            calls.append(self.params)
            if "pageToken" not in self.params:
                return {"files": [], "nextPageToken": "next"}
            return {"files": [{"id": "only", "mimeType": "application/vnd.google-apps.folder"}]}

    service.service = type(
        "API", (), {"files": lambda self: self, "list": lambda self, **kw: Request(kw)}
    )()
    assert (await service.find_folder("01_Profile", "root"))["id"] == "only"
    assert calls[1]["pageToken"] == "next"
