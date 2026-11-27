"""The first-proof sweep is spawned on the api process, behind its kill switch and the db pool."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.app import main_api
from backend.services.crm import delivery_proof_sweep as sweep

pytestmark = pytest.mark.asyncio


def _app(pool=object()):
    return SimpleNamespace(state=SimpleNamespace(db_pool=pool))


async def test_first_proof_sweep_spawn_runs_the_scheduler_on_the_pool(monkeypatch):
    monkeypatch.delenv("FIRST_PROOF_SWEEP_ENABLED", raising=False)
    seen = []

    async def fake(pool):
        seen.append(pool)
        await asyncio.sleep(3600)

    monkeypatch.setattr(sweep, "run_first_proof_sweep_scheduler", fake)
    app = _app(pool="pool")
    task = main_api._spawn_first_proof_sweep_task(app)
    assert task is not None
    await asyncio.sleep(0)
    assert seen == ["pool"]
    task.cancel()


@pytest.mark.parametrize("value", ["false", "0", " FALSE "])
async def test_first_proof_sweep_spawn_honours_the_kill_switch(monkeypatch, value):
    monkeypatch.setenv("FIRST_PROOF_SWEEP_ENABLED", value)
    assert main_api._spawn_first_proof_sweep_task(_app()) is None


async def test_first_proof_sweep_spawn_needs_a_pool(monkeypatch):
    monkeypatch.delenv("FIRST_PROOF_SWEEP_ENABLED", raising=False)
    assert main_api._spawn_first_proof_sweep_task(_app(pool=None)) is None


async def test_the_lifespan_spawns_and_cancels_the_sweep():
    source = open(main_api.__file__).read()
    assert "app.state._first_proof_sweep_task = _spawn_first_proof_sweep_task(app)" in source
    assert 'getattr(app.state, "_first_proof_sweep_task", None)' in source
