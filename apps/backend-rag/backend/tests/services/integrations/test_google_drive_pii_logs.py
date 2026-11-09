"""PII log hygiene for GoogleDriveService, the OAuth Drive service (token, folder and upload lines).

Class D of the 2026-11-04 PII-log window, the sibling of the service-account service cured in
class C. The service wrote client data in cleartext to the production log stream (Fly stdout and
the rotating file): the folder name built from the client's name, client document file names,
the client_name itself, subfolder paths, the raw text of the exceptions of the client-structure
loop, the raw body of every Google error response (it echoes the words of the request), and a
user_id that falls back to the signed-in user's e-mail address. The lines now keep ids, counts and
byte sizes only, the user id goes through redact_identifier_for_log, a Google error is logged as
its HTTP status, and an exception is logged as its class name.

GUILT: with a sentinel client, file, folder, search term, user address and Google error body
driven through the real methods over a fake httpx client and a fake pool, no sentinel reaches a
log record. INNOCENCE: every line is still emitted at the same level with its prefix, its ids and
its byte size, and the return values, the raised errors and the calls made to Drive are unchanged.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from itertools import count
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.security.pii_log_identifier import redact_identifier_for_log
from backend.services.integrations import google_drive_service as drive_module
from backend.services.integrations.google_drive_service import GoogleDriveService

_LOGGER = "backend.services.integrations.google_drive_service"
_TAG = "[GDRIVE]"
_FOLDER_MIME = "application/vnd.google-apps.folder"

_CLIENT_NAME = "Sentinel Clientname"
_FILE_NAME = "sentinel_passport_scan.pdf"
_SEARCH_TERM = "SENTINEL-SEARCH-TERM"
_USER_EMAIL = "sentinel.staff@example.com"
_BODY_TEXT = "SENTINEL-GOOGLE-BODY"


def _response(status: int, body: dict[str, Any]) -> httpx.Response:
    """A real httpx.Response: the service reads status_code, json() and headers from it."""
    return httpx.Response(status, json=body)


def _drive_error(status: int) -> httpx.Response:
    """What the Drive API answers on an error: a body that echoes the request's own words."""
    message = f"File not found: {_BODY_TEXT}"
    error = {"code": status, "message": message, "errors": [{"message": message}]}
    return _response(status, {"error": error})


def _oauth_error(status: int) -> httpx.Response:
    """What the Google token endpoint answers on an error."""
    return _response(
        status, {"error": "invalid_grant", "error_description": f"Bad code near {_BODY_TEXT}"}
    )


class _Conn:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.executed: list[tuple[Any, ...]] = []

    async def fetchrow(self, *_args: Any) -> dict[str, Any] | None:
        return self.row

    async def execute(self, *args: Any) -> str:
        self.executed.append(args)
        return "OK"


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Conn]:
        yield self.conn


def _client(**scripts: Any) -> AsyncMock:
    """An httpx.AsyncClient stand-in: each verb replays its script (a list, or a function)."""
    client = AsyncMock()
    client.is_closed = False
    for verb, script in scripts.items():
        getattr(client, verb).side_effect = script
    return client


def _service(client: AsyncMock, conn: _Conn | None = None) -> GoogleDriveService:
    configured = SimpleNamespace(
        google_drive_client_id="client-id-1",
        google_drive_client_secret="client-secret-1",
        google_drive_redirect_uri="https://drive.example.invalid/callback",
        google_drive_root_folder_id="root-id-1",
    )
    with patch.object(drive_module, "settings", configured):
        service = GoogleDriveService(db_pool=_Pool(conn or _Conn()))
    service._client = client
    return service


def _connected(client: AsyncMock) -> GoogleDriveService:
    """A service whose user always holds a valid token, so the call reaches Drive."""
    service = _service(client)
    service.get_valid_token = AsyncMock(return_value="access-token-1")
    return service


def _folder_poster(fail_on: int | None = None) -> Callable[..., httpx.Response]:
    """Answers the folder-create POSTs: created-1, created-2, ... and a 403 on call ``fail_on``."""
    calls = count(1)

    def post(_url: str, **kwargs: Any) -> httpx.Response:
        number = next(calls)
        if number == fail_on:
            return _drive_error(403)
        body = {
            "id": f"created-{number}",
            "name": kwargs["json"]["name"],
            "webViewLink": f"https://drive.example.invalid/created-{number}",
        }
        return _response(200, body)

    return post


