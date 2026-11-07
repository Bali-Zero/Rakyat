"""Tests for the EventBus outbox-replay-on-reconnect hook (P0-2 phase 1).

The hook lives in ``EventBus._replay_outbox_on_reconnect``. It is called
from ``_connect_and_listen`` immediately after ``add_listener`` for all
PG channels and before the keep-alive loop. These tests verify the
contract without exercising real PG infrastructure (asyncpg connection,
pool, NOTIFY) — the underlying ``replay_unconsumed`` is already covered
by ``test_outbox.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.events.event_bus import EventBus, EventTrace


def _make_bus_with_pool(pool=None) -> EventBus:
    bus = EventBus(db_dsn="postgresql://test/x", db_pool=pool)
    return bus


@pytest.mark.asyncio
async def test_replay_skipped_when_no_db_pool(caplog):
    """If db_pool was not provided, replay logs and returns cleanly."""
    bus = _make_bus_with_pool(pool=None)
    with caplog.at_level("INFO"):
        await bus._replay_outbox_on_reconnect()
    assert any("outbox replay skipped" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_replay_calls_replay_unconsumed_per_channel():
    """For each PG channel, the hook calls replay_unconsumed with that channel."""
    pool = MagicMock()
    conn = AsyncMock()

    class _AcquireCtx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return None

    pool.acquire = MagicMock(return_value=_AcquireCtx())
    bus = _make_bus_with_pool(pool=pool)

    with patch(
        "backend.services.events.outbox.replay_unconsumed",
        new=AsyncMock(return_value=0),
    ) as mock_replay:
        await bus._replay_outbox_on_reconnect()

    # One call per known PG channel
    from backend.services.events.event_bus import PG_CHANNEL_MAP

    assert mock_replay.await_count == len(PG_CHANNEL_MAP)
    called_channels = {call.kwargs["channel"] for call in mock_replay.await_args_list}
    assert called_channels == set(PG_CHANNEL_MAP.keys())


@pytest.mark.asyncio
async def test_replay_continues_when_one_channel_fails(caplog):
    """A failing replay on one channel must not abort the rest."""
    pool = MagicMock()
    conn = AsyncMock()

    class _AcquireCtx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return None

    pool.acquire = MagicMock(return_value=_AcquireCtx())
    bus = _make_bus_with_pool(pool=pool)

    from backend.services.events.event_bus import PG_CHANNEL_MAP

    n_channels = len(PG_CHANNEL_MAP)

    # Make the first call raise, the rest succeed.
    side_effects: list = [RuntimeError("boom")] + [0] * (n_channels - 1)
    with patch(
        "backend.services.events.outbox.replay_unconsumed",
        new=AsyncMock(side_effect=side_effects),
    ) as mock_replay:
        with caplog.at_level("ERROR"):
            await bus._replay_outbox_on_reconnect()

    # All channels attempted (no early return on first failure)
    assert mock_replay.await_count == n_channels
    assert any("outbox replay failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_replay_dispatch_routes_through_handle_pg_event():
    """Replayed payloads go through the same path as real PG NOTIFY events."""
    pool = MagicMock()
    conn = AsyncMock()

    class _AcquireCtx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return None

    pool.acquire = MagicMock(return_value=_AcquireCtx())
    bus = _make_bus_with_pool(pool=pool)

    captured: list[tuple[str, str]] = []

    async def _spy(channel: str, payload: str) -> EventTrace:
        captured.append((channel, payload))
        return EventTrace(channel, 0, 1, 0)

    bus._handle_pg_event = _spy  # type: ignore[method-assign]

    async def _fake_replay(conn_arg, dispatch_fn, *, channel, **_kwargs):
        # Simulate one event per channel hitting dispatch
        await dispatch_fn({"_outbox_id": 1, "channel": channel, "x": 1})
        return 1

    with patch(
        "backend.services.events.outbox.replay_unconsumed",
        new=AsyncMock(side_effect=_fake_replay),
    ):
        await bus._replay_outbox_on_reconnect()

    from backend.services.events.event_bus import PG_CHANNEL_MAP

    # _handle_pg_event called once per channel, with the channel name.
    assert {ch for ch, _ in captured} == set(PG_CHANNEL_MAP.keys())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.TimeoutError])
async def test_reconnect_preserves_failed_delivery_and_retries_after_recovery(failure, monkeypatch):
    """Exercise real dispatch and replay with an idempotent, in-memory DB boundary."""
    monkeypatch.setattr("backend.services.events.event_bus._HANDLER_TIMEOUT_S", 0.01)
    consumed: set[int] = set()
    deliveries: list[int] = []
    successful_effects: set[int] = set()
    should_fail = True

    async def fetch(_sql, channel, _limit):
        if channel != "practice_changed":
            return []
        return [
            {"id": event_id, "channel": channel, "payload": {"practice_id": event_id}}
            for event_id in (101, 102)
            if event_id not in consumed
        ]

    async def execute(_sql, event_id, _consumer_id):
        if event_id in consumed:
            return "UPDATE 0"
        consumed.add(event_id)
        return "UPDATE 1"

    conn = AsyncMock()
    conn.fetch.side_effect = fetch
    conn.execute.side_effect = execute
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    bus = _make_bus_with_pool(pool)

    async def idempotent_handler(payload):
        successful_effects.add(payload["_outbox_id"])

    async def recovering_handler(payload):
        event_id = payload["_outbox_id"]
        deliveries.append(event_id)
        if should_fail and event_id == 101:
            if failure is asyncio.TimeoutError:
                await asyncio.sleep(1)
            raise failure("synthetic delivery failure")

    bus.subscribe("practice.status_changed", idempotent_handler)
    bus.subscribe("practice.status_changed", recovering_handler)

    await bus._replay_outbox_on_reconnect()

    assert consumed == {102}
    assert deliveries == [101, 102]
    assert successful_effects == {101, 102}
    assert len(bus._traces[0].errors) == 1

    should_fail = False
    await bus._replay_outbox_on_reconnect()

    assert consumed == {101, 102}
    assert deliveries == [101, 102, 101]
    assert successful_effects == {101, 102}
    assert not bus._traces[-1].errors

    await bus._replay_outbox_on_reconnect()
    assert deliveries == [101, 102, 101]
