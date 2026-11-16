"""The listener's send audit: what is recorded for a send, and what never is.

Synthetic data only; no network and no database (the PG side is test_listener_emails_logged_pg).
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.services.crm import practice_status_listener as mod
from backend.services.notifications import email_audit

ADDR = "gate.synthetic@example.invalid"
CLIENT_TYPES = [
    "completion_client",
    "status_submitted_client",
    "status_approved_client",
    "payment_client",
]


def _listener() -> mod.PracticeStatusListener:
    return mod.PracticeStatusListener("unused", MagicMock())


def _http_client(response: httpx.Response) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    return client


def _answer(payload: dict) -> httpx.Response:
    return httpx.Response(
        200, request=httpx.Request("POST", "https://mail.invalid/send"), json=payload
    )


@pytest.mark.parametrize("kind", CLIENT_TYPES)
def test_client_types_are_never_resent_as_a_retry_stub(kind):
    assert kind in email_audit.NON_RESURRECTABLE_EMAIL_TYPES


@pytest.mark.asyncio
async def test_a_200_with_success_false_is_recorded_failed_not_sent():
    """Gate P1: the endpoint answers 200 + success=false when every provider failed."""
    listener = _listener()
    answer = _answer({"success": False, "message": "All providers failed: brevo, resend, zoho"})
    with (
        patch.object(mod, "log_email_attempt", AsyncMock(return_value=7)),
        patch.object(mod, "record_email_result", AsyncMock()) as result,
        patch.object(mod, "notify_email_failure_critical") as page,
        patch.object(mod.httpx, "AsyncClient", return_value=_http_client(answer)),
        pytest.raises(mod.EmailNotDelivered),
    ):
        await listener._send_logged("completion_client", 1, 1, ADDR, "synthetic", "body")
    assert result.await_args.kwargs["status"] == "failed"
    assert result.await_args.kwargs["provider"] is None
    page.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Email sent to x@example.invalid via zoho", "zoho"),
        ("Email sent to x@example.invalid via brevo", "brevo"),
        ("Email sent to x@example.invalid via resend", None),  # not an allowed audit provider
        ("Email accepted", None),
    ],
)
async def test_the_provider_recorded_is_the_one_the_endpoint_reports(message, expected):
    """Gate P2: never assume Brevo; NULL when the endpoint does not say or the table cannot hold it."""
    listener = _listener()
    answer = _answer({"success": True, "message": message})
    with (
        patch.object(mod, "log_email_attempt", AsyncMock(return_value=7)),
        patch.object(mod, "record_email_result", AsyncMock()) as result,
        patch.object(mod.httpx, "AsyncClient", return_value=_http_client(answer)),
    ):
        await listener._send_logged("payment_team", 1, 1, ADDR, "synthetic", "body")
    assert result.await_args.kwargs["status"] == "sent"
    assert result.await_args.kwargs["provider"] == expected


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_still_counts_as_sent_with_unknown_provider():
    listener = _listener()
    answer = httpx.Response(
        200, request=httpx.Request("POST", "https://mail.invalid/send"), content=b"ok"
    )
    with (
        patch.object(mod, "log_email_attempt", AsyncMock(return_value=7)),
        patch.object(mod, "record_email_result", AsyncMock()) as result,
        patch.object(mod.httpx, "AsyncClient", return_value=_http_client(answer)),
    ):
        await listener._send_logged("payment_team", 1, 1, ADDR, "synthetic", "body")
    assert result.await_args.kwargs["status"] == "sent"
    assert result.await_args.kwargs["provider"] is None


@pytest.mark.asyncio
async def test_the_stored_error_never_echoes_the_recipient():
    """Gate P2: provider errors often repeat the address; error_message must not."""
    listener = _listener()
    listener._send_via_internal_api = AsyncMock(
        side_effect=RuntimeError("rejected recipient " + ADDR)
    )
    with (
        patch.object(mod, "log_email_attempt", AsyncMock(return_value=7)),
        patch.object(mod, "record_email_result", AsyncMock()) as result,
        patch.object(mod, "notify_email_failure_critical") as page,
        pytest.raises(RuntimeError),
    ):
        await listener._send_logged("completion_client", 1, 1, ADDR, "synthetic", "body")
    assert ADDR not in result.await_args.kwargs["error_message"]
    assert ADDR not in page.call_args.kwargs["error"]


@pytest.mark.asyncio
async def test_send_logged_reraises_the_original_exception():
    listener = _listener()
    problem = RuntimeError("synthetic failure")
    listener._send_via_internal_api = AsyncMock(side_effect=problem)
    with (
        patch.object(mod, "log_email_attempt", AsyncMock(return_value=7)),
        patch.object(mod, "record_email_result", AsyncMock()) as result,
        patch.object(mod, "notify_email_failure_critical"),
        pytest.raises(RuntimeError) as raised,
    ):
        await listener._send_logged("completion_client", 1, 1, ADDR, "synthetic", "body")
    assert raised.value is problem
    assert result.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_the_milestone_failure_log_redacts_the_recipient(caplog):
    listener = _listener()
    listener._process_svc._fetch_practice_data = AsyncMock(return_value={"client_id": 1})
    listener._process_svc._fetch_client_data = AsyncMock(
        return_value={"full_name": "Synthetic", "email": ADDR}
    )
    listener._send_via_internal_api = AsyncMock(side_effect=RuntimeError("synthetic failure"))
    with (
        patch.object(mod, "log_email_attempt", AsyncMock(return_value=7)),
        patch.object(mod, "record_email_result", AsyncMock()),
        patch.object(mod, "notify_email_failure_critical"),
        caplog.at_level(logging.INFO),
    ):
        await listener._send_milestone_email(1, "completed", "system")
    records = [r for r in caplog.records if r.name == mod.__name__]
    assert records
    assert ADDR not in "\n".join(r.getMessage() for r in records)
