"""A 429 from the CRM is a pause, not a verdict: each delivery call retries it once.

The three calls of ``crm_push`` (phone resolution, client document, company document)
treated a 429 as a plain refusal. They now wait a short, bounded time and try once more,
out of the same single retry a transport error or a 5xx already had. A second refusal
ends the way a refused request always ended.

No database, no network: HTTP is an ``httpx.MockTransport`` and the wait is recorded
instead of slept. Invented values only.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from backend.services.intake import crm_push

pytestmark = pytest.mark.asyncio

FLY_CLIENT_ID = 900321
PHONE = "+62 840-0022-0000"
PHONE_DIGITS = "628400220000"
COMPANY_UUID = "11111111-2222-4333-8444-555555555555"
UPSERT_PATH = "/api/crm/clients/upsert-by-phone"
UPLOAD_OK = {"success": True, "document_id": 9, "file_url": None}
COMPANY_OK = {"success": True, "company_document_id": 12, "file_url": None}


@pytest.fixture
def blob(tmp_path: Path) -> Path:
    p = tmp_path / "scan.pdf"
    p.write_bytes(b"%PDF-1.4 invented bytes")
    return p


@pytest_asyncio.fixture
async def mock_client() -> AsyncIterator[Callable[..., list[httpx.Request]]]:
    captured: list[httpx.Request] = []
    saved = crm_push._client

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        def _wrapped(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return handler(request)

        crm_push._client = httpx.AsyncClient(transport=httpx.MockTransport(_wrapped))
        return captured

    yield install

    if crm_push._client is not None and not crm_push._client.is_closed:
        await crm_push._client.aclose()
    crm_push._client = saved


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every real wait the module asks for, without waiting."""
    recorded: list[float] = []

    async def _no_wait(seconds: float) -> None:
        if seconds:
            recorded.append(seconds)

    monkeypatch.setattr(crm_push.asyncio, "sleep", _no_wait)
    return recorded


def _limited(retry_after: str | bytes | None = None) -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(429, json={"detail": "Rate limit exceeded"}, headers=headers)


def _upsert_ok() -> httpx.Response:
    return httpx.Response(
        200, json={"client_id": FLY_CLIENT_ID, "was_created": False, "matched_count": 1}
    )


def _serve(upserts: list[httpx.Response], uploads: list[httpx.Response]):
    """Answer the phone resolution and the upload from two separate scripts."""
    upsert_iter, upload_iter = iter(upserts), iter(uploads)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == UPSERT_PATH:
            return next(upsert_iter)
        return next(upload_iter)

    return handler


def _client_kwargs(blob: Path) -> dict:
    return {
        "bearer_token": "jwt-stub",
        "client_id": 77,
        "file_name": "scan.pdf",
        "document_type": "passport",
        "blob_path": str(blob),
        "base_url": "http://fly.test",
        "sender_phone": PHONE,
        "crm_write_key": "key-stub",
    }


def _company_kwargs(blob: Path, **overrides: object) -> dict:
    kwargs: dict = {
        "bearer_token": None,
        "crm_write_key": "key-stub",
        "company_id": 42,
        "company_uuid": COMPANY_UUID,
        "file_name": "scan.pdf",
        "document_type": "nib",
        "blob_path": str(blob),
        "base_url": "http://fly.test",
    }
    kwargs.update(overrides)
    return kwargs


def _paths(requests: list[httpx.Request]) -> list[str]:
    return [r.url.path for r in requests]


# --------------------------------------------------------------------------- #
# Client document upload
# --------------------------------------------------------------------------- #
async def test_client_upload_limited_once_is_retried_and_delivered(mock_client, blob, waits):
    requests = mock_client(
        _serve([_upsert_ok()], [_limited("3"), httpx.Response(200, json=UPLOAD_OK)])
    )

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    assert result.status == "pushed"
    assert len(requests) == 3  # resolution + refused upload + retried upload
    assert waits == [3.0]


