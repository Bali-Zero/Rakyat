"""Regression tests for failed predictive-compliance source queries."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.routers import cron_notifiers
from backend.services.compliance.predictive_engine import (
    ForecastSummary,
    PredictiveComplianceEngine,
)


class _AcquireContext:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def __aenter__(self) -> Any:
        return self._connection

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Pool:
    def __init__(self, fetch_side_effect: list[Any]) -> None:
        self.connection = AsyncMock()
        self.connection.fetch = AsyncMock(side_effect=fetch_side_effect)
        self.connection.fetchval = AsyncMock(return_value=None)

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self.connection)


class _Request:
    def __init__(self) -> None:
        self.headers = {"X-API-Key": "test-key"}
        self.query_params: dict[str, str] = {}
        self.app = SimpleNamespace(state=SimpleNamespace(db_pool=object()))


def _prices() -> dict[str, Any]:
    return {"services": {"kitas_permits": {"Investor KITAS 2 Years (Extend)": {"price": "1 IDR"}}}}


def _client_row() -> dict[str, Any]:
    return {
        "client_id": 900001,
        "full_name": "Person One",
        "assigned_to": "person.one@example.test",
        "current_visa_type": "KITAS Investor",
        "passport_expiry": None,
        "document_type": "kitas",
        "expiry_date": date.today() + timedelta(days=120),
        "practice_type_code": None,
    }


def _endpoint_scan_result(failed_queries: tuple[str, ...]) -> Any:
    return SimpleNamespace(
        forecasts=[],
        summary=ForecastSummary(
            total_forecasts=0,
            by_urgency={},
            total_estimated_revenue_idr=0,
            top_revenue_forecasts=[],
            clients_with_active_practice_skipped=0,
        ),
        scan_window_days=365,
        generated_at="2026-11-22T00:00:00+00:00",
        failed_queries=failed_queries,
    )


def _fake_engine_class(result: Any) -> type:
    class _FakeEngine:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def scan(self) -> Any:
            return result

    return _FakeEngine


async def _run_endpoint(monkeypatch: pytest.MonkeyPatch, result: Any) -> dict[str, Any]:
    monkeypatch.setattr(cron_notifiers, "_API_KEY", "test-key")
    monkeypatch.setattr(
        "backend.services.compliance.predictive_engine.PredictiveComplianceEngine",
        _fake_engine_class(result),
    )
    monkeypatch.setattr(
        "backend.services.compliance.predictive_engine.is_engine_enabled",
        AsyncMock(return_value=True),
    )
    pricing_service = MagicMock()
    pricing_service.get_pricing.return_value = _prices()
    monkeypatch.setattr(
        "backend.services.pricing.pricing_service.get_pricing_service",
        MagicMock(return_value=pricing_service),
    )
    alerts_engine = MagicMock()
    alerts_engine.generate_alerts = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "backend.services.compliance.alert_wiring.build_alerts_engine",
        MagicMock(return_value=alerts_engine),
    )
    return await cron_notifiers.run_compliance_forecast(_Request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fetch_side_effect", "expected_failed_queries"),
    [
        ([RuntimeError("client documents unavailable"), []], ("client_documents",)),
        ([[], RuntimeError("practice expiries unavailable")], ("practice_expiries",)),
        (
            [
                RuntimeError("client documents unavailable"),
                RuntimeError("practice expiries unavailable"),
            ],
            ("client_documents", "practice_expiries"),
        ),
    ],
)
async def test_scan_reports_each_failed_query(
    fetch_side_effect: list[Any], expected_failed_queries: tuple[str, ...]
) -> None:
    result = await PredictiveComplianceEngine(_Pool(fetch_side_effect), _prices()).scan()

    assert getattr(result, "failed_queries", None) == expected_failed_queries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_queries",
    [
        ("client_documents",),
        ("practice_expiries",),
        ("client_documents", "practice_expiries"),
    ],
)
async def test_endpoint_reports_failed_queries(
    monkeypatch: pytest.MonkeyPatch, failed_queries: tuple[str, ...]
) -> None:
    response = await _run_endpoint(monkeypatch, _endpoint_scan_result(failed_queries))

    assert response["status"] == "error"
    assert response["reason"] == "query_failed"
    assert response["failed_queries"] == list(failed_queries)


@pytest.mark.asyncio
async def test_successful_queries_keep_the_existing_endpoint_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_result = await PredictiveComplianceEngine(_Pool([[], []]), _prices()).scan()
    response = await _run_endpoint(monkeypatch, scan_result)

    assert getattr(scan_result, "failed_queries", None) == ()
    assert response["status"] == "ok"
    assert set(response) == {
        "service",
        "status",
        "scan_window_days",
        "generated_at",
        "summary",
        "forecasts",
        "alerts_generated",
    }


@pytest.mark.asyncio
async def test_scan_keeps_forecasts_from_the_query_that_succeeded() -> None:
    result = await PredictiveComplianceEngine(
        _Pool([[_client_row()], RuntimeError("practice expiries unavailable")]), _prices()
    ).scan()

    assert getattr(result, "failed_queries", None) == ("practice_expiries",)
    assert [forecast.client_id for forecast in result.forecasts] == [900001]
