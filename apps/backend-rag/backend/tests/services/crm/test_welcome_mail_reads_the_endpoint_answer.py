"""The client welcome mail counts as sent only when the send endpoint says it was sent.

The endpoint answers HTTP 200 with ``success: false`` when every provider failed. The welcome
sender looked at the status code only: the mail was recorded as sent in the audit row and in the
client's alerts, and nobody was told, while the client had received nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import backend.services.crm.welcome.welcome_email_service as welcome

ALL_PROVIDERS_FAILED = {"success": False, "message": "All providers failed: brevo 401"}
UNCERTAIN = {"success": False, "message": "delivery uncertain", "delivery_uncertain": True}
SENT = {"success": True, "message": "Email sent"}


async def _send(monkeypatch: pytest.MonkeyPatch, status: int, body: Any) -> dict[str, Any]:
    """One run of the welcome sender for a client with an address; what it recorded."""
    connection = AsyncMock()
    connection.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "full_name": "Zz Client",
            "email": "client@example.test",
            "nationality": "Italian",
            "assigned_to": None,
        }
    )
    connection.fetchval = AsyncMock(return_value=None)  # not sent before
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=connection)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquired)

    posts: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # the brochure: not available in this test
            return httpx.Response(404)
        posts.append(request)
        return httpx.Response(status, json=body)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(answer), **kwargs)

    recorded = AsyncMock()
    paged = MagicMock()
    monkeypatch.setattr(welcome, "httpx", SimpleNamespace(AsyncClient=client))
    monkeypatch.setattr(welcome, "_BROCHURE_PATH", Path("/nonexistent/brochure.pdf"))
    monkeypatch.setattr(welcome, "get_email_client", AsyncMock(return_value=client()))
    monkeypatch.setattr(welcome, "log_email_attempt", AsyncMock(return_value=11))
    monkeypatch.setattr(welcome, "record_email_result", recorded)
    monkeypatch.setattr(welcome, "notify_email_failure_critical", paged)

    await welcome._send_client_welcome_impl(7, pool)

    assert len(posts) == 1
    return {
        "audit": recorded.await_args.kwargs,
        "paged": [call.kwargs for call in paged.call_args_list],
        "alert_rows": connection.execute.await_count,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(ALL_PROVIDERS_FAILED, id="every provider failed"),
        pytest.param(UNCERTAIN, id="delivery uncertain"),
    ],
)
async def test_a_welcome_the_endpoint_did_not_send_is_recorded_as_failed(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    seen = await _send(monkeypatch, 200, body)

    assert seen["audit"]["status"] == "failed"
    assert seen["audit"]["error_message"] == f"not delivered: {body['message']}"
    assert [page["email_type"] for page in seen["paged"]] == ["welcome"]
    assert seen["alert_rows"] == 0  # no "welcome sent" row for the client


@pytest.mark.asyncio
async def test_a_welcome_the_endpoint_sent_is_recorded_as_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _send(monkeypatch, 200, SENT)

    assert seen["audit"]["status"] == "sent"
    assert seen["paged"] == []
    assert seen["alert_rows"] == 1


@pytest.mark.asyncio
async def test_a_welcome_refused_with_an_error_status_is_recorded_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _send(monkeypatch, 500, {"detail": "boom"})

    assert seen["audit"]["status"] == "failed"
    assert seen["audit"]["error_message"].startswith("status=500 body=")
    assert [page["email_type"] for page in seen["paged"]] == ["welcome"]
    assert seen["alert_rows"] == 0
