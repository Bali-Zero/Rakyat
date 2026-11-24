"""Regression coverage for LKPM configurations without activity codes."""

from __future__ import annotations

from typing import Any

import pytest

from backend.app.models.lkpm import ValidationSeverity
from backend.services.compliance.lkpm_service import LKPMService
from backend.services.compliance.lkpm_validator import LKPMValidator


class _AcquireContext:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)


class _FakeConnection:
    def __init__(self, company: dict[str, Any] | None = None) -> None:
        self._company = company
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, query: str, *args: object) -> None:
        _ = (query, args)
        return None

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        _ = (query, args)
        return self._company

    async def execute(self, query: str, *args: object) -> None:
        self.execute_calls.append((query, args))

    async def fetch(self, query: str, *args: object) -> list[dict[str, str]]:
        _ = (query, args)
        return [{"entity_id": "kbli:62011"}]


def _company(kbli_code: str | None) -> dict[str, Any]:
    return {
        "company_id": 900001,
        "company_name": "PT Contoh Satu",
        "npwp_company": "00.900.001.0-000.000",
        "nib": "900001000001",
        "kbli_code": kbli_code,
    }


@pytest.mark.asyncio
async def test_blank_kbli_code_is_a_blocking_alert() -> None:
    alerts = await LKPMValidator(_FakePool(_FakeConnection())).validate_kbli_match(
        client_id=900001,
        registered_kbli=[""],
    )

    assert len(alerts) == 1
    assert alerts[0].severity == ValidationSeverity.RED
    assert alerts[0].message == "No KBLI codes registered for this client"


@pytest.mark.asyncio
async def test_auto_config_stores_empty_list_for_missing_kbli_code() -> None:
    conn = _FakeConnection(_company(None))

    await LKPMService(_FakePool(conn))._ensure_client_config(900001)

    assert conn.execute_calls[0][1][-1] == []


@pytest.mark.asyncio
async def test_real_kbli_code_is_verified_and_stored() -> None:
    conn = _FakeConnection(_company("62011"))
    alerts = await LKPMValidator(_FakePool(conn)).validate_kbli_match(
        client_id=900001,
        registered_kbli=["62011"],
    )

    await LKPMService(_FakePool(conn))._ensure_client_config(900001)

    assert alerts[0].severity == ValidationSeverity.GREEN
    assert conn.execute_calls[0][1][-1] == ["62011"]
