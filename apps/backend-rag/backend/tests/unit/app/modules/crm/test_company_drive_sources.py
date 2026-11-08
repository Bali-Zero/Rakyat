"""Informational links are staff-only metadata, never a document access grant."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.modules.crm.company_router import router

FOLDER = "synthetic_drive_folder_00001"
URL = "/api/crm/companies/17/drive-sources"


@pytest.fixture
def harness():
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "google_drive_folder_id": FOLDER,
        "tax_dept_folder_id": None,
        "custom_fields": {},
    }
    conn.fetch.return_value = []

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    user = {"role": "admin"}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_database_pool] = Pool
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), conn, user


@pytest.mark.parametrize("role", ["admin", "team", "Tax Care", "Consultant"])
def test_team_access(harness, role):
    client, conn, user = harness
    user["role"] = role
    assert client.get(URL).status_code == 200
    conn.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "role", ["client", "partner", "monitoring", "service", "unknown", "", None]
)
def test_non_team_denied_before_database(harness, role):
    client, conn, user = harness
    user["role"] = role
    assert client.get(URL).status_code == 403
    conn.fetchrow.assert_not_awaited()
    conn.fetch.assert_not_awaited()


def test_deduplicates_and_preserves_provenance(harness):
    client, conn, _ = harness
    conn.fetchrow.return_value.update(
        tax_dept_folder_id=FOLDER,
        custom_fields={"legacy_drive_folder_ids": [FOLDER, FOLDER], "private": "DO NOT RETURN"},
    )
    body = client.get(URL).json()
    assert body["sources"] == [
        {
            "url": f"https://drive.google.com/drive/folders/{FOLDER}",
            "kinds": ["primary", "restricted_tax", "legacy"],
        }
    ]
    assert "custom_fields" not in body
    assert "DO NOT RETURN" not in str(body)


def test_tax_archive_is_available_without_a_primary_folder(harness):
    client, conn, _ = harness
    conn.fetchrow.return_value.update(google_drive_folder_id=None, tax_dept_folder_id=FOLDER)
    assert client.get(URL).json()["sources"] == [
        {"url": f"https://drive.google.com/drive/folders/{FOLDER}", "kinds": ["restricted_tax"]}
    ]


@pytest.mark.parametrize("legacy", [FOLDER, {"id": FOLDER}, None, True])
def test_legacy_collection_must_be_a_list(harness, legacy):
    client, conn, _ = harness
    conn.fetchrow.return_value.update(
        google_drive_folder_id=None, custom_fields={"legacy_drive_folder_ids": legacy}
    )
    assert client.get(URL).json()["sources"] == []


@pytest.mark.parametrize("company_id", [0, -1, 2**31])
def test_company_id_is_bounded(harness, company_id):
    client, conn, _ = harness
    assert client.get(f"/api/crm/companies/{company_id}/drive-sources").status_code == 422
    conn.fetchrow.assert_not_awaited()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        True,
        123,
        "123456789012345678901234567890",
        "https://drive.google.com/drive/folders/" + FOLDER,
        "https://evil.example/folder",
        "AHU-123456789012345678901234",
        "NPWP123456789012345678901234",
        "short_folder",
        "../" + FOLDER,
        " " + FOLDER,
        {"id": FOLDER},
    ],
)
def test_malformed_values_never_become_links(harness, bad):
    client, conn, _ = harness
    conn.fetchrow.return_value.update(
        google_drive_folder_id=bad,
        tax_dept_folder_id=bad,
        custom_fields={"legacy_drive_folder_ids": [bad]},
    )
    assert client.get(URL).json()["sources"] == []


@pytest.mark.parametrize("bad", ["broken json", "[]", [], None, 12])
def test_malformed_custom_fields_are_ignored(harness, bad):
    client, conn, _ = harness
    conn.fetchrow.return_value["custom_fields"] = bad
    assert len(client.get(URL).json()["sources"]) == 1


def test_json_custom_fields_and_historical_record_ids(harness):
    client, conn, _ = harness
    conn.fetchrow.return_value["custom_fields"] = (
        '{"legacy_drive_folder_ids": ["1OtherSyntheticFolderAbCdEf123456"]}'
    )
    conn.fetch.return_value = [
        {"id": 8, "custom_fields": {"merged_into": 17}},
        {"id": 9, "custom_fields": {"merged_into": "17"}},
        {"id": 10, "custom_fields": {"merged_into": 17.0}},
    ]
    body = client.get(URL).json()
    assert len(body["sources"]) == 2
    assert body["historical_record_ids"] == [8]


@pytest.mark.parametrize("ref", [True, "18", 18.0, 0, -1, 2**31, 17, {}, []])
def test_invalid_lineage_is_not_exposed(harness, ref):
    client, conn, _ = harness
    conn.fetchrow.return_value["custom_fields"] = {"merged_into": ref}
    body = client.get(URL).json()
    assert body["lineage_status"] == "invalid"
    assert body["merged_into_company_id"] is None
    assert conn.fetchrow.await_count == 1


@pytest.mark.parametrize(
    "target",
    [
        None,
        {"custom_fields": {"merged_into": 17}},
        {"custom_fields": {"merged_into": 19}},
        {"custom_fields": "bad"},
    ],
)
def test_missing_cycle_or_unverified_chain_is_rejected(harness, target):
    client, conn, _ = harness
    source = dict(conn.fetchrow.return_value, custom_fields={"merged_into": 18})
    conn.fetchrow.side_effect = [source, target]
    body = client.get(URL).json()
    assert body["lineage_status"] == "invalid"
    assert body["merged_into_company_id"] is None


def test_valid_lineage_only_exposes_target_id(harness):
    client, conn, _ = harness
    source = dict(conn.fetchrow.return_value, custom_fields={"merged_into": 18})
    conn.fetchrow.side_effect = [source, {"custom_fields": {}}]
    body = client.get(URL).json()
    assert body["lineage_status"] == "valid"
    assert body["merged_into_company_id"] == 18
    assert len(body["sources"]) == 1
    assert body["historical_record_ids"] == []


def test_missing_company(harness):
    client, conn, _ = harness
    conn.fetchrow.return_value = None
    assert client.get(URL).status_code == 404
