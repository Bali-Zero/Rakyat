from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from backend.services.oracle import oracle_google_services as google_module
from backend.services.oracle import oracle_service as oracle_module
from backend.services.oracle.oracle_google_services import GoogleServices
from backend.services.oracle.oracle_service import OracleService


def blank_oracle_service() -> OracleService:
    service = OracleService.__new__(OracleService)
    service._db_pool = None
    service._orchestrator = None
    service._db_pool_lock = asyncio.Lock()
    service._orchestrator_lock = asyncio.Lock()
    service._entity_extractor = object()
    service._clarification_service = object()
    return service


@pytest.mark.asyncio
async def test_concurrent_first_orchestrator_requests_create_one_pool_and_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = blank_oracle_service()
    pool = object()
    orchestrator = SimpleNamespace(entity_extractor=None)
    pool_creations = 0
    orchestrator_creations = 0

    async def create_pool(**kwargs: Any) -> object:
        nonlocal pool_creations
        pool_creations += 1
        await asyncio.sleep(0)
        return pool

    def create_orchestrator(**kwargs: Any) -> SimpleNamespace:
        nonlocal orchestrator_creations
        orchestrator_creations += 1
        return orchestrator

    monkeypatch.setattr(oracle_module.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(oracle_module, "create_agentic_rag", create_orchestrator)

    results = await asyncio.gather(
        *(service._get_orchestrator(object()) for _ in range(6)),
    )

    assert pool_creations == 1
    assert orchestrator_creations == 1
    assert results == [orchestrator] * 6
    assert orchestrator.entity_extractor is service._entity_extractor


@pytest.mark.asyncio
async def test_later_orchestrator_request_returns_cached_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = blank_oracle_service()
    pool = object()
    orchestrator = SimpleNamespace(entity_extractor=None)

    async def create_pool(**kwargs: Any) -> object:
        return pool

    monkeypatch.setattr(oracle_module.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(oracle_module, "create_agentic_rag", lambda **kwargs: orchestrator)

    first = await service._get_orchestrator(object())
    later = await service._get_orchestrator(object())

    assert later is first


def test_first_drive_service_access_builds_without_google_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GoogleServices()
    drive_service = object()
    build_calls: list[tuple[str, str, object]] = []

    monkeypatch.setattr(
        google_module,
        "oracle_config",
        SimpleNamespace(
            google_api_key="",
            google_credentials_json='{"type": "service_account"}',
        ),
    )
    monkeypatch.setattr(
        google_module.service_account.Credentials,
        "from_service_account_info",
        lambda *args, **kwargs: object(),
    )

    def build(name: str, version: str, *, credentials: object) -> object:
        build_calls.append((name, version, credentials))
        return drive_service

    monkeypatch.setattr(google_module, "build", build)

    assert service.drive_service is drive_service
    assert len(build_calls) == 1


def test_repeated_drive_service_access_initializes_once_without_google_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GoogleServices()
    drive_service = object()
    build_calls: list[tuple[str, str, object]] = []

    monkeypatch.setattr(
        google_module,
        "oracle_config",
        SimpleNamespace(
            google_api_key="",
            google_credentials_json='{"type": "service_account"}',
        ),
    )
    monkeypatch.setattr(
        google_module.service_account.Credentials,
        "from_service_account_info",
        lambda *args, **kwargs: object(),
    )

    def build(name: str, version: str, *, credentials: object) -> object:
        build_calls.append((name, version, credentials))
        return drive_service

    monkeypatch.setattr(google_module, "build", build)

    assert service.drive_service is drive_service
    assert service.drive_service is drive_service
    assert len(build_calls) == 1
