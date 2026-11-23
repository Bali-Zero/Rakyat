"""Transient CRM failures wait once before their one retry."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from backend.services.intake import crm_push

pytestmark = pytest.mark.asyncio

PHONE = "+62 840-0022-0000"
CLIENT_ID = 900321
COMPANY_UUID = "11111111-2222-4333-8444-555555555555"
UPSERT = "/api/crm/clients/upsert-by-phone"
UPLOAD_OK = {"success": True, "document_id": 9, "file_url": None}
COMPANY_OK = {"success": True, "company_document_id": 12, "file_url": None}


@pytest.fixture
def blob(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    return path


@pytest_asyncio.fixture
async def events(monkeypatch) -> AsyncIterator[list[object]]:
    recorded: list[object] = []

    async def sleep(seconds: float) -> None:
        recorded.append(("wait", seconds))

    monkeypatch.setattr(crm_push.asyncio, "sleep", sleep)
    yield recorded


@pytest_asyncio.fixture
async def install() -> AsyncIterator[Callable[[Callable[[httpx.Request], httpx.Response]], None]]:
    saved = crm_push._client

    def use(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        crm_push._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    yield use
    if crm_push._client is not None and not crm_push._client.is_closed:
        await crm_push._client.aclose()
    crm_push._client = saved


def _client_kwargs(blob: Path) -> dict[str, object]:
    return {
        "bearer_token": None,
        "crm_write_key": "synthetic-key",
        "client_id": 42,
        "file_name": "synthetic.pdf",
        "document_type": "passport",
        "blob_path": str(blob),
        "base_url": "http://fly.test",
        "sender_phone": PHONE,
    }


def _company_kwargs(blob: Path, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "bearer_token": None,
        "crm_write_key": "synthetic-key",
        "company_id": 42,
        "company_uuid": COMPANY_UUID,
        "file_name": "synthetic.pdf",
        "document_type": "nib",
        "blob_path": str(blob),
        "base_url": "http://fly.test",
    }
    kwargs.update(overrides)
    return kwargs


def _upsert_ok() -> httpx.Response:
    return httpx.Response(200, json={"client_id": CLIENT_ID, "was_created": False})


def _temporary(kind: str, request: httpx.Request) -> httpx.Response:
    if kind == "connect":
        raise httpx.ConnectError("synthetic connection failed", request=request)
    return httpx.Response(503, text="synthetic unavailable")


@pytest.mark.parametrize("kind", ["server", "connect"])
async def test_a_temporary_lookup_waits_then_reaches_the_upload(blob, events, install, kind):
    answers = [kind, "ok", "upload"]

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("lookup" if request.url.path == UPSERT else "upload")
        answer = answers.pop(0)
        if answer in {"server", "connect"}:
            return _temporary(answer, request)
        return _upsert_ok() if answer == "ok" else httpx.Response(200, json=UPLOAD_OK)

    install(handler)
    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    assert events == ["lookup", ("wait", 2.0), "lookup", "upload"]


@pytest.mark.parametrize(
    ("answers", "status", "detail", "wait"),
    [
        (["server", "server"], "server_error", "HTTP 503: the CRM failed to answer", 2.0),
        (["connect", "connect"], "unreachable", "ConnectError: the CRM could not be reached", 2.0),
        (["connect", "server"], "server_error", "HTTP 503: the CRM failed to answer", 2.0),
        (["server", "connect"], "unreachable", "ConnectError: the CRM could not be reached", 2.0),
        (["limited", "server"], "server_error", "HTTP 503: the CRM failed to answer", 2.0),
        (["limited", "connect"], "unreachable", "ConnectError: the CRM could not be reached", 2.0),
        (["server", "limited"], "identity_unresolved", None, 2.0),
    ],
)
async def test_a_failed_lookup_uses_its_last_answer(
    blob, events, install, answers, status, detail, wait
):
    queued = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("lookup" if request.url.path == UPSERT else "upload")
        answer = queued.pop(0)
        if answer in {"server", "connect"}:
            return _temporary(answer, request)
        return httpx.Response(429) if answer == "limited" else _upsert_ok()

    install(handler)
    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.status == status
    if detail is not None:
        assert result.detail == detail
    assert events == ["lookup", ("wait", wait), "lookup"]


async def test_a_refused_lookup_does_not_wait_or_upload(blob, events, install):
    def handler(request: httpx.Request) -> httpx.Response:
        events.append("lookup" if request.url.path == UPSERT else "upload")
        return httpx.Response(422)

    install(handler)
    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.status == "identity_unresolved"
    assert events == ["lookup"]


@pytest.mark.parametrize("kind", ["server", "connect"])
async def test_a_temporary_client_upload_waits_before_its_retry(blob, events, install, kind):
    answers = ["ok", kind, "upload"]

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("lookup" if request.url.path == UPSERT else "upload")
        answer = answers.pop(0)
        if answer in {"server", "connect"}:
            return _temporary(answer, request)
        return _upsert_ok() if answer == "ok" else httpx.Response(200, json=UPLOAD_OK)

    install(handler)
    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    assert events[-3:] == ["upload", ("wait", 2.0), "upload"]


@pytest.mark.parametrize("kind", ["server", "connect"])
async def test_a_temporary_company_upload_waits_before_its_retry(blob, events, install, kind):
    answers = [kind, "ok"]

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("upload")
        answer = answers.pop(0)
        if answer in {"server", "connect"}:
            return _temporary(answer, request)
        return httpx.Response(200, json=COMPANY_OK)

    install(handler)
    result = await crm_push.push_committed_company_document(**_company_kwargs(blob))

    assert result.ok is True
    assert events == ["upload", ("wait", 2.0), "upload"]


async def test_lookup_and_upload_keep_separate_retry_budgets(blob, events, install):
    answers = ["server", "ok", "server", "upload"]

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("lookup" if request.url.path == UPSERT else "upload")
        answer = answers.pop(0)
        if answer == "server":
            return _temporary(answer, request)
        return _upsert_ok() if answer == "ok" else httpx.Response(200, json=UPLOAD_OK)

    install(handler)
    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    assert events == [
        "lookup",
        ("wait", 2.0),
        "lookup",
        "upload",
        ("wait", 2.0),
        "upload",
    ]


async def test_company_credential_fallback_keeps_its_retry_budget(blob, events, install):
    answers = [403, 503, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("upload")
        return httpx.Response(answers.pop(0), json=COMPANY_OK)

    install(handler)
    result = await crm_push.push_committed_company_document(
        **_company_kwargs(blob, bearer_token="synthetic-bearer")
    )

    assert result.ok is True
    assert events == ["upload", "upload", ("wait", 2.0), "upload"]


async def test_company_fallback_does_not_refill_a_spent_retry(blob, events, install):
    answers = [503, 403, 503]

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("upload")
        return httpx.Response(answers.pop(0))

    install(handler)
    result = await crm_push.push_committed_company_document(
        **_company_kwargs(blob, bearer_token="synthetic-bearer")
    )

    assert result.status == "server_error"
    assert events == ["upload", ("wait", 2.0), "upload", "upload"]
