"""Guilt and innocence tests for the reactivation campaign mail loop."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://synthetic.invalid/reactivation")

_MODULE_PATH = Path(__file__).resolve().parents[4] / "scripts" / "reactivation_email_campaign.py"
_SPEC = importlib.util.spec_from_file_location("reactivation_email_campaign", _MODULE_PATH)
assert _SPEC and _SPEC.loader
campaign = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = campaign
_SPEC.loader.exec_module(campaign)


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _HttpClient:
    def __init__(self, response: _Response, posts: list[dict[str, Any]]) -> None:
        self.response = response
        self.posts = posts

    async def __aenter__(self) -> _HttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, *_: object, **kwargs: Any) -> _Response:
        self.posts.append(kwargs["json"])
        return self.response


class _Connection:
    async def close(self) -> None:
        return None


def _client(client_id: int) -> dict[str, Any]:
    return {
        "id": client_id,
        "email": "person.one@example.test",
        "full_name": "Synthetic Person",
    }


def _install_http(monkeypatch: pytest.MonkeyPatch, body: Any) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    response = _Response(200, body)
    monkeypatch.setattr(campaign.httpx, "AsyncClient", lambda: _HttpClient(response, posts))

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(campaign.asyncio, "sleep", no_sleep)
    return posts


@pytest.mark.asyncio
async def test_200_provider_refusal_is_an_error_not_a_sent_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts = _install_http(monkeypatch, {"success": False})

    async def readers(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        return [_client(900001)]

    monkeypatch.setitem(campaign.SEGMENT_FNS, "urgent", readers)

    result = await campaign.run_segment(_Connection(), "urgent", False, None, True)

    assert len(posts) == 1
    assert result["sent"] == 0
    assert result["errors"] == 1


@pytest.mark.asyncio
async def test_200_non_json_response_is_an_error_not_a_sent_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts = _install_http(monkeypatch, ValueError("not JSON"))

    async def readers(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        return [_client(900001)]

    monkeypatch.setitem(campaign.SEGMENT_FNS, "urgent", readers)

    result = await campaign.run_segment(_Connection(), "urgent", False, None, True)

    assert len(posts) == 1
    assert result["sent"] == 0
    assert result["errors"] == 1


@pytest.mark.asyncio
async def test_all_segments_posts_an_overlapping_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts = _install_http(monkeypatch, {"success": True})

    async def urgent(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        return [_client(900001)]

    async def empty(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        return []

    async def stale(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        return [_client(900001)]

    async def connect(_: str) -> _Connection:
        return _Connection()

    monkeypatch.setattr(campaign.asyncpg, "connect", connect)
    monkeypatch.setattr(
        campaign,
        "SEGMENT_FNS",
        {"urgent": urgent, "inactive": empty, "prospect": empty, "stale": stale},
    )

    await campaign.main(False, "all", None, True)

    assert len(posts) == 1


@pytest.mark.asyncio
async def test_zero_limit_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _install_http(monkeypatch, {"success": True})

    async def readers(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        return [_client(900001)]

    monkeypatch.setitem(campaign.SEGMENT_FNS, "urgent", readers)

    result = await campaign.run_segment(_Connection(), "urgent", False, 0, True)

    assert posts == []
    assert result == {"segment": 0, "sent": 0, "errors": 0}


@pytest.mark.asyncio
async def test_successful_response_counts_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _install_http(monkeypatch, {"success": True})

    async def readers(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        return [_client(900001)]

    monkeypatch.setitem(campaign.SEGMENT_FNS, "urgent", readers)

    result = await campaign.run_segment(_Connection(), "urgent", False, None, True)

    assert len(posts) == 1
    assert result["sent"] == 1
    assert result["errors"] == 0


@pytest.mark.asyncio
async def test_without_confirm_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _install_http(monkeypatch, {"success": True})

    async def readers(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        return [_client(900001)]

    monkeypatch.setitem(campaign.SEGMENT_FNS, "urgent", readers)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    await campaign.run_segment(_Connection(), "urgent", False, None, False)

    assert posts == []


@pytest.mark.asyncio
async def test_limit_two_posts_two_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _install_http(monkeypatch, {"success": True})

    async def readers(_: Any, *, limit: int | None) -> list[dict[str, Any]]:
        assert limit == 2
        return [_client(900001), _client(900002)]

    monkeypatch.setitem(campaign.SEGMENT_FNS, "urgent", readers)

    result = await campaign.run_segment(_Connection(), "urgent", False, 2, True)

    assert len(posts) == 2
    assert result["sent"] == 2
