from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI


def _pool_with_connection() -> tuple[MagicMock, MagicMock]:
    pool = MagicMock()
    conn = MagicMock()

    class AsyncContext:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return None

    pool.acquire.return_value = AsyncContext()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    return pool, conn


def _cache_misses(monkeypatch: pytest.MonkeyPatch, function) -> None:
    for cell in function.__closure__ or ():
        cache = cell.cell_contents
        if hasattr(cache, "_generate_key") and hasattr(cache, "get") and hasattr(cache, "set"):
            monkeypatch.setattr(cache, "get", AsyncMock(return_value=None))
            monkeypatch.setattr(cache, "set", AsyncMock(return_value=None))
            return
    raise AssertionError("cached function did not retain a cache instance")


@pytest.mark.asyncio
async def test_expiry_alerts_outside_owner_filter_returns_before_query() -> None:
    from backend.app.routers.crm_enhanced_alerts import get_all_expiry_alerts

    pool, conn = _pool_with_connection()
    with patch(
        "backend.app.routers.crm_enhanced_alerts.get_practices_user_filter",
        return_value=["a@example.test", "b@example.test"],
    ):
        result = await get_all_expiry_alerts(
            assigned_to="c@example.test", pool=pool, current_user={}
        )

    assert result == []
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_alerts_matches_filtered_owner_case_insensitively() -> None:
    from backend.app.routers.crm_enhanced_alerts import get_all_expiry_alerts

    pool, conn = _pool_with_connection()
    with patch(
        "backend.app.routers.crm_enhanced_alerts.get_practices_user_filter",
        return_value=["a@example.test", "b@example.test"],
    ):
        await get_all_expiry_alerts(assigned_to="B@Example.Test", pool=pool, current_user={})

    assert conn.fetch.await_args.args[1] == ["b@example.test"]


@pytest.mark.asyncio
async def test_expiry_alerts_preserves_full_view_and_filter_without_assignment() -> None:
    from backend.app.routers.crm_enhanced_alerts import get_all_expiry_alerts

    full_pool, full_conn = _pool_with_connection()
    with patch(
        "backend.app.routers.crm_enhanced_alerts.get_practices_user_filter", return_value=None
    ):
        await get_all_expiry_alerts(assigned_to="a@example.test", pool=full_pool, current_user={})
    assert full_conn.fetch.await_args.args[1] == ["a@example.test"]

    filtered_pool, filtered_conn = _pool_with_connection()
    owners = "a@example.test"
    with patch(
        "backend.app.routers.crm_enhanced_alerts.get_practices_user_filter", return_value=owners
    ):
        await get_all_expiry_alerts(assigned_to=None, pool=filtered_pool, current_user={})
    assert filtered_conn.fetch.await_args.args[1] is owners


@pytest.mark.asyncio
async def test_active_practices_outside_owner_filter_returns_before_query() -> None:
    from backend.app.routers.crm_practices import get_active_practices

    pool, conn = _pool_with_connection()
    with patch(
        "backend.app.routers.crm_practices.process_assignees",
        return_value=["a@example.test", "b@example.test"],
    ):
        result = await get_active_practices(
            request=MagicMock(),
            assigned_to="c@example.test",
            db_pool=pool,
            current_user={},
        )

    assert result == []
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_practices_matches_filtered_owner_case_insensitively() -> None:
    from backend.app.routers.crm_practices import get_active_practices

    pool, conn = _pool_with_connection()
    with patch(
        "backend.app.routers.crm_practices.process_assignees",
        return_value=["a@example.test", "b@example.test"],
    ):
        await get_active_practices(
            request=MagicMock(),
            assigned_to="B@Example.Test",
            db_pool=pool,
            current_user={},
        )

    assert conn.fetch.await_args.args[1] == ["b@example.test"]


@pytest.mark.asyncio
async def test_active_practices_preserves_full_view_and_filter_without_assignment() -> None:
    from backend.app.routers.crm_practices import get_active_practices

    full_pool, full_conn = _pool_with_connection()
    with patch("backend.app.routers.crm_practices.process_assignees", return_value=None):
        await get_active_practices(
            request=MagicMock(),
            assigned_to="a@example.test",
            db_pool=full_pool,
            current_user={},
        )
    assert full_conn.fetch.await_args.args[1] == ["a@example.test"]

    filtered_pool, filtered_conn = _pool_with_connection()
    owners = ["a@example.test"]
    with patch("backend.app.routers.crm_practices.process_assignees", return_value=owners):
        await get_active_practices(
            request=MagicMock(), assigned_to=None, db_pool=filtered_pool, current_user={}
        )
    assert filtered_conn.fetch.await_args.args[1] is owners


def test_stats_route_does_not_expose_internal_parameters() -> None:
    from backend.app.routers.crm_practices import router

    app = FastAPI()
    app.include_router(router)
    operation = app.openapi()["paths"]["/api/crm/practices/stats/overview"]["get"]

    assert {parameter["name"] for parameter in operation.get("parameters", [])}.isdisjoint(
        {"is_admin", "user_id", "pool", "request"}
    )


@pytest.mark.asyncio
async def test_stats_route_uses_session_owner_filter_and_unauthenticated_call_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.routers.crm_practices import (
        _scoped_practices_stats,
        get_practices_stats,
        get_practices_stats_route,
    )

    _cache_misses(monkeypatch, _scoped_practices_stats)
    route_pool, route_conn = _pool_with_connection()
    route_conn.fetch = AsyncMock(side_effect=[[], []])
    route_conn.fetchrow = AsyncMock(side_effect=[{}, {"count": 0}])
    current_user = {"email": "viewer@example.test", "role": "team"}
    await get_practices_stats_route(db_pool=route_pool, current_user=current_user)
    assert route_conn.fetch.await_args_list[0].args[1] == ["viewer@example.test"]

    internal_pool, internal_conn = _pool_with_connection()
    internal_conn.fetch = AsyncMock(side_effect=[[], []])
    internal_conn.fetchrow = AsyncMock(side_effect=[{}, {"count": 0}])
    await get_practices_stats(db_pool=internal_pool, current_user={})
    assert internal_conn.fetch.await_args_list[0].args[1] == []