async def test_client_upload_limited_twice_ends_as_a_refused_request(mock_client, blob, waits):
    requests = mock_client(_serve([_upsert_ok()], [_limited(), _limited()]))

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is False
    assert result.status == "rejected"
    assert (result.detail or "").startswith("HTTP 429")
    assert len(requests) == 3  # exactly one retry
    assert waits == [2.0]  # and exactly one wait


async def test_client_upload_limited_then_server_error_is_not_retried_again(
    mock_client, blob, waits
):
    requests = mock_client(_serve([_upsert_ok()], [_limited(), httpx.Response(500, text="down")]))

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.status == "server_error"
    assert len(requests) == 3
    assert waits == [2.0]


async def test_client_upload_server_error_then_limited_spends_the_same_single_retry(
    mock_client, blob, waits
):
    requests = mock_client(_serve([_upsert_ok()], [httpx.Response(500, text="down"), _limited()]))

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.status == "rejected"
    assert len(requests) == 3
    assert waits == []  # the retry was already spent: nothing to wait for


@pytest.mark.parametrize(
    ("retry_after", "expected_wait"),
    [
        (None, 2.0),
        ("", 2.0),
        ("abc", 2.0),
        ("-3", 2.0),
        ("0", 2.0),
        ("inf", 2.0),
        ("nan", 2.0),
        ("1.5", 2.0),
        (b"\xb2", 2.0),  # a superscript two: a digit to str.isdigit, not a number
        ("Wed, 21 Oct 2026 07:28:00 GMT", 2.0),
        ("1", 1.0),
        (" 4 ", 4.0),
        ("5", 5.0),
        ("6", 5.0),
        ("999", 5.0),
        ("99999999999999999999", 5.0),
        # More digits than int() converts (it refuses over 4,300): still a bounded wait.
        ("9" * 4301, 5.0),
        ("1" + "0" * 6000, 5.0),
        ("0" * 5000, 2.0),
        ("0" * 5000 + "3", 3.0),
        ("00", 2.0),
        ("003", 3.0),
        ("007", 5.0),
    ],
)
async def test_the_wait_is_retry_after_kept_between_one_and_five_seconds(
    mock_client, blob, waits, retry_after, expected_wait
):
    mock_client(
        _serve([_upsert_ok()], [_limited(retry_after), httpx.Response(200, json=UPLOAD_OK)])
    )

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    assert waits == [expected_wait]


# --------------------------------------------------------------------------- #
# Phone resolution (the call that runs before every client upload)
# --------------------------------------------------------------------------- #
async def test_resolution_limited_once_is_retried_and_the_upload_runs(mock_client, blob, waits):
    requests = mock_client(
        _serve([_limited("1"), _upsert_ok()], [httpx.Response(200, json=UPLOAD_OK)])
    )

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    assert _paths(requests)[:2] == [UPSERT_PATH, UPSERT_PATH]
    assert len(requests) == 3
    assert waits == [1.0]


async def test_resolution_survives_a_retry_after_too_long_to_be_a_number(mock_client, blob, waits):
    # The resolution runs outside the upload's error handling: an exception here would
    # leave push_committed_document itself.
    requests = mock_client(
        _serve([_limited("9" * 4301), _upsert_ok()], [httpx.Response(200, json=UPLOAD_OK)])
    )

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    assert _paths(requests)[:2] == [UPSERT_PATH, UPSERT_PATH]
    assert waits == [5.0]


async def test_resolution_limited_twice_fails_closed_without_an_upload(mock_client, blob, waits):
    requests = mock_client(_serve([_limited(), _limited()], []))

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is False
    assert result.status == "identity_unresolved"
    assert _paths(requests) == [UPSERT_PATH, UPSERT_PATH]
    assert waits == [2.0]


async def test_resolution_and_upload_each_have_their_own_retry(mock_client, blob, waits):
    requests = mock_client(
        _serve(
            [_limited("1"), _upsert_ok()],
            [_limited("4"), httpx.Response(200, json=UPLOAD_OK)],
        )
    )

    result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    assert len(requests) == 4
    assert waits == [1.0, 4.0]


