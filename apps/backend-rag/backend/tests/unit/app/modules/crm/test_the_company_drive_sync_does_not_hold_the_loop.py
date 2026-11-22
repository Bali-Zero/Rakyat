"""The company Drive sync does its Drive work off the event loop.

The handler is a coroutine on the API's event loop and the Drive client is synchronous: it
read the credentials, built the client and listed every folder inline, so each Drive round
trip held every other request of the worker. Here the Drive client is a stand-in whose calls
block for real and record the thread they ran on.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.app.modules.crm import company_router

pytestmark = pytest.mark.asyncio

COMPANY = 7
ROOT = "zz-folder-root"
SUB = "zz-folder-sub"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
TREE: dict[str, list[dict[str, Any]]] = {
    ROOT: [
        {"id": SUB, "name": "Sub", "mimeType": FOLDER},
        {"id": "zz-file-1", "name": "zz notes one.pdf", "mimeType": "application/pdf"},
    ],
    SUB: [
        {"id": "zz-file-2", "name": "zz notes two.pdf", "mimeType": "application/pdf"},
        {"id": "zz-link", "name": "zz link", "mimeType": SHORTCUT},
    ],
}


class _Drive:
    """A Drive client whose every call blocks, as the real one does on the network."""

    def __init__(self, hold: float, fail_on_list: bool = False) -> None:
        self.hold = hold
        self.fail_on_list = fail_on_list
        self.threads: dict[str, list[int]] = {"credentials": [], "build": [], "list": []}
        self.queries: list[str] = []

    # google.oauth2.service_account.Credentials.from_service_account_file
    def credentials(self, _path: str, scopes: list[str]) -> MagicMock:
        self.threads["credentials"].append(threading.get_ident())
        assert scopes == ["https://www.googleapis.com/auth/drive.readonly"]
        return MagicMock(name="credentials")

    # googleapiclient.discovery.build
    def build(self, name: str, version: str, credentials: Any) -> _Drive:
        self.threads["build"].append(threading.get_ident())
        assert (name, version) == ("drive", "v3")
        time.sleep(self.hold)
        return self

    def files(self) -> _Drive:
        return self

    def list(self, **kwargs: Any) -> MagicMock:
        request = MagicMock()
        request.execute = lambda: self._execute(kwargs["q"])
        return request

    def _execute(self, query: str) -> dict[str, Any]:
        self.threads["list"].append(threading.get_ident())
        self.queries.append(query)
        time.sleep(self.hold)
        if self.fail_on_list:
            raise RuntimeError("zz drive refused")
        folder = query.split("'")[1]
        return {"files": TREE[folder]}


class _Database:
    """The pool and its one connection: the company row, no tracked document, new ids."""

    def __init__(self) -> None:
        self.inserted: list[tuple[Any, ...]] = []

    def acquire(self) -> _Database:
        return self

    async def __aenter__(self) -> _Database:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def fetchrow(self, _sql: str, company_id: int) -> dict[str, Any]:
        return {"id": company_id, "company_name": "Zz Company", "google_drive_folder_id": ROOT}

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "INSERT INTO company_documents" in sql:
            self.inserted.append(args)
            return 100 + len(self.inserted)
        return None


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> _Drive:
    from google.oauth2 import service_account
    from googleapiclient import discovery

    stand_in = _Drive(**getattr(request, "param", {"hold": 0.2}))
    monkeypatch.delenv("GOOGLE_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/zz-credentials.json")
    monkeypatch.setattr(
        service_account.Credentials, "from_service_account_file", stand_in.credentials
    )
    monkeypatch.setattr(discovery, "build", stand_in.build)
    monkeypatch.setattr(company_router, "_require_company_editor", AsyncMock())
    return stand_in


async def _sync_while_counting_beats(database: _Database) -> tuple[dict[str, Any], int]:
    """Run the handler; count how often a 20 ms heartbeat on the same loop ran meanwhile."""
    beats = 0
    running = True

    async def heart() -> None:
        nonlocal beats
        while running:
            await asyncio.sleep(0.02)
            beats += 1

    task = asyncio.create_task(heart())
    await asyncio.sleep(0)
    try:
        result = await company_router.sync_company_drive_folder(
            company_id=COMPANY, db=database, current_user={"email": "staff@example.test"}
        )
    finally:
        running = False
        await task
    return result, beats


async def test_no_drive_call_runs_on_the_loops_thread_and_the_loop_keeps_running(
    drive: _Drive,
) -> None:
    loop_thread = threading.get_ident()
    database = _Database()

    result, beats = await _sync_while_counting_beats(database)

    # One build and two listings (the root and its sub-folder), each blocking for 0.2 s.
    assert {step: len(ran_on) for step, ran_on in drive.threads.items()} == {
        "credentials": 1,
        "build": 1,
        "list": 2,
    }
    for step, ran_on in drive.threads.items():
        assert loop_thread not in ran_on, f"{step} ran on the event loop's thread"
    # 0.6 s of blocking Drive work: a free loop beats about thirty times, a held one never.
    assert beats >= 10
    # The sync itself is what it was: both files added, the shortcut left out.
    assert drive.queries == [
        f"'{ROOT}' in parents and trashed=false",
        f"'{SUB}' in parents and trashed=false",
    ]
    assert result == {
        "company_id": COMPANY,
        "folder_id": ROOT,
        "added": 2,
        "skipped": 0,
        "total_in_folder": 2,
        "docs": [
            {
                "id": 101,
                "file_name": "zz notes two.pdf",
                "document_type": company_router._infer_doc_type("zz notes two.pdf"),
            },
            {
                "id": 102,
                "file_name": "zz notes one.pdf",
                "document_type": company_router._infer_doc_type("zz notes one.pdf"),
            },
        ],
        "ocr_queued": 0,
    }
    assert [row[3] for row in database.inserted] == ["zz-file-2", "zz-file-1"]


@pytest.mark.parametrize("drive", [{"hold": 0.0, "fail_on_list": True}], indirect=True)
async def test_a_listing_that_fails_is_still_a_503_with_its_reason(drive: _Drive) -> None:
    with pytest.raises(HTTPException) as refused:
        await company_router.sync_company_drive_folder(
            company_id=COMPANY, db=_Database(), current_user={"email": "staff@example.test"}
        )

    assert refused.value.status_code == 503
    assert refused.value.detail == "Drive list error: zz drive refused"


async def test_missing_credentials_are_still_a_503(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with pytest.raises(HTTPException) as refused:
        await company_router.sync_company_drive_folder(
            company_id=COMPANY, db=_Database(), current_user={"email": "staff@example.test"}
        )

    assert refused.value.status_code == 503
    assert "Drive credentials not configured" in refused.value.detail
    assert drive.threads["build"] == []
