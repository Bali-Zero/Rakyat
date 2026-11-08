from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.misc import autonomous_scheduler as module
from backend.services.misc.autonomous_scheduler import (
    AutonomousScheduler,
    get_autonomous_scheduler,
)


async def _noop_task() -> None:
    return None


def test_get_autonomous_scheduler_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_scheduler", None)

    first = get_autonomous_scheduler()
    second = get_autonomous_scheduler()

    assert isinstance(first, AutonomousScheduler)
    assert first is second


def test_register_task_status_and_enable_disable() -> None:
    scheduler = AutonomousScheduler()
    scheduler.register_task("health", _noop_task, interval_seconds=300, enabled=False)

    status = scheduler.get_status()

    assert status["running"] is False
    assert status["task_count"] == 1
    assert status["tasks"]["health"]["enabled"] is False
    assert status["tasks"]["health"]["interval_seconds"] == 300
    assert scheduler.enable_task("health") is True
    assert scheduler.tasks["health"].enabled is True
    assert scheduler.disable_task("health") is True
    assert scheduler.tasks["health"].enabled is False
    assert scheduler.enable_task("missing") is False
    assert scheduler.disable_task("missing") is False


@pytest.mark.asyncio
async def test_start_and_stop_manage_enabled_task_without_running_body() -> None:
    scheduler = AutonomousScheduler()
    scheduler.register_task("health", _noop_task, interval_seconds=300, enabled=True)

    await scheduler.start()
    assert scheduler.get_status()["running"] is True
    assert scheduler.tasks["health"]._task is not None
    assert scheduler.tasks["health"]._task.done() is False

    await scheduler.stop()
    assert scheduler.get_status()["running"] is False