async def test_the_rate_limited_log_names_no_phone_and_no_body(mock_client, blob, waits, caplog):
    mock_client(
        _serve([_limited(), _upsert_ok()], [_limited(), httpx.Response(200, json=UPLOAD_OK)])
    )

    with caplog.at_level(logging.WARNING, logger=crm_push.logger.name):
        result = await crm_push.push_committed_document(**_client_kwargs(blob))

    assert result.ok is True
    limited = [r.getMessage() for r in caplog.records if "rate_limited" in r.getMessage()]
    assert len(limited) == 2
    for line in limited:
        assert PHONE_DIGITS not in line
        assert "Rate limit exceeded" not in line


# --------------------------------------------------------------------------- #
# Company document upload
# --------------------------------------------------------------------------- #
async def test_company_upload_limited_once_is_retried_and_delivered(mock_client, blob, waits):
    responses = iter([_limited("2"), httpx.Response(200, json=COMPANY_OK)])
    requests = mock_client(lambda r: next(responses))

    result = await crm_push.push_committed_company_document(**_company_kwargs(blob))

    assert result.ok is True
    assert result.fly_company_doc_id == 12
    assert len(requests) == 2
    assert waits == [2.0]


async def test_company_upload_survives_a_retry_after_too_long_to_be_a_number(
    mock_client, blob, waits
):
    responses = iter([_limited("9" * 4301), httpx.Response(200, json=COMPANY_OK)])
    requests = mock_client(lambda r: next(responses))

    result = await crm_push.push_committed_company_document(**_company_kwargs(blob))

    assert result.ok is True
    assert len(requests) == 2
    assert waits == [5.0]


async def test_company_upload_limited_twice_ends_as_a_refused_request(mock_client, blob, waits):
    requests = mock_client(lambda r: _limited())

    result = await crm_push.push_committed_company_document(**_company_kwargs(blob))

    assert result.ok is False
    assert result.status == "rejected"
    assert len(requests) == 2
    assert waits == [2.0]


async def test_company_upload_limited_then_server_error_is_not_retried_again(
    mock_client, blob, waits
):
    responses = iter([_limited(), httpx.Response(500, text="down")])
    requests = mock_client(lambda r: next(responses))

    result = await crm_push.push_committed_company_document(**_company_kwargs(blob))

    assert result.status == "server_error"
    assert len(requests) == 2
    assert waits == [2.0]


async def test_company_fallback_to_the_service_key_survives_a_limited_answer(
    mock_client, blob, waits
):
    """Staff token refused, service key used, service key limited once, then delivered."""
    responses = iter(
        [
            httpx.Response(403, json={"detail": "forbidden"}),
            _limited("1"),
            httpx.Response(200, json=COMPANY_OK),
        ]
    )
    requests = mock_client(lambda r: next(responses))

    result = await crm_push.push_committed_company_document(
        **_company_kwargs(blob, bearer_token="jwt-stub")
    )

    assert result.ok is True
    assert len(requests) == 3
    assert "authorization" in requests[0].headers
    for retried in requests[1:]:
        assert "/internal/" in retried.url.path
        assert retried.headers.get("x-crm-write-key") == "key-stub"
        assert "authorization" not in retried.headers
    assert waits == [1.0]


async def test_company_limited_answer_does_not_cancel_the_fallback(mock_client, blob, waits):
    """Staff token limited once, then refused: the service key is still tried."""
    responses = iter(
        [
            _limited("1"),
            httpx.Response(403, json={"detail": "forbidden"}),
            httpx.Response(200, json=COMPANY_OK),
        ]
    )
    requests = mock_client(lambda r: next(responses))

    result = await crm_push.push_committed_company_document(
        **_company_kwargs(blob, bearer_token="jwt-stub")
    )

    assert result.ok is True
    assert len(requests) == 3
    assert "authorization" in requests[0].headers
    assert "authorization" in requests[1].headers  # the retry keeps the credential in use
    assert requests[2].headers.get("x-crm-write-key") == "key-stub"
    assert waits == [1.0]
