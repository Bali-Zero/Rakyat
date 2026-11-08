"""Synthetic regressions for fail-closed automatic Drive ownership."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.core.config import settings
from backend.services.crm.drive_poll_ownership import (
    FOLDER_MIME,
    is_billing_file,
    ownership_maps,
    revalidate_poll_owner,
    validate_client_root,
)


@pytest.fixture(autouse=True)
def configured_roots(monkeypatch):
    monkeypatch.setattr(settings, "gdrive_individuals_folder_id", "individuals")
    monkeypatch.setattr(settings, "gdrive_companies_folder_id", "companies")


def client(cid=11, root="root", **kwargs):
    return {
        "id": cid,
        "google_drive_folder_id": root,
        "client_type": "individual",
        "deleted_at": None,
        **kwargs,
    }


def folder(cid=11, name="01_Profile", fid="profile"):
    return {"client_id": cid, "subfolder_name": name, "subfolder_id": fid}


def metadata(fid, name, parent):
    return {"id": fid, "name": name, "parents": [parent], "mimeType": FOLDER_MIME, "trashed": False}


@pytest.mark.parametrize("reverse", [False, True])
def test_distinct_historical_owners_never_choose_a_survivor(reverse):
    rows = [folder(), folder(12)]
    if reverse:
        rows.reverse()
    maps, _ = ownership_maps(rows, [client(), client(12, "old", deleted_at="archived")])
    assert maps == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"deleted_at": "archived"},
        {"status": "archived"},
        {"google_drive_folder_id": None},
        {"client_type": "invalid"},
    ],
)
def test_ineligible_clients_do_not_enter_maps(changes):
    assert ownership_maps([folder()], [client(**changes)]) == ({}, {})


def test_aliases_must_have_one_classification():
    rows = [folder(), folder(name="00_Profile")]
    assert ownership_maps(rows, [client()])[0] == {"profile": (11, "01_Profile")}
    assert ownership_maps(rows + [folder(name="02_Immigration")], [client()])[0] == {}


def test_shared_root_blocks_every_subfolder_even_with_deleted_alias():
    assert ownership_maps([folder()], [client(), client(12, deleted_at="archived")]) == ({}, {})


def valid_context(rows=None, refs=None):
    conn = AsyncMock()
    conn.fetch.side_effect = [
        rows if rows is not None else [client()],
        refs if refs is not None else [folder()],
    ]
    conn.fetchval.return_value = False
    drive = MagicMock()
    drive._parent_folder_for_client_type.return_value = "individuals"
    drive.get_file_metadata = AsyncMock(
        side_effect=[
            metadata("root", "Client", "individuals"),
            metadata("profile", "01_Profile", "root"),
        ]
    )
    return conn, drive


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [client(deleted_at="archived")],
        [client(status="archived")],
        [client(root="changed")],
        [client(), client(12, deleted_at="archived")],
        [client(client_type="invalid")],
    ],
)
async def test_revalidation_rejects_owner_changes_after_map_load(rows):
    assert ownership_maps([folder()], [client()])[0]
    conn, drive = valid_context(rows=rows)
    assert not await revalidate_poll_owner(conn, drive, 11, "root", "profile", "01_Profile")
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("refs", [[folder(), folder(12)], [folder(name="02_Immigration")]])
async def test_revalidation_rejects_new_alias_or_classification_conflict(refs):
    conn, drive = valid_context(refs=refs)
    assert not await revalidate_poll_owner(conn, drive, 11, "root", "profile", "01_Profile")


@pytest.mark.asyncio
async def test_valid_current_owner_passes():
    conn, drive = valid_context()
    assert await revalidate_poll_owner(conn, drive, 11, "root", "profile", "01_Profile")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["invoices", "practices"])
async def test_exact_billing_reference_excluded_from_either_store(source):
    conn = AsyncMock()

    async def lookup(sql, fid):
        assert "invoices WHERE drive_file_id = $1" in sql
        assert "documents->'invoice'->>'invoice_drive_id' = $1" in sql
        refs = {"invoices": {"opaque-a"}, "practices": {"opaque-b"}}
        return fid in refs[source]

    conn.fetchval.side_effect = lookup
    assert await is_billing_file(conn, "opaque-a" if source == "invoices" else "opaque-b")
    assert not await is_billing_file(conn, "Invoice_receipt.pdf")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "healthy",
        "company",
        "legacy-company",
        "archived",
        "changed-root",
        "new-alias",
        "billing-invoices",
        "billing-practices",
        "late-billing",
        "nested-conflict",
        "nested-healthy",
    ],
)
async def test_poll_write_boundary_and_nested_conflict(scenario):
    from backend.services.crm.drive_poll_service import _do_poll_drive_changes

    conn = AsyncMock()
    conn.fetchrow.return_value = {"value": "token"}
    nested = scenario.startswith("nested-")
    parent = "nested" if nested else "profile"
    corporate = scenario in ("company", "legacy-company")
    top_name = "01_Legal_and_Licensing" if scenario == "company" else "01_Profile"
    refs = [folder(name=top_name)]
    writes = []
    bill_checks = 0

    async def fetch(sql, *args):
        if "FROM client_drive_subfolders" in sql:
            return refs + ([folder(12)] if args and scenario == "new-alias" else [])
        if "FROM companies" in sql:
            return []
        if "FROM clients" in sql:
            if args and scenario == "archived":
                return [client(deleted_at="archived")]
            if args and scenario == "changed-root":
                return [client(root="moved")]
            return [client(client_type="company" if corporate else "individual")]
        raise AssertionError(sql)

    async def fetchval(sql, *args):
        nonlocal bill_checks
        if "FROM invoices" in sql:
            bill_checks += 1
            if scenario in ("billing-invoices", "billing-practices"):
                return True
            return scenario == "late-billing" and bill_checks > 1
        if "SELECT EXISTS" in sql:
            return False
        if "INSERT INTO client_drive_subfolders" in sql:
            assert "ON CONFLICT (client_id, subfolder_name)" in sql
            assert "WHERE client_drive_subfolders.subfolder_id = EXCLUDED.subfolder_id" in sql
            writes.append("nested")
            if scenario == "nested-conflict":
                return None
            refs.append(folder(fid="nested", name="Attachments"))
            return "nested"
        if "INSERT INTO documents" in sql:
            writes.append("document")
            return 900
        if "SELECT id FROM documents" in sql:
            return None
        raise AssertionError(sql)

    conn.fetch.side_effect = fetch
    conn.fetchval.side_effect = fetchval
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool.close = AsyncMock()
    drive = MagicMock()
    drive._parent_folder_for_client_type.return_value = "individuals"
    drive.list_changes_since = AsyncMock(
        return_value={
            "changes": [
                {
                    "file": {
                        "id": "scan",
                        "name": "scan.pdf",
                        "mimeType": "application/pdf",
                        "parents": [parent],
                    }
                }
            ],
            "new_page_token": "next",
        }
    )
    metadata_by_id = {
        "root": metadata("root", "Client", "companies" if scenario == "company" else "individuals"),
        "profile": metadata("profile", top_name, "root"),
        "nested": metadata("nested", "Attachments", "profile"),
        "scan": {"id": "scan", "size": "0", "parents": [parent], "trashed": False},
    }
    drive.get_file_metadata = AsyncMock(side_effect=lambda fid: metadata_by_id[fid])
    with (
        patch(
            "backend.services.crm.drive_poll_service.asyncpg.create_pool",
            AsyncMock(return_value=pool),
        ),
        patch(
            "backend.services.integrations.service_account_drive_service.ServiceAccountDriveService",
            return_value=drive,
        ),
        patch("backend.services.crm.drive_poll_service._enqueue_guardian_client", AsyncMock()),
        patch("backend.services.crm.drive_poll_service.invalidate_crm_stats", AsyncMock()),
        patch.dict("os.environ", {"DATABASE_URL": "postgresql://test/ownership"}),
    ):
        result = await _do_poll_drive_changes(inline_ocr=False, acquire_advisory_lock=False)
    assert result["processed"] == (
        1 if scenario in ("healthy", "nested-healthy", "company", "legacy-company") else 0
    )
    assert ("document" in writes) == (
        scenario in ("healthy", "nested-healthy", "company", "legacy-company")
    )
    if scenario.startswith("billing-"):
        drive.get_file_metadata.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"parents": ["foreign-root"]},
        {"mimeType": "application/vnd.google-apps.shortcut"},
        {"trashed": True},
        {"parents": ["root", "foreign-root"]},
    ],
)
async def test_stale_or_nonphysical_subfolder_is_rejected(changes):
    conn, drive = valid_context()
    drive.get_file_metadata.side_effect = [
        metadata("root", "Client", "individuals"),
        {**metadata("profile", "01_Profile", "root"), **changes},
        {**metadata("foreign-root", "Foreign", "individuals"), "parents": []},
    ]
    assert not await revalidate_poll_owner(conn, drive, 11, "root", "profile", "01_Profile")


@pytest.mark.asyncio
async def test_current_owner_checked_again_after_drive_requests():
    conn, drive = valid_context()
    conn.fetchval.return_value = True
    assert not await revalidate_poll_owner(conn, drive, 11, "root", "profile", "01_Profile")
    sql, path, cid, root, client_type = conn.fetchval.call_args.args
    assert "deleted_at IS NULL" in sql
    assert "google_drive_folder_id = $3" in sql
    assert (cid, root) == (11, "root")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows", [[client(status="archived")], [client(deleted_at="deleted")], [client(), client(12)]]
)
async def test_physical_layout_does_not_relax_owner_checks(rows):
    conn, drive = valid_context(rows=rows)
    with pytest.raises(ValueError):
        await validate_client_root(conn, drive, 11, "root")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["prospect", "lead", "completed"])
async def test_nonarchived_business_states_remain_eligible(status):
    conn, drive = valid_context(rows=[client(status=status)])
    assert ownership_maps([folder()], [client(status=status)])[0]
    assert await revalidate_poll_owner(conn, drive, 11, "root", "profile", "01_Profile")


@pytest.mark.asyncio
async def test_rejects_root_outside_configured_trees():
    conn, drive = valid_context(rows=[client(client_type="company")])
    drive.get_file_metadata = AsyncMock(return_value=metadata("root", "Client", "foreign-parent"))
    with pytest.raises(ValueError, match="topology"):
        await validate_client_root(conn, drive, 11, "root")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent,folder_name",
    [
        ("companies", "01_Legal_and_Licensing"),
        ("companies", "00_PROFIL_E_FONDAMENTA_AZIENDALI"),
        ("individuals", "01_Profile"),
        ("individuals", "00_Profile"),
    ],
)
async def test_active_company_customer_uses_existing_physical_taxonomy(parent, folder_name):
    refs = [folder(name=folder_name)]
    conn, drive = valid_context(rows=[client(client_type="company")], refs=refs)
    drive.get_file_metadata.side_effect = [
        metadata("root", "Customer", parent),
        metadata("profile", folder_name, "root"),
    ]
    maps, roots = ownership_maps(refs, [client(client_type="company")])
    assert maps["profile"] == (11, folder_name)
    assert roots == {"root": 11}
    assert await revalidate_poll_owner(conn, drive, 11, "root", "profile", folder_name)
    assert conn.fetchval.call_args.args[-1] == "company"
    conn.execute.assert_not_awaited()
    drive.create_folder.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_company_under_individuals_rejects_company_folder_taxonomy():
    conn, drive = valid_context(
        rows=[client(client_type="company")], refs=[folder(name="99_Archive")]
    )
    drive.get_file_metadata.side_effect = [
        metadata("root", "Customer", "individuals"),
        metadata("profile", "99_Archive", "root"),
    ]
    assert not await revalidate_poll_owner(conn, drive, 11, "root", "profile", "99_Archive")


def test_company_aliases_collapse_only_when_classification_agrees():
    refs = [folder(name="00_PROFIL_E_FONDAMENTA_AZIENDALI"), folder(name="01_Legal_and_Licensing")]
    assert ownership_maps(refs, [client(client_type="company")])[0] == {
        "profile": (11, "01_Legal_and_Licensing")
    }
    assert not ownership_maps(refs + [folder(name="99_Archive")], [client(client_type="company")])[
        0
    ]
