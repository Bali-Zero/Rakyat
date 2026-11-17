"""run_forever starts CRM KG maintenance as a background task, never inline."""

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
    """Counts run_once calls and stops the loop after `stop_after` of them."""

    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.run_once_calls = 0
        self.kg_calls = 0
        self.kg_cancelled = 0
        self.kg_release = asyncio.Event()
        self.stop_requested = asyncio.Event()

    async def run_once(self, _config: Any) -> dict[str, Any]:
        self.run_once_calls += 1
        if self.run_once_calls >= self.stop_after:
            self.stop_requested.set()
        await asyncio.sleep(0)
        return {}


def install(harness: Harness, kg: Any):
    return (
        patch.object(worker, "run_once", new=harness.run_once),
        patch.object(worker, "run_kg_maintenance", new=kg),
    )


async def drive(config: worker.DrivePollWorkerConfig, harness: Harness, kg: Any) -> int:
    p1, p2 = install(harness, kg)
    # interval 0 makes wait_for time out immediately, so the loop spins per iteration;
    # a stop request flips the real stop_event through the SIGTERM handler.
    loop = asyncio.get_running_loop()
    handlers: dict[Any, Any] = {}
    with (
        p1,
        p2,
        patch.object(loop, "add_signal_handler", new=lambda sig, cb: handlers.setdefault(sig, cb)),
    ):
        task = asyncio.create_task(worker.run_forever(config))

        async def stopper() -> None:
            await harness.stop_requested.wait()
            for cb in handlers.values():
                cb()
                break

        stop_task = asyncio.create_task(stopper())
        try:
            return await asyncio.wait_for(task, timeout=1)
        finally:
            stop_task.cancel()


@pytest.mark.asyncio
async def test_loop_keeps_polling_while_maintenance_never_completes() -> None:
    harness = Harness(stop_after=5)

    async def never(_url: str, _config: Any) -> dict[str, Any]:
        harness.kg_calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            harness.kg_cancelled += 1
            raise
        return {}

    assert await drive(make_config(), harness, never) == 0
    assert harness.run_once_calls >= 5
    # one task started, later iterations skipped, shutdown cancelled the pending one
    assert harness.kg_calls == 1
    assert harness.kg_cancelled == 1


@pytest.mark.asyncio
async def test_new_task_starts_after_previous_completes() -> None:
    harness = Harness(stop_after=4)

    async def quick(_url: str, _config: Any) -> dict[str, Any]:
        harness.kg_calls += 1
        return {"status": "ran"}

    assert await drive(make_config(), harness, quick) == 0
    assert harness.kg_calls >= 2


@pytest.mark.asyncio
async def test_failed_maintenance_task_is_reaped_and_loop_continues() -> None:
    harness = Harness(stop_after=4)

    async def boom(_url: str, _config: Any) -> dict[str, Any]:
        harness.kg_calls += 1
        raise RuntimeError("kg boom")

    assert await drive(make_config(), harness, boom) == 0
    assert harness.kg_calls >= 2


@pytest.mark.asyncio
async def test_once_never_runs_maintenance() -> None:
    harness = Harness(stop_after=99)
    calls: list[str] = []

    async def kg(_url: str, _config: Any) -> dict[str, Any]:
        calls.append("kg")
        return {}

    p1, p2 = install(harness, kg)
    with p1, p2:
        assert await worker.run_forever(make_config(once=True)) == 0
    assert harness.run_once_calls == 1
    assert calls == []
