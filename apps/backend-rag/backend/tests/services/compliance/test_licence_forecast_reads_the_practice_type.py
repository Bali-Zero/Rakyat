"""Regression tests for licence forecast rule selection."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.compliance.predictive_engine import PredictiveComplianceEngine


def _make_engine() -> PredictiveComplianceEngine:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    pool = AsyncMock()
    pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return PredictiveComplianceEngine(pool, {})


def _row(
    *,
    document_type: str,
    current_visa_type: str | None,
    practice_type_code: str | None,
) -> dict:
    return {
        "client_id": 900001,
        "full_name": "Person One",
        "assigned_to": "person.one@example.test",
        "document_type": document_type,
        "current_visa_type": current_visa_type,
        "practice_type_code": practice_type_code,
        "expiry_date": date.today() + timedelta(days=120),
        "passport_expiry": None,
    }


@pytest.mark.asyncio
async def test_licence_forecast_prefers_work_permit_practice_type_for_rule() -> None:
    forecast = await _make_engine()._build_forecast(
        _row(
            document_type="license",
            current_visa_type="KITAS Investor",
            practice_type_code="other_imta_rptka_only_12mo",
        ),
        date.today(),
    )

    assert forecast is not None
    assert forecast.matched_rule_id == "imta_annual_renewal"
    assert forecast.current_visa_type == "KITAS Investor"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document_type", "current_visa_type", "practice_type_code", "expected_rule"),
    [
        ("visa", "KITAS Investor", "other_imta_rptka_only_12mo", "kitas_investor_extend"),
        ("license", "KITAS Investor", None, "generic_visa_renewal"),
        ("license", None, "other_imta_rptka_only_12mo", "imta_annual_renewal"),
    ],
)
async def test_forecast_rule_selection_preserves_existing_non_licence_and_fallback_behaviour(
    document_type: str,
    current_visa_type: str | None,
    practice_type_code: str | None,
    expected_rule: str,
) -> None:
    forecast = await _make_engine()._build_forecast(
        _row(
            document_type=document_type,
            current_visa_type=current_visa_type,
            practice_type_code=practice_type_code,
        ),
        date.today(),
    )

    assert forecast is not None
    assert forecast.matched_rule_id == expected_rule
