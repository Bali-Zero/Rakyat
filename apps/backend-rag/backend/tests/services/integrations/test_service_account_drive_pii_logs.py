"""PII log hygiene for ServiceAccountDriveService (folder, upload and client-structure lines).

Class C of the 2026-11-04 PII-log window. The service wrote client data in cleartext to the
production log stream (Fly stdout and the rotating file): the root folder name built from the
client's name, client document file names, the client_name itself, subfolder paths, the
delegated Workspace address, the folder name read back from Drive metadata, and the raw text of
Drive, database and credential-parsing exceptions (a googleapiclient HttpError carries the
request URI and a files().list query carries the name; a UnicodeDecodeError raised while
decoding the base64 service-account credential prints a byte derived from it). The lines now
keep ids, counts and byte sizes only, the delegated address goes through
redact_identifier_for_log, and an exception is logged as its class name (plus the HTTP status
when it exposes resp.status).

GUILT: with a sentinel client, file, folder, subfolder and address driven through the real
methods over a fake Drive API, no sentinel reaches a log record. INNOCENCE: every line is still
emitted at the same level with its prefix, its ids and its byte size, and the return values and
the calls made to Drive are unchanged.
"""

from __future__ import annotations

import base64
import json
import logging
import traceback
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from itertools import count
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import httplib2
import pytest
from asyncpg import exceptions as pg_exceptions
from googleapiclient.errors import HttpError

from backend.security.pii_log_identifier import redact_identifier_for_log
from backend.services.integrations import service_account_drive_service as drive_module
from backend.services.integrations.service_account_drive_service import (
    ServiceAccountDriveService,
)

_LOGGER = "backend.services.integrations.service_account_drive_service"

_OK = "✅"
_ALERT = "\U0001f6a8"
_WARN = "⚠"
_RECYCLE = "♻️"

_CLIENT_NAME = "Sentinel Clientname"
_FILE_NAME = "sentinel_passport_scan.pdf"
_FOLDER_NAME = "SENTINEL-FOLDER"
_SUBFOLDER = "SENTINEL-SUBFOLDER"
_DELEGATED = "sentinel.user@example.com"
_DB_DETAIL = "SENTINEL-DB-DETAIL"
_CRED_DETAIL = "SENTINEL-CRED-DETAIL"


def _http_error(status: int, leaked: str) -> HttpError:
    """A real googleapiclient HttpError whose str() carries ``leaked`` twice, as Drive does."""
    resp = httplib2.Response({"status": status, "reason": "Probe"})
    content = f'{{"error": {{"message": "File not found: {leaked}", "code": {status}}}}}'
    uri = f"https://www.googleapis.com/drive/v3/files?q=name%20%3D%20%27{quote(leaked, safe='')}%27"
    return HttpError(resp, content.encode(), uri=uri)


def _db_error(error_class: type[Exception], leaked: str) -> Exception:
    return error_class(f"driver detail: key (subfolder_name)=({leaked}) failed")


class _ResponselessError(Exception):
    """An exception that declares a ``resp`` attribute holding nothing."""

    resp = None


class _StatuslessResponseError(Exception):
    """An exception whose ``resp`` exists but exposes no ``status``."""

    resp = SimpleNamespace()


class _Request:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    def execute(self) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _FakeFiles:
    """Scripted stand-in for ``service.files()``: each call returns a request whose
    ``execute()`` answers, or raises, what the test scripted."""

    def __init__(
        self,
        *,
        get_by_id: dict[str, Any] | None = None,
        list_outcome: Any = None,
        create_result: dict[str, Any] | None = None,
        create_failures: dict[str, BaseException] | None = None,
    ) -> None:
        self._get_by_id = get_by_id or {}
        self._list_outcome = {"files": []} if list_outcome is None else list_outcome
        self._create_result = create_result
        self._create_failures = create_failures or {}
        self._ids = count(1)
        self.created_bodies: list[dict[str, Any]] = []

    def get(self, **kwargs: Any) -> _Request:
        return _Request(self._get_by_id[kwargs["fileId"]])

    def list(self, **kwargs: Any) -> _Request:
        return _Request(self._list_outcome)

    def create(self, **kwargs: Any) -> _Request:
        body = kwargs["body"]
        self.created_bodies.append(body)
        if body["name"] in self._create_failures:
            return _Request(self._create_failures[body["name"]])
        if self._create_result is not None:
            return _Request(self._create_result)
        n = next(self._ids)
        return _Request({"id": f"created-{n}", "webViewLink": f"https://drive.example/created-{n}"})


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def execute(self, sql: str, *args: Any) -> str:
        for needle, error in self._pool.fail_on.items():
            if needle in sql:
                raise error
        if "UPDATE clients SET google_drive_folder_id" in sql:
            self._pool.state["google_drive_folder_id"] = args[0]
            return "UPDATE 1"
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._pool.state.get("google_drive_folder_id")


