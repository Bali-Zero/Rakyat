import asyncio

import pytest

from backend.services.monitoring import unified_health_service as health_module
from backend.services.monitoring.unified_health_service import (
    HealthCheckResult,
    SystemMetrics,
    UnifiedHealthService,
)


def _set_checks(service: UnifiedHealthService, statuses: dict[str, str]) -> None:
    for name, status in statuses.items():
        setattr(
            service,
            f"check_{name}",
            lambda name=name, status=status: _async_value(HealthCheckResult(name, status, status)),
        )
    service.get_system_metrics = lambda: _async_value(SystemMetrics(1, 2, 3, 4, 5))


@pytest.mark.asyncio
async def test_run_all_checks_treats_skipped_dependency_as_neutral() -> None:
    service = UnifiedHealthService()
    _set_checks(
        service,
        {
            "database": "ok",
            "qdrant": "ok",
            "redis": "skipped",
            "api": "ok",
            "crm_models": "ok",
            "collection_manager": "ok",
        },
    )

    report = await service.run_all_checks(use_cache=False)

    assert report["overall_status"] == "ok"


@pytest.mark.asyncio
async def test_run_all_checks_keeps_error_status_for_failing_dependency() -> None:
    service = UnifiedHealthService()
    _set_checks(
        service,
        {
            "database": "error",
            "qdrant": "ok",
            "redis": "ok",
            "api": "ok",
            "crm_models": "ok",
            "collection_manager": "ok",
        },
    )

    report = await service.run_all_checks(use_cache=False)

    assert report["overall_status"] == "error"


@pytest.mark.asyncio
async def test_run_all_checks_with_nothing_configured_stays_unknown() -> None:
    service = UnifiedHealthService()
    _set_checks(
        service,
        dict.fromkeys(
            ("database", "qdrant", "redis", "api", "crm_models", "collection_manager"), "skipped"
        ),
    )

    report = await service.run_all_checks(use_cache=False)

    assert report["overall_status"] == "unknown"


class _Connection:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.closed = False

    async def execute(self, query: str) -> str:
        if self.fail:
            raise RuntimeError("synthetic query failure")
        return "SELECT 1"

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [True, False])
async def test_the_database_check_closes_its_connection(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    connection = _Connection(fail=fail)

    async def connect(url: str) -> _Connection:
        return connection

    monkeypatch.setattr(health_module.settings, "database_url", "postgres://example", raising=False)
    monkeypatch.setattr(health_module.asyncpg, "connect", connect)

    result = await UnifiedHealthService().check_database()

    assert result.status == ("error" if fail else "ok")
    assert connection.closed is True


@pytest.mark.asyncio
async def test_the_database_check_answers_when_the_connect_never_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never = asyncio.Event()

    async def connect(url: str) -> None:
        await never.wait()

    monkeypatch.setattr(health_module.settings, "database_url", "postgres://example", raising=False)
    monkeypatch.setattr(health_module.asyncpg, "connect", connect)
    monkeypatch.setattr(health_module, "DATABASE_CONNECT_TIMEOUT_SECONDS", 0.05, raising=False)

    try:
        result = await asyncio.wait_for(UnifiedHealthService().check_database(), timeout=3)
    except TimeoutError:
        result = None

    assert result is not None, "the check is still waiting for the connection"
    assert result.status == "error"


async def _async_value(value: object) -> object:
    return value
