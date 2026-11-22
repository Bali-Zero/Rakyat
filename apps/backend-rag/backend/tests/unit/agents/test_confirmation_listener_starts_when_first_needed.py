"""Regression coverage for lazy confirmation pub/sub listener startup."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from backend.services.agents.confirmation_service import (
    CONFIRMATION_PUBSUB_CHANNEL,
    ConfirmationRedisDown,
    ConfirmationService,
)


class _FakeRedisManager:
    def __init__(self, *, available: bool = True) -> None:
        self._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    def get_async_client(self) -> Any | None:
        return self._client if self._available else None


async def _request_id(emitter: AsyncMock) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.0
    while emitter.call_args is None:
        if loop.time() > deadline:
            raise AssertionError("confirmation emitter was not called")
        await asyncio.sleep(0.01)
    return emitter.call_args.args[0]["data"]["request_id"]


@pytest.mark.asyncio
async def test_request_starts_listener_after_redis_recovers_and_receives_publish() -> None:
    """GUILT: a listener missed at boot must start before the first wait."""
    manager = _FakeRedisManager(available=False)
    service = ConfirmationService(manager)
    await service.start()
    assert service._listener_task is None

    manager._available = True
    emitter = AsyncMock()

    async def publish_from_another_process() -> None:
        request_id = await _request_id(emitter)
        await manager.get_async_client().publish(
            CONFIRMATION_PUBSUB_CHANNEL,
            json.dumps({"request_id": request_id, "decision": "approve"}),
        )

    publisher = asyncio.create_task(publish_from_another_process())
    assert (
        await service.request_and_wait(
            tool_name="synthetic_write",
            args={"record": "synthetic"},
            user_email="person.one@example.test",
            preview="synthetic preview",
            emitter=emitter,
            timeout=1.0,
        )
        is True
    )
    await publisher
    assert service._listener_task is not None
    await service.stop()


@pytest.mark.asyncio
async def test_request_restarts_listener_after_previous_listener_ended() -> None:
    """GUILT: the next wait replaces an ended listener task."""
    manager = _FakeRedisManager()
    service = ConfirmationService(manager)
    await service.start()
    ended_listener = service._listener_task
    assert ended_listener is not None
    ended_listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ended_listener

    emitter = AsyncMock()

    async def resolve_locally() -> None:
        request_id = await _request_id(emitter)
        assert (
            await service.resolve_confirmation(request_id, "approve", "person.one@example.test")
            is True
        )

    resolver = asyncio.create_task(resolve_locally())
    assert (
        await service.request_and_wait(
            tool_name="synthetic_write",
            args={"record": "synthetic"},
            user_email="person.one@example.test",
            preview="synthetic preview",
            emitter=emitter,
            timeout=1.0,
        )
        is True
    )
    await resolver
    assert service._listener_task is not ended_listener
    await service.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent_and_redis_down_remains_fail_closed() -> None:
    """Innocence: an active listener is reused and unavailable Redis still fails closed."""
    manager = _FakeRedisManager()
    service = ConfirmationService(manager)
    await service.start()
    listener = service._listener_task
    await service.start()
    assert service._listener_task is listener

    emitter = AsyncMock()

    async def resolve_locally() -> None:
        request_id = await _request_id(emitter)
        assert (
            await service.resolve_confirmation(request_id, "approve", "person.one@example.test")
            is True
        )

    resolver = asyncio.create_task(resolve_locally())
    assert (
        await service.request_and_wait(
            tool_name="synthetic_write",
            args={"record": "synthetic"},
            user_email="person.one@example.test",
            preview="synthetic preview",
            emitter=emitter,
            timeout=1.0,
        )
        is True
    )
    await resolver
    await service.stop()

    manager._available = False
    with pytest.raises(ConfirmationRedisDown):
        await service.request_and_wait(
            tool_name="synthetic_write",
            args={"record": "synthetic"},
            user_email="person.one@example.test",
            preview="synthetic preview",
            timeout=0.01,
        )