class _FakePool:
    def __init__(self, fail_on: dict[str, BaseException] | None = None) -> None:
        self.state: dict[str, Any] = {}
        self.fail_on = fail_on or {}

    @asynccontextmanager
    async def _connection(self):
        yield _FakeConn(self)

    def acquire(self):
        return self._connection()


def _service(
    monkeypatch: pytest.MonkeyPatch,
    files: _FakeFiles,
    *,
    subfolders: list[str] | None = None,
    **settings: Any,
) -> ServiceAccountDriveService:
    """The real service over the fake Drive API, without touching Google credentials."""
    configured: dict[str, Any] = {
        "google_drive_root_folder_id": "root-id-2",
        "gdrive_individuals_folder_id": "individuals-id-3",
        "gdrive_companies_folder_id": None,
    }
    configured.update(settings)
    monkeypatch.setattr(drive_module, "settings", SimpleNamespace(**configured))
    service = ServiceAccountDriveService.__new__(ServiceAccountDriveService)
    service.service = SimpleNamespace(files=lambda: files)
    service.root_folder_id = "root-id-1"
    if subfolders is not None:
        monkeypatch.setattr(
            drive_module, "standard_subfolder_paths", lambda client_type: subfolders
        )

        async def resolve_for_log_test(name: str, parent: str, *, conn: Any) -> str:
            return (await service.create_folder(name, parent_id=parent))["id"]

        service.resolve_canonical_folder = resolve_for_log_test
    return service


@contextmanager
def _constructor_patches(creds_str: str) -> Iterator[None]:
    """Settings, credential factory, API builder and folder validation patched out, so the real
    constructor runs on ``creds_str`` without touching Google."""
    configured = SimpleNamespace(
        google_credentials_json=creds_str,
        google_drive_root_folder_id="root-id-2",
        gdrive_individuals_folder_id=None,
        gdrive_companies_folder_id=None,
    )
    with (
        patch.object(drive_module, "settings", configured),
        patch.object(
            drive_module.service_account.Credentials,
            "from_service_account_info",
            return_value=MagicMock(),
        ),
        patch.object(drive_module, "build"),
        patch.object(ServiceAccountDriveService, "_validate_configured_folders"),
    ):
        yield


def _json_loads_failing_on(raw: str, leaked: str) -> Callable[..., Any]:
    """A json.loads that rejects ``raw`` with a JSONDecodeError whose message carries ``leaked``
    and parses everything else for real."""
    real_loads = json.loads

    def loads(text: str, *args: Any, **kwargs: Any) -> Any:
        if text == raw:
            raise json.JSONDecodeError(f"cannot parse near {leaked}", text, 0)
        return real_loads(text, *args, **kwargs)

    return loads


def _module_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in _module_records(caplog)]


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


def test_the_exception_fixtures_really_carry_the_names_in_their_text() -> None:
    """Positive control: the leak vector exists, so a clean log is a result and not a vacuum."""
    assert _FOLDER_NAME in str(_http_error(404, _FOLDER_NAME))
    assert _DB_DETAIL in str(_db_error(pg_exceptions.UniqueViolationError, _DB_DETAIL))


