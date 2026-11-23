"""
POST /api/whatsapp/send: recipient check without a database, and rate-limit slot.

- With no database handle the "known contact" check cannot run, so a recipient
  outside WHATSAPP_TEAM_ALLOWLIST is refused with 503 and nothing is sent. An
  allow-listed recipient is still sent.
- The rate-limit slot is recorded before the provider call is awaited, so a
  concurrent burst cannot all pass the check together; a slot is not given
  back when the provider call fails.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from backend.app.dependencies import get_current_user, get_optional_database_pool
from backend.app.routers import whatsapp_conversations
from backend.app.routers.whatsapp_conversations import _outbound_rate, router

ALLOWED_PHONE = "+6280000000001"
OUTSIDE_PHONE = "+6280000000002"
LIMIT = 3


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    _outbound_rate.clear()
    yield
    _outbound_rate.clear()


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setattr(
        whatsapp_conversations.settings, "whatsapp_team_allowlist", "6280000000001", raising=False
    )
    monkeypatch.setattr(whatsapp_conversations, "_RATE_LIMIT_MAX", LIMIT)


def _app(db_pool) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {
        "email": "synthetic@example.test",
        "role": "admin",
    }
    app.dependency_overrides[get_optional_database_pool] = lambda: db_pool
    return app


def _client(db_pool) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(db_pool)), base_url="http://test"
    )


async def _yielding_send(*_args, **_kwargs):
    for _ in range(20):
        await asyncio.sleep(0)
    return {"messages": [{"id": "wamid.synthetic"}]}


@pytest.mark.asyncio
async def test_no_database_and_recipient_outside_allowlist_is_refused():
    with patch.object(
        whatsapp_conversations.whatsapp_service, "send_message", new=AsyncMock()
    ) as mock_send:
        async with _client(None) as client:
            response = await client.post(
                "/api/whatsapp/send", json={"phone": OUTSIDE_PHONE, "message": "hello"}
            )

    assert response.status_code == 503
    mock_send.assert_not_awaited()
    assert not any(_outbound_rate.values())


@pytest.mark.asyncio
async def test_no_database_and_allowlisted_recipient_is_still_sent():
    with patch.object(
        whatsapp_conversations.whatsapp_service,
        "send_message",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.synthetic"}]}),
    ) as mock_send:
        async with _client(None) as client:
            response = await client.post(
                "/api/whatsapp/send", json={"phone": ALLOWED_PHONE, "message": "hello"}
            )

    assert response.status_code == 200
    mock_send.assert_awaited_once_with(phone="6280000000001", text="hello")


@pytest.mark.asyncio
async def test_with_database_unknown_recipient_is_still_403(mock_db_pool, mock_db_conn):
    mock_db_conn.fetchval = AsyncMock(return_value=False)
    with patch.object(
        whatsapp_conversations.whatsapp_service, "send_message", new=AsyncMock()
    ) as mock_send:
        async with _client(mock_db_pool) as client:
            response = await client.post(
                "/api/whatsapp/send", json={"phone": OUTSIDE_PHONE, "message": "hello"}
            )

    assert response.status_code == 403
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_burst_sends_exactly_the_limit_without_database():
    with patch.object(
        whatsapp_conversations.whatsapp_service,
        "send_message",
        new=AsyncMock(side_effect=_yielding_send),
    ) as mock_send:
        async with _client(None) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/whatsapp/send", json={"phone": ALLOWED_PHONE, "message": "hello"}
                    )
                    for _ in range(LIMIT + 1)
                ]
            )

    assert sorted(r.status_code for r in responses) == [200] * LIMIT + [429]
    assert mock_send.await_count == LIMIT


@pytest.mark.asyncio
async def test_concurrent_burst_sends_exactly_the_limit_with_database(mock_db_pool, mock_db_conn):
    # The lookup yields long enough for every request of the burst to have passed the
    # rate check at the top of the route, so only the check made right before the
    # reservation can hold the burst to the limit.
    async def _yielding_lookup(*_args, **_kwargs):
        for _ in range(20):
            await asyncio.sleep(0)
        return True

    mock_db_conn.fetchval = AsyncMock(side_effect=_yielding_lookup)
    with patch.object(
        whatsapp_conversations.whatsapp_service,
        "send_message",
        new=AsyncMock(side_effect=_yielding_send),
    ) as mock_send:
        async with _client(mock_db_pool) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/whatsapp/send", json={"phone": OUTSIDE_PHONE, "message": "hello"}
                    )
                    for _ in range(LIMIT + 1)
                ]
            )

    assert sorted(r.status_code for r in responses) == [200] * LIMIT + [429]
    assert mock_send.await_count == LIMIT


@pytest.mark.asyncio
async def test_sequential_caller_under_the_limit_is_unchanged():
    with patch.object(
        whatsapp_conversations.whatsapp_service,
        "send_message",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.synthetic"}]}),
    ) as mock_send:
        async with _client(None) as client:
            for _ in range(LIMIT):
                response = await client.post(
                    "/api/whatsapp/send", json={"phone": ALLOWED_PHONE, "message": "hello"}
                )
                assert response.status_code == 200
            over = await client.post(
                "/api/whatsapp/send", json={"phone": ALLOWED_PHONE, "message": "hello"}
            )

    assert over.status_code == 429
    assert mock_send.await_count == LIMIT
    assert len(_outbound_rate["6280000000001"]) == LIMIT


@pytest.mark.asyncio
async def test_slot_is_not_given_back_when_the_provider_call_fails():
    with patch.object(
        whatsapp_conversations.whatsapp_service,
        "send_message",
        new=AsyncMock(side_effect=ValueError("provider refused")),
    ):
        async with _client(None) as client:
            response = await client.post(
                "/api/whatsapp/send", json={"phone": ALLOWED_PHONE, "message": "hello"}
            )

    assert response.status_code == 502
    assert len(_outbound_rate["6280000000001"]) == 1
