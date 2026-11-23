from __future__ import annotations

import asyncio
from pathlib import Path
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


@pytest.mark.asyncio
async def test_concurrent_direct_pool_requests_create_one_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = blank_oracle_service()
    pool = object()
    pool_creations = 0

    async def create_pool(**kwargs: Any) -> object:
        nonlocal pool_creations
        pool_creations += 1
        await asyncio.sleep(0)
        return pool

    monkeypatch.setattr(oracle_module.asyncpg, "create_pool", create_pool)

    results = await asyncio.gather(*(service._get_db_pool() for _ in range(6)))

    assert pool_creations == 1
    assert results == [pool] * 6


@pytest.mark.asyncio
async def test_a_pool_that_failed_to_open_is_opened_on_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = blank_oracle_service()
    pool = object()
    outcomes: list[object] = [ConnectionError("database not reachable"), pool]

    async def create_pool(**kwargs: Any) -> object:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(oracle_module.asyncpg, "create_pool", create_pool)

    with pytest.raises(ConnectionError):
        await service._get_db_pool()
    assert await service._get_db_pool() is pool
    assert await service._get_db_pool() is pool
    assert outcomes == []


_SYNTHETIC_KEY = "synthetic-key"  # pragma: allowlist secret


def _drive_config(monkeypatch: pytest.MonkeyPatch, *, api_key: str = "") -> None:
    monkeypatch.setattr(
        google_module,
        "oracle_config",
        SimpleNamespace(
            google_api_key=api_key,
            google_credentials_json='{"type": "service_account"}',
        ),
    )
    monkeypatch.setattr(
        google_module.service_account.Credentials,
        "from_service_account_info",
        lambda *args, **kwargs: object(),
    )


def _flaky_build(monkeypatch: pytest.MonkeyPatch, drive_service: object) -> list[str]:
    calls: list[str] = []

    def build(name: str, version: str, *, credentials: object) -> object:
        calls.append(name)
        if len(calls) == 1:
            raise TimeoutError("discovery document not reachable")
        return drive_service

    monkeypatch.setattr(google_module, "build", build)
    return calls


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(google_module, "time", clock)
    return clock


def test_a_drive_set_up_that_failed_is_tried_again_after_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _fake_clock(monkeypatch)
    service = GoogleServices()
    drive_service = object()
    _drive_config(monkeypatch)
    calls = _flaky_build(monkeypatch, drive_service)

    assert service.drive_service is None
    clock.now += google_module._DRIVE_RETRY_SECONDS - 1
    assert service.drive_service is None
    assert len(calls) == 1
    clock.now += 1
    assert service.drive_service is drive_service
    clock.now += 10 * google_module._DRIVE_RETRY_SECONDS
    assert service.drive_service is drive_service
    assert len(calls) == 2


def test_a_permanent_drive_failure_is_tried_once_per_interval_not_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _fake_clock(monkeypatch)
    service = GoogleServices()
    _drive_config(monkeypatch)
    calls: list[str] = []

    def build(name: str, version: str, *, credentials: object) -> object:
        calls.append(name)
        raise ValueError("synthetic malformed credentials")

    monkeypatch.setattr(google_module, "build", build)

    for _ in range(50):
        assert service.drive_service is None
    assert len(calls) == 1
    clock.now += google_module._DRIVE_RETRY_SECONDS
    for _ in range(50):
        assert service.drive_service is None
    assert len(calls) == 2


def test_a_failed_drive_set_up_is_retried_without_recreating_the_gemini_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.llm.genai_client as genai_client_module

    created: list[str] = []

    class _GenAIClient:
        is_available = True

        def __init__(self, *, api_key: str) -> None:
            created.append(api_key)

    monkeypatch.setattr(genai_client_module, "GenAIClient", _GenAIClient)
    monkeypatch.setattr(google_module, "GENAI_AVAILABLE", True)
    clock = _fake_clock(monkeypatch)
    service = GoogleServices()
    drive_service = object()
    _drive_config(monkeypatch, api_key=_SYNTHETIC_KEY)
    calls = _flaky_build(monkeypatch, drive_service)

    assert service.drive_service is None
    assert service.gemini_available is True
    clock.now += google_module._DRIVE_RETRY_SECONDS
    assert service.drive_service is drive_service
    assert len(calls) == 2
    assert created == [_SYNTHETIC_KEY]


def test_a_drive_without_credentials_is_settled_and_not_set_up_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reads = 0

    class _Config:
        google_api_key = ""

        @property
        def google_credentials_json(self) -> str:
            nonlocal reads
            reads += 1
            return ""

    monkeypatch.setattr(google_module, "oracle_config", _Config())
    monkeypatch.chdir(tmp_path)
    clock = _fake_clock(monkeypatch)
    service = GoogleServices()

    assert service.drive_service is None
    clock.now += 10 * google_module._DRIVE_RETRY_SECONDS
    assert service.drive_service is None
    assert reads == 1
