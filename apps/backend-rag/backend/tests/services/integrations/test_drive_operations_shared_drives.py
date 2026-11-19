"""Guard: folder listing must include Shared Drive items (2026-11-19).

Client folders live in a Shared Drive (the SA impersonates a Workspace user
"who has access to Shared Drives" and creates folders with
supportsAllDrives=True — service_account_drive_service.py). Drive v3
`files.list` called WITHOUT `supportsAllDrives` + `includeItemsFromAllDrives`
silently returns `files: []` for a Shared Drive parent: no error, so the CRM
Drive panels rendered "This folder is empty." for folders that are not empty.

Mock pattern mirrors test_drive_operations_normalization.py (no network).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.integrations.drive.drive_operations import DriveOperationsManager

pytestmark = pytest.mark.asyncio

USER = "staff@example.com"
FOLDER_ID = "FOLDER_123"


def _manager() -> tuple[DriveOperationsManager, MagicMock]:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"files": []})
    auth = MagicMock()
    auth.get_access_token = AsyncMock(return_value="tok-123")
    http = MagicMock()
    http.get = AsyncMock(return_value=resp)
    return DriveOperationsManager(auth_manager=auth, http_client=http, audit=MagicMock()), http


async def test_list_files_requests_shared_drive_items():
    mgr, http = _manager()
    await mgr.list_files(USER, folder_id=FOLDER_ID, page_size=100)

    params = http.get.call_args.kwargs["params"]
    assert params.get("supportsAllDrives") is True
    assert params.get("includeItemsFromAllDrives") is True
    # The folder scoping itself is unchanged.
    assert f"'{FOLDER_ID}' in parents" in params["q"]


async def test_list_files_keeps_shared_drive_flags_when_paginating():
    mgr, http = _manager()
    await mgr.list_files(USER, folder_id=FOLDER_ID, page_token="PAGE_2")

    params = http.get.call_args.kwargs["params"]
    assert params["pageToken"] == "PAGE_2"
    assert params.get("supportsAllDrives") is True
    assert params.get("includeItemsFromAllDrives") is True
