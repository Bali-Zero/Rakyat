"""The magic-link mail shows the client's name as text, never as markup."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

import backend.app.routers.auth as auth_module

LOOPBACK_SINK = "http://127.0.0.1:18181/api/notifications/send-email"
LINK = "http://127.0.0.1:3101/portal/magic?token=synthetic-token"


class _Response:
    def raise_for_status(self) -> None:
        return None


class _RecordingClient:
    sent: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> _Response:
        _RecordingClient.sent.append(json)
        return _Response()


async def _body_for(name: str | None, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("INTERNAL_EMAIL_API_URL", LOOPBACK_SINK)
    monkeypatch.setenv("NUZANTARA_API_KEY", "synthetic-test-key")
    _RecordingClient.sent = []
    with patch("httpx.AsyncClient", _RecordingClient):
        sent = await auth_module._send_magic_link_email("portal-active@example.com", name, LINK)
    assert sent is True
    assert len(_RecordingClient.sent) == 1
    return _RecordingClient.sent[0]["body"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_markup_in_the_name_is_shown_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _body_for('<a href="https://evil.example">Ann</a> & "Co" <img src=x>', monkeypatch)

    assert body.startswith(
        "Hello &lt;a href=&quot;https://evil.example&quot;&gt;Ann&lt;/a&gt; "
        "&amp; &quot;Co&quot; &lt;img src=x&gt;,<br><br>"
    )
    assert 'evil.example">' not in body
    assert "<img" not in body
    assert body.count("<a href=") == 1
    assert f'<a href="{LINK}">' in body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_plain_name_reads_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _body_for("Synthetic Client", monkeypatch)

    assert body.startswith("Hello Synthetic Client,<br><br>")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_name_greets_without_one(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _body_for(None, monkeypatch)

    assert body.startswith("Hello,<br><br>")