@pytest.mark.asyncio
async def test_create_and_start_scheduler_uses_global_scheduler_and_starts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeScheduler:
        def __init__(self) -> None:
            self.registered: list[dict] = []
            self.started = False

        def register_task(self, **kwargs) -> None:
            self.registered.append(kwargs)

        async def start(self) -> None:
            self.started = True

    birthday = AsyncMock(return_value={"sent": 0})
    drive = AsyncMock(return_value={"processed": 0})
    for name, attribute, callback in (
        ("backend.services.crm.birthday_notifier_service", "run_birthday_notifier_task", birthday),
        ("backend.services.crm.drive_poll_service", "poll_drive_changes", drive),
    ):
        stub = ModuleType(name)
        setattr(stub, attribute, callback)
        monkeypatch.setitem(sys.modules, name, stub)
    fake_scheduler = FakeScheduler()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ENABLE_KG_INCREMENTAL", "false")
    monkeypatch.setattr(module, "get_autonomous_scheduler", lambda: fake_scheduler)

    pool = object()
    result = await module.create_and_start_scheduler(db_pool=pool)

    assert result is fake_scheduler
    assert fake_scheduler.started is True
    assert all(task["name"] != "conversation_cleanup" for task in fake_scheduler.registered)
    assert [
        (t["name"], t["interval_seconds"], t["enabled"]) for t in fake_scheduler.registered
    ] == [
        ("birthday_notifier", 86400, False),
        ("drive_changes_poll", 300, False),
    ]
    for task in fake_scheduler.registered:
        await task["task_func"]()
    birthday.assert_awaited_once_with(pool)
    drive.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_retired_cleanup_switch_is_refused_before_starting_any_task(monkeypatch):
    scheduler = MagicMock()
    scheduler.start = AsyncMock()
    monkeypatch.setattr(module, "get_autonomous_scheduler", lambda: scheduler)
    # This was the only activation interface of the dormant duplicate.
    # Rejecting it prevents a stale caller from reintroducing that schedule.
    with pytest.raises(TypeError, match="conversation_cleanup_enabled"):
        await module.create_and_start_scheduler(db_pool=object(), conversation_cleanup_enabled=True)
    scheduler.register_task.assert_not_called()
    scheduler.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_retained_manual_cleanup_route_auth_policy_and_recovery(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from backend.app.routers import admin_conversation_cleanup as cleanup
    from backend.app.setup.router_manifest import routers_for_group
    from backend.core.retention_policy import ALLOW_CLOCK_DELETE_ENV

    # The production manifest still exposes the real manual owner to API.
    assert "admin_conversation_cleanup" in {entry.name for entry in routers_for_group("api")}
    conn = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = transaction
    identities = [
        {"id": 1, "session_id": "cleanup-fixture-delete"},
        {"id": 2, "session_id": "cleanup-fixture-anonymize"},
    ]

    async def fetchval(query, *args):
        if "hashtext" in query:
            return {"cleanup-fixture-delete": 11, "cleanup-fixture-anonymize": 22}[args[0]]
        return 0

    async def execute(query, *args):
        normalized = " ".join(query.split())
        if normalized.startswith("DELETE FROM conversations"):
            return "DELETE 1"
        if normalized.startswith("UPDATE conversations"):
            return "UPDATE 1"
        assert normalized.startswith(
            ("SELECT set_config(", "SET LOCAL ", "SELECT pg_advisory_xact_lock(")
        )
        return "SELECT 1"

    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetch = AsyncMock(side_effect=[identities, [{"id": 1}, {"id": 2}]])
    conn.execute = AsyncMock(side_effect=execute)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(cleanup.settings, "admin_api_key", "synthetic-admin-only")
    monkeypatch.delenv(ALLOW_CLOCK_DELETE_ENV, raising=False)
    app = FastAPI()
    app.include_router(cleanup.router)
    app.dependency_overrides[cleanup.get_database_pool] = lambda: pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid"
    ) as client:
        path = "/api/admin/conversation-cleanup"
        denied = await client.post(path)
        assert denied.status_code == 401
        pool.acquire.assert_not_called()
        headers = {"x-debug-key": "synthetic-admin-only"}
        dry = await client.post(path, headers=headers)
        assert dry.status_code == 200
        assert dry.json()["status"] == "dry_run"
        assert conn.fetchval.await_count == 2
        conn.execute.assert_not_awaited()
        pool.acquire.reset_mock()
        refused = await client.post(path, params={"dry_run": "false"}, headers=headers)
        assert refused.status_code == 409
        pool.acquire.assert_not_called()
        monkeypatch.setenv(ALLOW_CLOCK_DELETE_ENV, "1")
        recovered = await client.post(path, params={"dry_run": "false"}, headers=headers)
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "ok"
        transaction.__aenter__.assert_awaited_once()
        transaction.__aexit__.assert_awaited_once_with(None, None, None)
        pool.acquire.return_value.__aenter__.assert_awaited_once()
        pool.acquire.return_value.__aexit__.assert_awaited_once_with(None, None, None)
        assert conn.fetch.await_count == 2
        assert "FOR UPDATE" in conn.fetch.await_args_list[1].args[0]
        assert conn.fetch.await_args_list[1].args[2] == [1, 2]
        calls = [
            (" ".join(call.args[0].split()), call.args[1:]) for call in conn.execute.await_args_list
        ]
        assert len(calls) == 5
        assert sum(query.startswith("DELETE FROM conversations") for query, _ in calls) == 1
        assert sum(query.startswith("UPDATE conversations") for query, _ in calls) == 1
        assert calls[0] == ("SELECT set_config('statement_timeout', $1, true)", ("5.0s",))
        assert calls[1] == ("SET LOCAL lock_timeout = '5s'", ())
        # One exclusive coordination barrier, regardless of how many identities match.
        assert [args for query, args in calls if query.startswith("SELECT pg_advisory_xact_lock(")] == [
            (0x43564E43,)
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_refused", [False, True])
async def test_retained_manual_cli_keeps_floor_reports_refusal_and_closes_pool(
    monkeypatch, policy_refused
):
    from backend.core.retention_policy import RETENTION_MIN_DAYS, RetentionPolicyViolation
    from backend.jobs import conversation_cleanup as job

    pool = MagicMock()
    pool.close = AsyncMock()
    get_pool = AsyncMock(return_value=pool)
    repo = MagicMock()
    repo.anonymize_user_data = AsyncMock(return_value=1)
    repo.cleanup_old_conversations = AsyncMock(return_value=2)
    if policy_refused:
        repo.cleanup_old_conversations.side_effect = RetentionPolicyViolation(
            "synthetic policy refusal"
        )
    constructor = MagicMock(return_value=repo)
    monkeypatch.setattr(job, "get_db_pool", get_pool)
    monkeypatch.setattr(job, "ConversationRepository", constructor)
    result = await job.main()
    assert result["success"] is not policy_refused
    constructor.assert_called_once_with(pool)
    repo.anonymize_user_data.assert_awaited_once_with(days=RETENTION_MIN_DAYS)
    repo.cleanup_old_conversations.assert_awaited_once_with(days=RETENTION_MIN_DAYS)
    if not policy_refused:
        assert result["deleted_count"] == 2
        assert result["anonymized_count"] == 1
    pool.close.assert_awaited_once_with()