def _folder_client(fail_on: int | None = None) -> AsyncMock:
    """In-memory Drive API supporting list, metadata and creation."""
    files: dict[str, dict[str, Any]] = {}
    calls = count(1)

    def post(_url: str, **kwargs: Any) -> httpx.Response:
        number = next(calls)
        if number == fail_on:
            return _drive_error(403)
        body = kwargs["json"]
        file_id = f"created-{number}"
        files[file_id] = {
            "id": file_id,
            "name": body["name"],
            "mimeType": _FOLDER_MIME,
            "parents": body["parents"],
            "trashed": False,
            "webViewLink": f"https://drive.example.invalid/{file_id}",
        }
        return _response(200, files[file_id])

    def get(url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/files"):
            query = kwargs["params"]["q"]
            parent = query.split("'", 2)[1]
            return _response(200, {"files": [f for f in files.values() if parent in f["parents"]]})
        return _response(200, files[url.rsplit("/", 1)[-1]])

    return _client(post=post, get=get)


def _folder_settings() -> Any:
    """Override only Drive parents, leaving other application settings intact."""
    from backend.app.core.config import settings

    return patch.multiple(
        settings,
        gdrive_individuals_folder_id="individuals-id-3",
        gdrive_companies_folder_id="companies-id-4",
        google_drive_root_folder_id="root-id-2",
    )


def _module_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


def _lines(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in _module_records(caplog)]


def _record_texts(record: logging.LogRecord) -> list[str]:
    texts = [str(record.msg), record.getMessage(), repr(record.args)]
    if record.exc_text:
        texts.append(record.exc_text)
    if record.exc_info:
        texts.extend(traceback.format_exception(*record.exc_info))
    return texts


def _assert_clean(caplog: pytest.LogCaptureFixture, *banned: str) -> None:
    """No sentinel, address or traceback anywhere in any captured record."""
    blob = "\n".join(t for r in caplog.records for t in _record_texts(r)) + "\n" + caplog.text
    lowered = blob.lower()
    assert "sentinel" not in lowered
    assert "@example.com" not in lowered
    for value in banned:
        assert value.lower() not in lowered
    assert all(r.exc_info is None and not r.exc_text for r in caplog.records)


@pytest.fixture(autouse=True)
def _log_digest_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_PII_HMAC_SALT", "classd-test-salt")


def test_the_google_bodies_really_carry_the_leaked_text() -> None:
    """Positive control: the leak vector exists, so a clean log is a result and not a vacuum."""
    assert _BODY_TEXT in str(_drive_error(404).json())
    assert _BODY_TEXT in str(_oauth_error(400).json())


@pytest.mark.asyncio
async def test_exchange_code_lines_carry_the_redacted_user_not_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tokens = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}
    conn = _Conn()
    service = _service(_client(post=[_response(200, tokens)]), conn)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.exchange_code("auth-code-1", _USER_EMAIL)

    assert result["access_token"] == "at-1"
    assert conn.executed[0][1] == _USER_EMAIL
    digest = redact_identifier_for_log(_USER_EMAIL)
    assert digest.startswith("id:")
    _assert_clean(caplog, _USER_EMAIL)
    assert _lines(caplog) == [
        ("INFO", f"{_TAG} Exchanging code for user {digest}"),
        ("INFO", f"{_TAG} Successfully connected for user {digest}"),
    ]


@pytest.mark.parametrize("status", [400, 503])
@pytest.mark.asyncio
async def test_exchange_code_failure_logs_the_status_not_the_google_body(
    caplog: pytest.LogCaptureFixture, status: int
) -> None:
    service = _service(_client(post=[_oauth_error(status)]))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        with pytest.raises(ValueError):
            await service.exchange_code("bad-code-1", _USER_EMAIL)

    digest = redact_identifier_for_log(_USER_EMAIL)
    _assert_clean(caplog, _USER_EMAIL, _BODY_TEXT)
    assert _lines(caplog) == [
        ("INFO", f"{_TAG} Exchanging code for user {digest}"),
        ("ERROR", f"{_TAG} Token exchange failed: HTTP {status}"),
    ]


@pytest.mark.asyncio
async def test_refresh_failure_lines_carry_the_redacted_user_not_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _service(_client(post=[_oauth_error(401)]))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        refreshed = await service._refresh_token(_USER_EMAIL, "refresh-token-1")

    assert refreshed is None
    digest = redact_identifier_for_log(_USER_EMAIL)
    _assert_clean(caplog, _USER_EMAIL, _BODY_TEXT)
    assert _lines(caplog) == [
        ("INFO", f"{_TAG} Refreshing token for user {digest}"),
        ("ERROR", f"{_TAG} Token refresh failed for user {digest}"),
    ]


