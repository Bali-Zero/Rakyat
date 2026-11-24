"""Exercise the real initializer→factory→PostgreSQL path with synthetic services."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Sequence
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import asyncpg
import pytest

from backend.app.core import database
from backend.app.setup import service_initializer as initializer
from backend.tests.unit.app.setup.test_service_initializer_lifecycle import (
    _configure_full_initializer,
    _module,
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not TEST_DSN, reason="isolated PG required"),
]


_WATCHED_CONNECTIONS = (
    "FROM pg_stat_activity WHERE datname = current_database() AND pid = ANY($1::int[])"
)


async def _wait_for_connections_to_close(
    observer: asyncpg.Connection, server_pids: Sequence[int], budget: float
) -> None:
    """Wait until none of the given server processes is connected to the database.

    The caller names the connections it saw opened. The database of a test
    worker is shared with the tests that ran before on it, and a pool one of
    them left can still be closing: those connections are not this probe's to
    judge, and counting them made it fail on changes that touch no pool.

    When the budget runs out the error names what was still connected, so a
    red run says who held a connection and not only that one was held.
    """
    watched = list(server_pids)
    try:
        async with asyncio.timeout(budget):
            while await observer.fetchval(f"SELECT count(*) {_WATCHED_CONNECTIONS}", watched):
                await asyncio.sleep(0.02)
    except TimeoutError as exc:
        try:
            rows = await observer.fetch(
                "SELECT pid, datname, application_name, state, backend_start, "
                "left(split_part(query, chr(10), 1), 120) AS query_head "
                f"{_WATCHED_CONNECTIONS} ORDER BY backend_start",
                watched,
            )
            held = "; ".join(
                f"pid={row['pid']} datname={row['datname']} "
                f"application_name={row['application_name']!r} state={row['state']} "
                f"backend_start={row['backend_start']} query={row['query_head']!r}"
                for row in rows
            )
            detail = f"{len(rows)} still connected: {held}" if rows else "none left when read"
        except Exception as read_error:  # the diagnosis must never hide the timeout
            detail = f"could not be listed ({type(read_error).__name__})"
        raise TimeoutError(
            f"watched connections on the database after {budget} s; {detail}"
        ) from exc


@pytest.fixture
def isolated_dsn() -> str:
    parsed = urlparse(TEST_DSN)
    host = parse_qs(parsed.query).get("host", [parsed.hostname or ""])[0]
    name = parsed.path.removeprefix("/")
    assert host.startswith("/") or host in {"localhost", "127.0.0.1", "::1"}
    assert (
        name in {"test", "test_db", "nuzantara_test"}
        or name.startswith("udb_")
        or re.fullmatch(r"(?:test|test_db|nuzantara_test)_gw[0-9]+", name)
    )
    return TEST_DSN


@pytest.mark.parametrize(
    ("host", "name", "allowed"),
    [
        ("localhost", "test", True),
        ("localhost", "test_db", True),
        ("localhost", "nuzantara_test", True),
        ("localhost", "udb_w1_synthetic", True),
        ("localhost", "nuzantara_test_gw0", True),
        ("localhost", "nuzantara_test_gw12", True),
        ("localhost", "test_gw1", True),
        ("localhost", "test_db_gw2", True),
        ("localhost", "nuzantara_rag", False),
        ("localhost", "nuzantara_test_gw", False),
        ("localhost", "nuzantara_test_gw0_prod", False),
        ("localhost", "nuzantara_test_gw0_gw1", False),
        ("localhost", "nuzantara_test_gw\u0661", False),
        ("localhost", "unrelated_gw0", False),
        ("db.example.invalid", "nuzantara_test_gw0", False),
        ("localhost.example.invalid", "nuzantara_test_gw0", False),
    ],
)
async def test_isolated_dsn_boundary(
    monkeypatch: pytest.MonkeyPatch, host: str, name: str, allowed: bool
) -> None:
    dsn = f"postgresql://fixture@{host}/{name}"
    monkeypatch.setattr(sys.modules[__name__], "TEST_DSN", dsn)
    if allowed:
        assert isolated_dsn.__wrapped__() == dsn
    else:
        with pytest.raises(AssertionError):
            isolated_dsn.__wrapped__()


def _stub_non_database_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep actual pool creation; exclude unrelated jobs and external services."""
    attendance = SimpleNamespace(start_schedulers=AsyncMock())
    timesheet = SimpleNamespace(start_auto_logout_monitor=AsyncMock())
    notifier = SimpleNamespace(start=AsyncMock())
    modules = {
        "backend.services.analytics.attendance_monitor": {
            "AttendanceMonitor": MagicMock(return_value=attendance)
        },
        "backend.services.analytics.team_timesheet_service": {
            "init_timesheet_service": MagicMock(return_value=timesheet)
        },
        "backend.services.analytics.daily_checkin_notifier": {
            "init_daily_notifier": MagicMock(return_value=notifier)
        },
        "backend.services.analytics.weekly_email_reporter": {
            "init_weekly_reporter": MagicMock(return_value=notifier)
        },
        "backend.services.misc.graph_service": {"GraphService": MagicMock()},
        "backend.services.security.token_revocation": {"set_revocation_db_pool": MagicMock()},
        "backend.core.cache": {"CacheService": MagicMock()},
    }
    for name, attributes in modules.items():
        monkeypatch.setitem(sys.modules, name, _module(name, **attributes))
    monkeypatch.setattr(initializer, "_initialize_redis_manager", MagicMock())
    monkeypatch.setattr(initializer, "initialize_garuda_services", AsyncMock())
    monkeypatch.setattr(initializer, "probe_garuda_artifact_bucket", AsyncMock())
    monkeypatch.setattr(initializer, "_database_health_check_loop", AsyncMock())
    monkeypatch.setenv("DISABLE_BACKGROUND_WORKERS", "1")
    monkeypatch.setenv("SELF_HEALING_ENABLED", "false")