@pytest.mark.parametrize(
    "delegated_user",
    [_DELEGATED, None],
    ids=["explicit-address", "default-address"],
)
def test_delegation_line_carries_the_redacted_address_not_the_address(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    delegated_user: str | None,
) -> None:
    monkeypatch.setenv("LOG_PII_HMAC_SALT", "classc-test-salt")
    with (
        _constructor_patches('{"type": "service_account"}'),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
    ):
        caplog.clear()
        service = ServiceAccountDriveService(
            root_folder_id="root-id-1", delegated_user=delegated_user
        )

    # With no delegated_user the service falls back to its own default mailbox. That value is
    # read back from the service at runtime and never written into this file, so the only
    # address a test carries is the synthetic one above.
    raw_address = service.delegated_user
    if delegated_user is not None:
        assert raw_address == delegated_user
    digest = redact_identifier_for_log(raw_address)
    assert digest.startswith("id:")
    _assert_clean(caplog, raw_address)
    assert _lines(caplog) == [
        f"{_OK} Loaded Google credentials from raw JSON",
        f"{_OK} Using Domain-wide delegation, impersonating: {digest}",
    ]


def test_raw_json_parse_failure_logs_the_exception_class_not_its_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_PII_HMAC_SALT", "classc-test-salt")
    creds = base64.b64encode(b'{"type": "service_account"}').decode()
    with (
        _constructor_patches(creds),
        patch.object(drive_module.json, "loads", _json_loads_failing_on(creds, _CRED_DETAIL)),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
    ):
        caplog.clear()
        service = ServiceAccountDriveService(root_folder_id="root-id-1", delegated_user=_DELEGATED)

    assert service.delegated_user == _DELEGATED
    _assert_clean(caplog)
    assert [r.levelname for r in _module_records(caplog)] == ["DEBUG", "INFO", "INFO"]
    assert _lines(caplog) == [
        "SA creds not raw JSON, trying base64: JSONDecodeError",
        f"{_OK} Loaded Google credentials from base64-encoded JSON",
        f"{_OK} Using Domain-wide delegation, impersonating: "
        f"{redact_identifier_for_log(_DELEGATED)}",
    ]


def test_base64_decode_failure_logs_the_exception_class_not_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    leaking = ValueError(f"decode failed near {_CRED_DETAIL}")
    with (
        _constructor_patches("not-json"),
        patch("base64.b64decode", side_effect=leaking),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
        pytest.raises(ValueError, match="Invalid Service Account credentials"),
    ):
        caplog.clear()
        ServiceAccountDriveService(root_folder_id="root-id-1")

    assert _CRED_DETAIL in str(leaking)
    _assert_clean(caplog)
    assert [r.levelname for r in _module_records(caplog)] == ["DEBUG", "DEBUG"]
    assert _lines(caplog) == [
        "SA creds not raw JSON, trying base64: JSONDecodeError",
        "SA creds base64 decode failed: ValueError",
    ]