@pytest.mark.asyncio
async def test_the_system_token_line_stays_a_constant_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _service(_client())
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        first = await service._refresh_token("SYSTEM", "refresh-token-1")
        second = await service._refresh_token("SYSTEM", "refresh-token-1")

    assert first is None
    assert second is None
    (record,) = _module_records(caplog)
    assert record.levelname == "INFO"
    assert not record.args
    assert record.getMessage().startswith(f"{_TAG} OAuth SYSTEM disabled")


@pytest.mark.parametrize("user_id", [_USER_EMAIL, "SYSTEM"], ids=["staff-address", "system-user"])
@pytest.mark.asyncio
async def test_disconnect_line_carries_the_redacted_user_not_the_address(
    caplog: pytest.LogCaptureFixture, user_id: str
) -> None:
    conn = _Conn(row={"access_token": "access-token-1"})
    service = _service(_client(post=[_response(200, {})]), conn)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        disconnected = await service.disconnect(user_id)

    assert disconnected is True
    assert conn.executed[0][1] == user_id
    _assert_clean(caplog, _USER_EMAIL)
    assert _lines(caplog) == [
        ("INFO", f"{_TAG} Disconnected for user {redact_identifier_for_log(user_id)}")
    ]


@pytest.mark.asyncio
async def test_list_files_failure_logs_the_status_not_the_google_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _connected(_client(get=[_drive_error(404)]))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        with pytest.raises(ValueError):
            await service.list_files(_USER_EMAIL, folder_id="folder-1")

    _assert_clean(caplog, _USER_EMAIL, _BODY_TEXT)
    assert _lines(caplog) == [("ERROR", f"{_TAG} List files failed: HTTP 404")]


@pytest.mark.asyncio
async def test_created_folder_line_carries_the_id_not_the_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    created = {"id": "new-folder-id-7", "name": f"11_{_CLIENT_NAME}", "webViewLink": "https://x"}
    client = _client(post=[_response(200, created)])
    service = _connected(client)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        folder = await service.create_folder(_USER_EMAIL, f"11_{_CLIENT_NAME}", parent_id="p-1")

    assert folder["id"] == "new-folder-id-7"
    assert client.post.await_args.kwargs["json"] == {
        "name": f"11_{_CLIENT_NAME}",
        "mimeType": _FOLDER_MIME,
        "parents": ["p-1"],
    }
    _assert_clean(caplog, _CLIENT_NAME, _USER_EMAIL)
    (record,) = _module_records(caplog)
    assert record.levelname == "INFO"
    assert record.msg == f"{_TAG} Created folder with ID: %s"
    assert record.args == ("new-folder-id-7",)


@pytest.mark.asyncio
async def test_create_folder_failure_logs_the_status_not_the_google_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _connected(_client(post=[_drive_error(403)]))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        with pytest.raises(ValueError):
            await service.create_folder(_USER_EMAIL, f"11_{_CLIENT_NAME}")

    _assert_clean(caplog, _CLIENT_NAME, _USER_EMAIL, _BODY_TEXT)
    assert _lines(caplog) == [("ERROR", f"{_TAG} Create folder failed: HTTP 403")]


@pytest.mark.asyncio
async def test_client_folder_structure_lines_carry_ids_never_the_client_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _folder_client()
    service = _connected(client)
    with _folder_settings(), caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.create_client_folder_async(31, _CLIENT_NAME, "individual")

    assert result["success"] is True
    assert result["root_folder_id"] == "created-1"
    assert len(result["subfolders"]) == 14
    assert client.post.await_args_list[0].kwargs["json"] == {
        "name": f"31_{_CLIENT_NAME}",
        "mimeType": _FOLDER_MIME,
        "parents": ["individuals-id-3"],
    }
    _assert_clean(caplog, _CLIENT_NAME)
    created = [("INFO", f"{_TAG} Created folder with ID: created-{n}") for n in range(1, 16)]
    structure = ("INFO", f"{_TAG} Created client folder structure for client 31: created-1")
    assert _lines(caplog) == [*created, structure]


@pytest.mark.asyncio
async def test_subfolder_failure_logs_ids_and_class_not_the_path_or_the_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _connected(_folder_client(fail_on=5))
    with _folder_settings(), caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.create_client_folder_async(32, _CLIENT_NAME, "individual")

    assert result["success"] is True
    assert len(result["subfolders"]) == 13
    assert "04_Archive" not in result["subfolders"]
    _assert_clean(caplog, _CLIENT_NAME, _BODY_TEXT)
    before = [("INFO", f"{_TAG} Created folder with ID: created-{n}") for n in range(1, 5)]
    failed = [
        ("ERROR", f"{_TAG} Create folder failed: HTTP 403"),
        (
            "ERROR",
            f"{_TAG} Failed to create subfolder under root created-1 for client 32: ValueError",
        ),
    ]
    after = [("INFO", f"{_TAG} Created folder with ID: created-{n}") for n in range(6, 16)]
    structure = ("INFO", f"{_TAG} Created client folder structure for client 32: created-1")
    assert _lines(caplog) == [*before, *failed, *after, structure]