@pytest.mark.parametrize("mode", ["full", "light", "standalone"])
async def test_actual_startup_reaches_factory_and_retains_connection_policy(
    monkeypatch: pytest.MonkeyPatch, isolated_dsn: str, mode: str
) -> None:
    factory = AsyncMock(wraps=database.create_database_pool)
    db_initializer = initializer.initialize_database_services
    app = SimpleNamespace(state=SimpleNamespace())
    if mode == "full":
        app, calls = _configure_full_initializer(monkeypatch)
        monkeypatch.setattr(initializer, "initialize_database_services", db_initializer)
    else:
        monkeypatch.setattr(initializer, "service_registry", MagicMock())
    _stub_non_database_services(monkeypatch)
    monkeypatch.setattr(initializer.settings, "database_url", isolated_dsn)
    monkeypatch.setattr(database, "create_database_pool", factory)
    monkeypatch.setattr(initializer, "create_database_pool", factory)
    pool = None
    try:
        if mode == "full":
            await initializer.initialize_services(app)
            pool = app.state.db_pool
            assert app.state.services_initialized is True
            assert calls["channel"].await_args.args[-1] is pool
        elif mode == "light":
            await initializer.initialize_services_light(app)
            pool = app.state.db_pool
        else:
            pool = await database.get_db_pool()

        assert isinstance(pool, asyncpg.Pool)
        factory.assert_awaited_once()
        policy = factory.await_args.kwargs["policy"]
        assert policy.min_size == pool.get_min_size() == 2
        assert policy.max_size == pool.get_max_size() == (5 if mode == "standalone" else 10)
        assert policy.command_timeout == (30 if mode == "full" else 60)
        assert policy.max_inactive_connection_lifetime == (300 if mode == "standalone" else 30)
        assert policy.validate_after_create is (mode == "full")
        assert policy.validate_on_connect is (mode == "full")
        assert factory.await_args.kwargs.get("normalize_dsn", True) is (mode != "standalone")

        async with pool.acquire() as connection:
            # Full validation already released/reset this connection. Light has
            # not; its configured timeout applies to the first consumer only.
            assert await connection.fetchval("SHOW statement_timeout") == (
                "30s" if mode == "light" else "0"
            )
            row = await connection.fetchrow(
                "SELECT jsonb_typeof($1::jsonb), $1::jsonb",
                {"probe": "pool-composition", "day": date(2026, 10, 24)},
            )
            assert row[0] == "object"
            assert row[1] == {"probe": "pool-composition", "day": "2026-10-24"}
        async with pool.acquire() as connection:
            assert await connection.fetchval("SHOW statement_timeout") == "0"
    finally:
        pool = pool or getattr(app.state, "db_pool", None)
        task = getattr(app.state, "db_health_check_task", None)
        if task is not None:
            await task
        if pool is not None:
            async with asyncio.timeout(10):
                await pool.close()


