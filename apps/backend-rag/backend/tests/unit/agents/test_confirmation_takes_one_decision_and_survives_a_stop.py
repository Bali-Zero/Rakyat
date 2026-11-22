"""Regression coverage for confirmation decision claiming and shutdown."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from backend.services.agents.confirmation_service import (
    CONFIRMATION_KEY_PREFIX,
    ConfirmationError,
    ConfirmationRedisDown,
    ConfirmationService,
    ConfirmationTimeout,
)
from backend.services.rag.agentic import tool_executor


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


class _FakeRedisManager:
    available = True

    def __init__(self) -> None:
        self.client = _FakeRedis()

    def get_async_client(self) -> _FakeRedis:
        return self.client


class _ConfirmAuthorizer:
    async def authorize(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            is_denied=False,
            needs_confirmation=True,
            reason="synthetic preview",
            args=kwargs["args"].copy(),
        )


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "executed"


class _FakeMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def record_tool_call(self, tool_name: str, status: str) -> None:
        self.calls.append((tool_name, status))


class _RaisingConfirmationService:
    def __init__(self, exception: ConfirmationError) -> None:
        self.exception = exception

    async def request_and_wait(self, **kwargs: Any) -> bool:
        del kwargs
        raise self.exception


async def _request_id_from_emitter(event: dict[str, Any]) -> str:
    return event["data"]["request_id"]


@pytest.mark.asyncio
async def test_contradictory_decision_is_not_accepted_and_publishes_once() -> None:
    """GUILT: the second, contradictory decision must lose."""
    manager = _FakeRedisManager()
    service = ConfirmationService(manager)
    emitted: list[dict[str, Any]] = []

    async def emitter(event: dict[str, Any]) -> None:
        emitted.append(event)

    waiting = asyncio.create_task(
        service.request_and_wait(
            tool_name="synthetic_write",
            args={"record": "synthetic"},
            user_email="person.one@example.test",
            preview="synthetic preview",
            emitter=emitter,
        )
    )
    while not emitted:
        await asyncio.sleep(0)
    request_id = await _request_id_from_emitter(emitted[0])

    assert (
        await service.resolve_confirmation(request_id, "approve", "person.one@example.test") is True
    )
    assert (
        await service.resolve_confirmation(request_id, "reject", "person.one@example.test") is False
    )
    assert await waiting is True
    assert len(manager.client.published) == 1


@pytest.mark.asyncio
async def test_same_decision_replays_and_cleanup_preserves_ownership_rules() -> None:
    """INNOCENCE: the same decision again is accepted and delivered again."""
    manager = _FakeRedisManager()
    service = ConfirmationService(manager)
    emitted: list[dict[str, Any]] = []

    async def emitter(event: dict[str, Any]) -> None:
        emitted.append(event)

    waiting = asyncio.create_task(
        service.request_and_wait(
            tool_name="synthetic_write",
            args={"record": "synthetic"},
            user_email="person.one@example.test",
            preview="synthetic preview",
            emitter=emitter,
        )
    )
    while not emitted:
        await asyncio.sleep(0)
    request_id = await _request_id_from_emitter(emitted[0])

    assert (
        await service.resolve_confirmation(request_id, "approve", "person.two@example.test")
        is False
    )
    assert (
        await service.resolve_confirmation("unknown-request", "approve", "person.one@example.test")
        is False
    )
    assert (
        await service.resolve_confirmation(request_id, "approve", "person.one@example.test") is True
    )
    assert (
        await service.resolve_confirmation(request_id, "approve", "person.one@example.test") is True
    )
    assert await waiting is True
    # Delivered twice on purpose: a lost first delivery is cured by the retry.
    assert len(manager.client.published) == 2
    assert f"{CONFIRMATION_KEY_PREFIX}{request_id}" not in manager.client.values
    assert f"conf:decision:{request_id}" not in manager.client.values


@pytest.mark.asyncio
async def test_executor_denies_confirmation_interrupted_by_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GUILT: stopping with a pending confirmation cannot escape the executor."""
    manager = _FakeRedisManager()
    service = ConfirmationService(manager)
    metrics = _FakeMetrics()
    tool = _FakeTool()
    monkeypatch.setattr(tool_executor, "_authorizer", _ConfirmAuthorizer())
    monkeypatch.setattr(tool_executor, "_confirmation_service", service)
    monkeypatch.setattr(tool_executor, "metrics_collector", metrics)

    async def stop_when_requested(event: dict[str, Any]) -> None:
        await _request_id_from_emitter(event)
        await service.stop()

    result, _ = await tool_executor.execute_tool(
        tool_map={"synthetic_write": tool},
        tool_name="synthetic_write",
        arguments={"record": "synthetic"},
        user_id="person.one@example.test",
        agent_role=object(),
        confirmation_emitter=stop_when_requested,
    )

    assert "confirmation was interrupted for 'synthetic_write'" in result
    assert tool.calls == []
    assert metrics.calls == [("synthetic_write", "denied")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "expected_status", "expected_text"),
    [
        (ConfirmationTimeout("synthetic timeout"), "timeout", "did not confirm"),
        (ConfirmationRedisDown("synthetic redis down"), "denied", "Redis down"),
    ],
)
async def test_executor_keeps_timeout_and_redis_down_denials(
    monkeypatch: pytest.MonkeyPatch,
    exception: ConfirmationError,
    expected_status: str,
    expected_text: str,
) -> None:
    """INNOCENCE: existing confirmation failure paths retain their results."""
    metrics = _FakeMetrics()
    tool = _FakeTool()
    monkeypatch.setattr(tool_executor, "_authorizer", _ConfirmAuthorizer())
    monkeypatch.setattr(
        tool_executor, "_confirmation_service", _RaisingConfirmationService(exception)
    )
    monkeypatch.setattr(tool_executor, "metrics_collector", metrics)

    result, _ = await tool_executor.execute_tool(
        tool_map={"synthetic_write": tool},
        tool_name="synthetic_write",
        arguments={"record": "synthetic"},
        user_id="person.one@example.test",
        agent_role=object(),
    )

    assert expected_text in result
    assert tool.calls == []
    assert metrics.calls == [("synthetic_write", expected_status)]
