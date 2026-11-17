"""run_forever starts the CRM KG maintenance loop once, in its own task."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from backend.workers import drive_poll_worker as worker


def make_config(*, once: bool = False) -> worker.DrivePollWorkerConfig:
    return worker.DrivePollWorkerConfig(
        database_url="postgresql://x/db",
        interval_seconds=0,
        jitter_seconds=0,
        timeout_seconds=60,
        inline_ocr=False,
        once=once,
    )


class Harness:
    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.run_once_calls = 0
        self.loop_starts = 0
        self.loop_cancelled = 0
        self.stop_seen_by_loop: asyncio.Event | None = None
        self.stop_requested = asyncio.Event()

    async def run_once(self, _config: Any) -> dict[str, Any]:
        self.run_once_calls += 1
        if self.run_once_calls >= self.stop_after:
            self.stop_requested.set()
        await asyncio.sleep(0)
        return {}


async def drive(config: worker.DrivePollWorkerConfig, harness: Harness, loop_fn: Any) -> int:
    loop = asyncio.get_running_loop()
    handlers: list[Any] = []
    with (
        patch.object(worker, "run_once", new=harness.run_once),
        patch.object(worker, "run_maintenance_loop", new=loop_fn),
        patch.object(loop, "add_signal_handler", new=lambda _sig, cb: handlers.append(cb)),
    ):
        task = asyncio.create_task(worker.run_forever(config))

        async def stopper() -> None:
            await harness.stop_requested.wait()
            handlers[0]()

        stop_task = asyncio.create_task(stopper())
        try:
            return await asyncio.wait_for(task, timeout=1)
        finally:
            stop_task.cancel()


@pytest.mark.asyncio
async def test_loop_started_once_and_polling_continues_while_it_hangs() -> None:
    harness = Harness(stop_after=5)

    async def hang(_url: str, _config: Any, stop_event: asyncio.Event) -> None:
        harness.loop_starts += 1
        harness.stop_seen_by_loop = stop_event
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            harness.loop_cancelled += 1
            raise

    assert await drive(make_config(), harness, hang) == 0
    assert harness.run_once_calls >= 5
    assert harness.loop_starts == 1
    # stop cancelled the pending maintenance task and the worker returned
    assert harness.loop_cancelled == 1
    assert harness.stop_seen_by_loop is not None
    assert harness.stop_seen_by_loop.is_set()


@pytest.mark.asyncio
async def test_unexpected_maintenance_exception_is_logged_and_polling_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness(stop_after=3)

    async def boom(_url: str, _config: Any, _stop: asyncio.Event) -> None:
        raise RuntimeError("kg boom")

    with caplog.at_level("ERROR"):
        assert await drive(make_config(), harness, boom) == 0
    assert harness.run_once_calls >= 3
    assert any("ended unexpectedly" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_once_never_starts_maintenance() -> None:
    harness = Harness(stop_after=99)
    started: list[str] = []

    async def loop_fn(_url: str, _config: Any, _stop: asyncio.Event) -> None:
        started.append("kg")

    with (
        patch.object(worker, "run_once", new=harness.run_once),
        patch.object(worker, "run_maintenance_loop", new=loop_fn),
    ):
        assert await worker.run_forever(make_config(once=True)) == 0
    assert harness.run_once_calls == 1
    assert started == []