async def _rejected_pool_releases_the_connections_it_opened(
    monkeypatch: pytest.MonkeyPatch, isolated_dsn: str
) -> None:
    real_fetchval = asyncpg.Connection.fetchval
    created = []
    opened: list[int] = []
    real_create_pool = asyncpg.create_pool

    async def capture_pool(*args, **kwargs):
        initialize = kwargs.get("init")

        async def record_then_initialize(connection):
            # asyncpg calls this once for every connection the pool opens.
            opened.append(connection.get_server_pid())
            if initialize is not None:
                await initialize(connection)

        pool = await real_create_pool(*args, **{**kwargs, "init": record_then_initialize})
        created.append(pool)
        return pool

    async def reject_validation(connection, query, *args, **kwargs):
        value = await real_fetchval(connection, query, *args, **kwargs)
        return 0 if query == "SELECT 1" else value

    monkeypatch.setattr(database.asyncpg, "create_pool", capture_pool)
    monkeypatch.setattr(asyncpg.Connection, "fetchval", reject_validation)
    observer = None
    try:
        with pytest.raises(ValueError, match="Pool validation failed"):
            await database.create_database_pool(
                dsn=isolated_dsn,
                policy=database.DatabasePoolPolicy(
                    min_size=2,
                    max_size=2,
                    command_timeout=30,
                    max_inactive_connection_lifetime=30,
                    validate_after_create=True,
                ),
            )
        assert len(created) == 1
        # Both connections of the pool were seen opening: with none recorded the
        # wait below would have nothing to watch and would pass for nothing.
        assert len(set(opened)) == 2, opened
        observer = await asyncpg.connect(isolated_dsn)
        await _wait_for_connections_to_close(observer, opened, 3)
    finally:
        if observer is not None:
            await observer.close()
        # Test cleanup must not manufacture the assertion above: it happens only
        # after observing absence, or after a preserved failing assertion/timeout.
        for pool in created:
            pool.terminate()


async def test_failed_post_create_validation_releases_real_server_connections(
    monkeypatch: pytest.MonkeyPatch, isolated_dsn: str
) -> None:
    await _rejected_pool_releases_the_connections_it_opened(monkeypatch, isolated_dsn)


async def test_a_connection_the_rejected_pool_did_not_open_is_not_counted_against_it(
    monkeypatch: pytest.MonkeyPatch, isolated_dsn: str
) -> None:
    """Another connection idle on the same database, as an earlier test's pool leaves one."""
    bystander = await asyncpg.connect(
        isolated_dsn, server_settings={"application_name": "another_tests_pool"}
    )
    try:
        await _rejected_pool_releases_the_connections_it_opened(monkeypatch, isolated_dsn)
        # It was there the whole time and it is still usable.
        assert await bystander.fetchval("SELECT 41 + 1") == 42
    finally:
        await bystander.close()


async def test_a_connection_the_pool_opened_and_kept_fails_the_wait(isolated_dsn: str) -> None:
    """The wait is not satisfied by looking away: a watched connection that stays is a red."""
    observer = await asyncpg.connect(isolated_dsn)
    kept = await asyncpg.connect(isolated_dsn)
    gone = await asyncpg.connect(isolated_dsn)
    try:
        gone_pid = gone.get_server_pid()
        await gone.close()
        await _wait_for_connections_to_close(observer, [gone_pid], 3)
        with pytest.raises(TimeoutError, match="1 still connected"):
            await _wait_for_connections_to_close(observer, [gone_pid, kept.get_server_pid()], 0.2)
    finally:
        await kept.close()
        await observer.close()


async def test_the_wait_names_the_connection_it_timed_out_on(isolated_dsn: str) -> None:
    observer = await asyncpg.connect(isolated_dsn)
    held = None
    try:
        held = await asyncpg.connect(
            isolated_dsn, server_settings={"application_name": "pool_composition_probe"}
        )
        with pytest.raises(TimeoutError) as caught:
            await _wait_for_connections_to_close(observer, [held.get_server_pid()], 0.2)
        message = str(caught.value)
        assert "application_name='pool_composition_probe'" in message
        assert f"pid={held.get_server_pid()} datname=" in message
        assert "after 0.2 s" in message
    finally:
        if held is not None:
            await held.close()
        await observer.close()
