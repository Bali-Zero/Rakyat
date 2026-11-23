from datetime import datetime

import pytest
from pydantic import ValidationError

from backend.app.routers.crm_practices import (
    HistoricalTaxCompletionCreate,
    _can_import_historical_tax_completion,
)


def _payload(**overrides):
    payload = {
        "client_id": 1,
        "company_id": 2,
        "assigned_to": "consultant@example.test",
        "service": "lkpm_quarterly",
        "fiscal_year": 2026,
        "fiscal_period": "Q3",
        "issuer": "OSS",
        "completed_at": "2026-10-01T09:00:00+08:00",
        "evidence_references": ["receipt-1", "receipt-2"],
        "source": "controlled historical import",
    }
    payload.update(overrides)
    return payload


def test_historical_lkpm_accepts_quarter_and_wita_timestamp():
    result = HistoricalTaxCompletionCreate(**_payload())
    assert result.fiscal_period == "Q3"
    assert result.completed_at == datetime.fromisoformat("2026-10-01T09:00:00+08:00")


@pytest.mark.parametrize("period", ["Q0", "ANNUAL", "q3"])
def test_historical_lkpm_rejects_ambiguous_period(period):
    with pytest.raises(ValidationError):
        HistoricalTaxCompletionCreate(**_payload(fiscal_period=period))


def test_historical_spt_is_annual_only():
    result = HistoricalTaxCompletionCreate(
        **_payload(service="spt_annual_company", fiscal_period="ANNUAL")
    )
    assert result.service == "spt_annual_company"


def test_only_privileged_tax_roles_can_import():
    assert _can_import_historical_tax_completion({"role": "tax supervisor"})
    assert not _can_import_historical_tax_completion({"role": "tax consultant"})