@pytest.mark.asyncio
async def test_root_folder_failure_logs_the_client_id_and_the_class_not_the_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _connected(_folder_client(fail_on=1))
    with _folder_settings(), caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.create_client_folder_async(33, _CLIENT_NAME, "individual")

    assert result["success"] is False
    _assert_clean(caplog, _CLIENT_NAME, _BODY_TEXT)
    assert _lines(caplog) == [
        ("ERROR", f"{_TAG} Create folder failed: HTTP 403"),
        ("ERROR", f"{_TAG} Failed to create client folder for client 33: ValueError"),
    ]


@pytest.mark.asyncio
async def test_list_folder_files_failure_logs_the_status_not_the_body_or_the_search_term(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(get=[_drive_error(500)])
    service = _connected(client)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        with pytest.raises(ValueError):
            await service.list_folder_files(_USER_EMAIL, "folder-1", search=_SEARCH_TERM)

    assert _SEARCH_TERM in client.get.await_args.kwargs["params"]["q"]
    _assert_clean(caplog, _USER_EMAIL, _SEARCH_TERM, _BODY_TEXT)
    assert _lines(caplog) == [("ERROR", f"{_TAG} List folder files failed: HTTP 500")]


@pytest.mark.asyncio
async def test_get_folder_structure_failure_logs_the_status_not_the_google_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _connected(_client(get=[_drive_error(404)]))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        with pytest.raises(ValueError):
            await service.get_folder_structure(_USER_EMAIL, "root-folder-9")

    _assert_clean(caplog, _USER_EMAIL, _BODY_TEXT)
    assert _lines(caplog) == [("ERROR", f"{_TAG} Get folder structure failed: HTTP 404")]


@pytest.mark.asyncio
async def test_uploaded_line_carries_the_file_id_and_the_byte_size_not_the_file_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metadata = {"id": "uploaded-file-id-9", "name": _FILE_NAME, "size": "2048"}
    client = _client(post=[_response(200, metadata)], patch=[_response(200, {})])
    service = _connected(client)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        uploaded = await service.upload_file_to_folder(
            _USER_EMAIL, "folder-1", b"x" * 2048, _FILE_NAME
        )

    assert uploaded == {
        "id": "uploaded-file-id-9",
        "name": _FILE_NAME,
        "size_bytes": 2048,
        "download_url": "/api/documents/proxy/uploaded-file-id-9",
    }
    assert client.post.await_args.kwargs["json"] == {"name": _FILE_NAME, "parents": ["folder-1"]}
    _assert_clean(caplog, _FILE_NAME, _USER_EMAIL)
    (record,) = _module_records(caplog)
    assert record.levelname == "INFO"
    assert record.msg == f"{_TAG} Uploaded file %s (%s bytes) to folder %s"
    assert record.args == ("uploaded-file-id-9", 2048, "folder-1")


@pytest.mark.asyncio
async def test_upload_metadata_failure_logs_the_status_not_the_google_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(post=[_drive_error(403)])
    service = _connected(client)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        with pytest.raises(ValueError):
            await service.upload_file_to_folder(_USER_EMAIL, "folder-1", b"x", _FILE_NAME)

    client.patch.assert_not_awaited()
    _assert_clean(caplog, _FILE_NAME, _USER_EMAIL, _BODY_TEXT)
    assert _lines(caplog) == [("ERROR", f"{_TAG} Upload file metadata failed: HTTP 403")]


@pytest.mark.asyncio
async def test_upload_content_failure_logs_the_status_not_the_google_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metadata = {"id": "uploaded-file-id-9", "name": _FILE_NAME, "size": "1"}
    client = _client(post=[_response(200, metadata)], patch=[_drive_error(500)])
    service = _connected(client)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        with pytest.raises(ValueError):
            await service.upload_file_to_folder(_USER_EMAIL, "folder-1", b"x", _FILE_NAME)

    _assert_clean(caplog, _FILE_NAME, _USER_EMAIL, _BODY_TEXT)
    assert _lines(caplog) == [("ERROR", f"{_TAG} Upload file content failed: HTTP 500")]
