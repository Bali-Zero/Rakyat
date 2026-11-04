"""`lifespan_light` shutdown must end a light init that is still retrying its pool.

`initialize_services_light` retries pool creation for as long as the process
lives (2026-10-21 outage: one failed create at boot left the api machine dead).
That loop lives inside `app.state._init_task`; a shutdown that does not cancel
it would leave the task pending past the lifespan, and a retry that succeeded
during shutdown would open a pool nobody closes.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.app import main_api


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


@pytest.mark.asyncio
async def test_lifespan_light_shutdown_cancels_a_still_retrying_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def retrying_forever(_app: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setitem(
        sys.modules,
        "backend.app.setup.service_initializer",
        _module(
            "backend.app.setup.service_initializer",
            initialize_services_light=retrying_forever,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.app.rag_proxy",
        _module("backend.app.rag_proxy", close_proxy_client=AsyncMock()),
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.integrations.wa_inbox_bot",
        _module("backend.services.integrations.wa_inbox_bot", close_rag_client=AsyncMock()),
    )

    async with main_api.lifespan_light(app):
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not app.state._init_task.done()

    assert cancelled.is_set()
    assert app.state._init_task.cancelled()
    assert getattr(app.state, "db_pool", None) is None


@pytest.mark.asyncio
async def test_lifespan_light_shutdown_leaves_a_finished_init_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    db_pool = SimpleNamespace(close=AsyncMock())

    async def init_light(target: Any) -> None:
        target.state.db_pool = db_pool

    monkeypatch.setenv("DISABLE_BACKGROUND_WORKERS", "1")
    monkeypatch.setitem(
        sys.modules,
        "backend.app.setup.service_initializer",
        _module("backend.app.setup.service_initializer", initialize_services_light=init_light),
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.app.rag_proxy",
        _module("backend.app.rag_proxy", close_proxy_client=AsyncMock()),
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.integrations.wa_inbox_bot",
        _module("backend.services.integrations.wa_inbox_bot", close_rag_client=AsyncMock()),
    )

    async with main_api.lifespan_light(app):
        await app.state._init_task

    assert app.state._init_task.done()
    assert not app.state._init_task.cancelled()
    db_pool.close.assert_awaited_once()