def test_an_undecodable_credential_byte_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A UnicodeDecodeError names the offending byte of the decoded credential."""
    creds = base64.b64encode(b"\xfe\xfd\xfc").decode()
    with pytest.raises(UnicodeDecodeError) as probe:
        base64.b64decode(creds).decode("utf-8")
    assert "0xfe" in str(probe.value)

    with (
        _constructor_patches(creds),
        caplog.at_level(logging.DEBUG, logger=_LOGGER),
        pytest.raises(ValueError, match="Invalid Service Account credentials"),
    ):
        caplog.clear()
        ServiceAccountDriveService(root_folder_id="root-id-1")

    _assert_clean(caplog, "0xfe", "0xfd")
    assert _lines(caplog) == [
        "SA creds not raw JSON, trying base64: JSONDecodeError",
        "SA creds base64 decode failed: UnicodeDecodeError",
    ]


@pytest.mark.parametrize(
    ("make_error", "expected_tail"),
    [
        pytest.param(
            lambda: _http_error(404, _FOLDER_NAME), "HttpError (HTTP status: 404)", id="http-404"
        ),
        pytest.param(
            lambda: _http_error(403, _FOLDER_NAME), "HttpError (HTTP status: 403)", id="http-403"
        ),
        pytest.param(
            lambda: RuntimeError(f"drive down for {_FOLDER_NAME}"),
            "RuntimeError (HTTP status: None)",
            id="no-resp-attribute",
        ),
        pytest.param(
            lambda: _ResponselessError(f"no response for {_FOLDER_NAME}"),
            "_ResponselessError (HTTP status: None)",
            id="resp-is-none",
        ),
        pytest.param(
            lambda: _StatuslessResponseError(f"no status for {_FOLDER_NAME}"),
            "_StatuslessResponseError (HTTP status: None)",
            id="resp-without-status",
        ),
    ],
)
def test_folder_validation_lines_carry_ids_never_the_folder_name_or_the_error_text(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[], BaseException],
    expected_tail: str,
) -> None:
    files = _FakeFiles(
        get_by_id={
            "root-id-1": {"id": "root-id-1", "name": _FOLDER_NAME, "trashed": False},
            "trashed-id-2": {"id": "trashed-id-2", "name": _FOLDER_NAME, "trashed": True},
            "broken-id-3": make_error(),
        }
    )
    service = _service(
        monkeypatch,
        files,
        google_drive_root_folder_id="trashed-id-2",
        gdrive_individuals_folder_id="broken-id-3",
        gdrive_companies_folder_id=None,
    )
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        service._validate_configured_folders()

    _assert_clean(caplog)
    assert [r.levelname for r in _module_records(caplog)] == ["INFO", "ERROR", "ERROR", "WARNING"]
    assert _lines(caplog) == [
        f"{_OK} Drive folder DRIVE_ROOT_FOLDER_ID (active) OK: root-id-1",
        f"{_ALERT} Drive folder GOOGLE_DRIVE_ROOT_FOLDER_ID (trashed-id-2) is TRASHED"
        " — client folder creation will fail",
        f"{_ALERT} Drive folder GDRIVE_INDIVIDUALS_FOLDER_ID (broken-id-3) UNREACHABLE"
        " — clients of the matching type will fail to get a Drive folder."
        f" Error: {expected_tail}",
        f"{_WARN} Drive folder env GDRIVE_COMPANIES_FOLDER_ID not set — skipping validation",
    ]


@pytest.mark.asyncio
async def test_created_folder_line_carries_the_id_not_the_name(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _FakeFiles(
        create_result={"id": "new-folder-id-7", "webViewLink": "https://drive.example/new"}
    )
    service = _service(monkeypatch, files)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        folder = await service.create_folder(f"11_{_CLIENT_NAME}", parent_id="parent-1")

    assert folder["id"] == "new-folder-id-7"
    assert files.created_bodies == [
        {
            "name": f"11_{_CLIENT_NAME}",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["parent-1"],
        }
    ]
    _assert_clean(caplog)
    (record,) = _module_records(caplog)
    assert record.levelname == "INFO"
    assert record.msg == f"{_OK} Created folder (ID: %s)"
    assert record.args == ("new-folder-id-7",)


@pytest.mark.asyncio
async def test_uploaded_line_carries_the_id_and_the_byte_size_not_the_file_name(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _FakeFiles(
        create_result={"id": "uploaded-file-id-9", "name": _FILE_NAME, "size": "2048"}
    )
    service = _service(monkeypatch, files)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        uploaded = await service.upload_file_to_folder(
            folder_id="folder-1", file_content=b"x" * 2048, file_name=_FILE_NAME
        )

    assert uploaded["size"] == "2048"
    assert files.created_bodies == [{"name": _FILE_NAME, "parents": ["folder-1"]}]
    _assert_clean(caplog)
    (record,) = _module_records(caplog)
    assert record.levelname == "INFO"
    assert record.msg == f"{_OK} Uploaded (ID: %s, %s bytes)"
    assert record.args == ("uploaded-file-id-9", "2048")


@pytest.mark.asyncio
async def test_ensure_fails_closed_when_lookup_fails(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _FakeFiles(list_outcome=_http_error(404, f"11_{_CLIENT_NAME}"))
    service = _service(monkeypatch, files)
    pool = _FakePool()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER), pytest.raises(HttpError):
        await service.ensure_client_folder(
            client_id=11, client_name=_CLIENT_NAME, client_type="individual", db_pool=pool
        )
    assert files.created_bodies == []
    assert "google_drive_folder_id" not in pool.state
    _assert_clean(caplog)


@pytest.mark.asyncio
async def test_ensure_reuse_line_keeps_the_client_id_and_the_folder_id(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _FakeFiles(
        list_outcome={
            "files": [
                {
                    "id": "twin-id",
                    "mimeType": "application/vnd.google-apps.folder",
                    "name": f"11_{_CLIENT_NAME}",
                    "webViewLink": "https://drive.example/twin-id",
                }
            ]
        }
    )
    service = _service(monkeypatch, files)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.ensure_client_folder(
            client_id=11, client_name=_CLIENT_NAME, client_type="individual", db_pool=_FakePool()
        )

    assert result["created"] is False
    assert result["root_folder_id"] == "twin-id"
    _assert_clean(caplog)
    assert _lines(caplog) == [f"{_RECYCLE} Reusing existing Drive folder for client 11: twin-id"]


@pytest.mark.asyncio
async def test_create_client_folder_structure_line_carries_the_client_id_not_the_name(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _FakeFiles()
    service = _service(monkeypatch, files, subfolders=["00_Profile"])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.create_client_folder(
            client_id=12, client_name=_CLIENT_NAME, client_type="company", db_pool=_FakePool()
        )

    assert result["root_folder_id"] == "created-1"
    assert files.created_bodies[0]["name"] == f"12_{_CLIENT_NAME}"
    _assert_clean(caplog)
    assert _lines(caplog) == [
        f"{_OK} Created folder (ID: created-1)",
        f"{_OK} Created folder (ID: created-2)",
        f"{_OK} Drive folder IDs persisted to DB for client 12",
        f"{_OK} Created client folder structure for client 12: created-1",
    ]


@pytest.mark.asyncio
async def test_root_id_persist_failure_logs_the_exception_class_not_its_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _FakeFiles()
    service = _service(monkeypatch, files, subfolders=["00_Profile"])
    pool = _FakePool(
        fail_on={"UPDATE clients": _db_error(pg_exceptions.ConnectionDoesNotExistError, _DB_DETAIL)}
    )
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        with pytest.raises(pg_exceptions.ConnectionDoesNotExistError):
            await service.create_client_folder(
                client_id=12, client_name=_CLIENT_NAME, client_type="company", db_pool=pool
            )
    assert "google_drive_folder_id" not in pool.state
    _assert_clean(caplog, _DB_DETAIL)
    assert _lines(caplog) == [f"{_OK} Created folder (ID: created-1)"]


@pytest.mark.asyncio
async def test_subfolder_failure_logs_ids_class_and_status_not_the_path_or_the_error_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _FakeFiles(create_failures={_SUBFOLDER: _http_error(403, _SUBFOLDER)})
    service = _service(monkeypatch, files, subfolders=[_SUBFOLDER, "00_Profile"])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        subfolders = await service._create_standard_subfolders(
            "root-id-5", client_id=13, db_pool=None, conn=_FakeConn(_FakePool())
        )

    assert list(subfolders) == ["00_Profile"]
    assert subfolders["00_Profile"]["id"] == "created-1"
    _assert_clean(caplog)
    assert [r.levelname for r in _module_records(caplog)] == ["ERROR", "INFO"]
    assert _lines(caplog) == [
        "Failed to create subfolder under root root-id-5 for client 13: HttpError (HTTP status: 403)",
        f"{_OK} Created folder (ID: created-1)",
    ]


@pytest.mark.asyncio
async def test_subfolder_id_persist_failure_logs_the_exception_class_not_its_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _FakeFiles()
    service = _service(monkeypatch, files, subfolders=["00_Profile"])
    pool = _FakePool(
        fail_on={
            "INSERT INTO client_drive_subfolders": _db_error(
                pg_exceptions.UniqueViolationError, _DB_DETAIL
            )
        }
    )
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        subfolders = await service._create_standard_subfolders(
            "root-id-6", client_id=14, db_pool=pool
        )

    assert list(subfolders) == ["00_Profile"]
    _assert_clean(caplog, _DB_DETAIL)
    assert [r.levelname for r in _module_records(caplog)] == ["INFO", "ERROR"]
    assert _lines(caplog) == [
        f"{_OK} Created folder (ID: created-1)",
        "Failed to persist drive folder IDs for client 14: UniqueViolationError",
    ]
