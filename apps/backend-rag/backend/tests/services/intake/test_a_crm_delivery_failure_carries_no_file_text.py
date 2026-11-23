"""A CRM delivery failure names a status or an error type with fixed text, never a file's text.

The body of a failed CRM answer, the text of an exception and a traceback can all quote the
uploaded file's name or path. Each failure channel of the delivery (the log, the returned
result, the commit audit row) is checked with a planted file name. NO database, NO network:
HTTP is an httpx.MockTransport in the crm_push client and the pool is a fake.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
import pytest_asyncio

from backend.services.intake import crm_delivery, crm_push, writer

pytestmark = pytest.mark.asyncio

PLANTED = "Zzq Client passport scan.pdf"
FLY_CLIENT_ID = 4321
COMPANY_UUID = str(uuid.UUID(int=7))


@pytest.fixture
def blob(tmp_path: Path) -> Path:
    path = tmp_path / PLANTED
    path.write_bytes(b"%PDF-1.4 synthetic bytes")
    return path


@pytest_asyncio.fixture
async def answer() -> AsyncIterator[Callable[..., None]]:
    saved = crm_push._client

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def _wrapped(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/upsert-by-phone"):
                return httpx.Response(
                    200,
                    json={"client_id": FLY_CLIENT_ID, "was_created": False, "matched_count": 1},
                )
            return handler(request)

        crm_push._client = httpx.AsyncClient(transport=httpx.MockTransport(_wrapped))

    yield install
    if crm_push._client is not None and not crm_push._client.is_closed:
        await crm_push._client.aclose()
    crm_push._client = saved


def _client_push(blob: Path) -> Any:
    return crm_push.push_committed_document(
        bearer_token="jwt-token-abc",
        client_id=77,
        file_name=PLANTED,
        document_type="passport",
        blob_path=str(blob),
        practice_uuid="11111111-2222-4333-8444-555555555555",
        document_category="immigration",
        mime_type="application/pdf",
        base_url="http://fly.test",
        sender_phone="+62 840-0022-0000",
        crm_write_key="service-key-abc",
    )


def _company_push(blob: Path) -> Any:
    return crm_push.push_committed_company_document(
        bearer_token=None,
        crm_write_key="service-key-abc",
        company_id=42,
        company_uuid=COMPANY_UUID,
        file_name=PLANTED,
        document_type="nib",
        blob_path=str(blob),
        mime_type="application/pdf",
        base_url="http://fly.test",
    )


def _no_planted_text(caplog: pytest.LogCaptureFixture, *channels: Any) -> None:
    assert PLANTED not in caplog.text
    for record in caplog.records:
        assert record.exc_info is None
        assert PLANTED not in record.getMessage()
    for channel in channels:
        assert PLANTED not in repr(channel)


@pytest.mark.parametrize("push", [_client_push, _company_push], ids=["client", "company"])
@pytest.mark.parametrize(
    ("status_code", "outcome", "detail"),
    [
        (500, "server_error", "HTTP 500: the CRM failed to answer"),
        (403, "denied_rbac", "HTTP 403: the CRM refused the credential"),
        (422, "rejected", "HTTP 422: the CRM rejected the request"),
    ],
)
async def test_a_failed_answer_is_named_by_its_status_and_never_by_its_body(
    answer, blob, caplog, push, status_code, outcome, detail
):
    caplog.set_level(logging.DEBUG)
    answer(lambda _request: httpx.Response(status_code, text=f"cannot store {PLANTED}"))

    result = await push(blob)

    assert (result.status, result.detail) == (outcome, detail)
    _no_planted_text(caplog, result)


async def test_a_rate_limited_answer_is_named_by_its_status(answer, blob, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(crm_push.asyncio, "sleep", AsyncMock())
    answer(lambda _request: httpx.Response(429, text=f"slow down for {PLANTED}"))

    result = await _client_push(blob)

    assert (result.status, result.detail) == ("rejected", "HTTP 429: the CRM is rate limiting")
    _no_planted_text(caplog, result)


async def test_a_failed_client_lookup_is_logged_by_its_status_alone(answer, blob, caplog):
    caplog.set_level(logging.DEBUG)
    saved = crm_push._client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"lookup failed near {PLANTED}")

    crm_push._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await _client_push(blob)
    finally:
        await crm_push._client.aclose()
        crm_push._client = saved

    assert result.status == "identity_unresolved"
    _no_planted_text(caplog, result)


@pytest.mark.parametrize("push", [_client_push, _company_push], ids=["client", "company"])
async def test_a_call_that_gets_no_answer_is_named_by_its_type(answer, blob, caplog, push):
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot send {PLANTED}", request=request)

    answer(handler)
    result = await push(blob)

    assert (result.status, result.detail) == (
        "unreachable",
        "ConnectError: the CRM could not be reached",
    )
    _no_planted_text(caplog, result)


@pytest.mark.parametrize("push", [_client_push, _company_push], ids=["client", "company"])
async def test_an_unexpected_error_is_logged_by_its_type_without_a_traceback(
    answer, blob, caplog, push
):
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"broke while reading {PLANTED}")

    answer(handler)
    result = await push(blob)

    assert (result.status, result.detail) == ("error", "RuntimeError: unexpected delivery error")
    assert any("err=RuntimeError" in record.getMessage() for record in caplog.records)
    _no_planted_text(caplog, result)


@pytest.mark.parametrize("push", [_client_push, _company_push], ids=["client", "company"])
async def test_a_blob_that_cannot_be_read_is_named_by_its_type(answer, blob, caplog, push):
    caplog.set_level(logging.DEBUG)
    answer(lambda _request: httpx.Response(200, json={"success": True, "document_id": 1}))
    blob.unlink()

    result = await push(blob)

    assert (result.status, result.detail) == (
        "missing_blob",
        "FileNotFoundError: blob could not be read",
    )
    _no_planted_text(caplog, result)


class _Acquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FailingConn:
    """Every read and every write fails with an error whose text quotes the file."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        raise self.error

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        raise self.error


class _Pool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "No such file or directory", f"/srv/intake/{PLANTED}"),
        asyncpg.PostgresError(f"DETAIL: Key (blob_path)=({PLANTED}) already exists."),
    ],
    ids=["file", "database"],
)
async def test_a_database_or_file_error_reaches_no_log_result_or_audit_row(
    caplog, monkeypatch, error
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("INTAKE_CRM_PUSH_ENABLED", "1")
    conn = _FailingConn(error)
    plan = writer.CommitPlan(
        proposal_id=501,
        queue_id=8801,
        client_id=77,
        practice_id=None,
        decision="NEEDS_REVIEW",
        doc_type="passport",
        committed_by="tester",
        idempotency_key="idem-no-text",
        payload={"file_name": "scan.pdf", "document_type": "passport"},
    )
    result = writer.CommitResult(
        proposal_id=501,
        dry_run=False,
        outcome="committed",
        doc_id=None,
        practice_id=None,
        audit_id=902,
    )

    out = await crm_delivery.deliver_committed_to_crm(
        pool=_Pool(conn),
        queue_id=8801,
        plan=plan,
        result=result,
        bearer_token="jwt-token-abc",
        crm_write_key="service-key-abc",
    )

    assert out["status"] == "error"
    assert out["detail"] == f"{type(error).__name__}: delivery could not be completed"
    assert conn.calls, "the audit row write was attempted"
    assert any("bookkeeping_failed" in record.getMessage() for record in caplog.records)
    _no_planted_text(caplog, out, conn.calls)
