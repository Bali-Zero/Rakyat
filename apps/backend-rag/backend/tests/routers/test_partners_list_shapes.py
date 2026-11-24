"""The two lists of a partner, as they go over the wire.

The workspace's partner page reads these answers field by field, and its own test
uses the rows written here (apps/mouth, partners/[id]/page.tabs.test.tsx). The
answer is a bare list, an identifier of a practice is a number and a decimal is
text: a change to any of the three is a change to that page.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.routers.partners as partners_module
from backend.app.dependencies import get_current_user, get_database_pool
from backend.services.crm.partners.models import PartnerCommission, PartnerReferral

_PARTNER_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_REFERRAL_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
_COMMISSION_ID = uuid.UUID("cccccccc-0000-0000-0000-000000000001")
_AT = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)

_REFERRAL = PartnerReferral(
    id=_REFERRAL_ID,
    partner_id=_PARTNER_ID,
    practice_id=42,
    share_percent=Decimal("100.00"),
    referred_at=_AT,
    referred_by_user_id="team-1",
    notes="Met at the expo",
)
_REFERRAL_ON_THE_WIRE = {
    "id": "bbbbbbbb-0000-0000-0000-000000000001",
    "partner_id": "aaaaaaaa-0000-0000-0000-000000000001",
    "practice_id": 42,
    "share_percent": "100.00",
    "referred_at": "2026-03-01T08:00:00Z",
    "referred_by_user_id": "team-1",
    "notes": "Met at the expo",
}

_COMMISSION = PartnerCommission(
    id=_COMMISSION_ID,
    partner_id=_PARTNER_ID,
    entry_type="accrual",
    base_amount_idr=Decimal("12500000.00"),
    commission_type_snapshot="percentage",
    commission_value_snapshot=Decimal("10.0000"),
    rule_source="partner_default",
    gross_amount_idr=Decimal("1250000.00"),
    withholding_category="pph23",
    withholding_rate=Decimal("0.0200"),
    withholding_amount_idr=Decimal("25000.00"),
    net_amount_idr=Decimal("1225000.00"),
    status="accrued",
    accrued_at=_AT,
    eligible_for_approval_at=_AT,
    created_at=_AT,
    referral_id=_REFERRAL_ID,
    practice_id=42,
)
_COMMISSION_ON_THE_WIRE = {
    "id": "cccccccc-0000-0000-0000-000000000001",
    "partner_id": "aaaaaaaa-0000-0000-0000-000000000001",
    "entry_type": "accrual",
    "base_amount_idr": "12500000.00",
    "commission_type_snapshot": "percentage",
    "commission_value_snapshot": "10.0000",
    "rule_source": "partner_default",
    "gross_amount_idr": "1250000.00",
    "withholding_category": "pph23",
    "withholding_rate": "0.0200",
    "withholding_amount_idr": "25000.00",
    "net_amount_idr": "1225000.00",
    "status": "accrued",
    "accrued_at": "2026-03-01T08:00:00Z",
    "eligible_for_approval_at": "2026-03-01T08:00:00Z",
    "created_at": "2026-03-01T08:00:00Z",
    "referral_id": "bbbbbbbb-0000-0000-0000-000000000001",
    "practice_id": 42,
    "related_commission_id": None,
    "assigned_to_snapshot": None,
    "approved_at": None,
    "approved_by": None,
    "paid_at": None,
    "paid_by": None,
    "paid_via": None,
    "payment_reference": None,
    "payment_proof_url": None,
    "receipt_type": None,
    "receipt_file_url": None,
    "manual_override_reason": None,
    "clawback_reason": None,
    "waiver_reason": None,
    "idempotency_key": None,
    "commission_email_sent_at": None,
}


@pytest.fixture
def client(mock_db_pool) -> TestClient:
    pool, _ = mock_db_pool
    application = FastAPI()
    application.include_router(partners_module.router)
    application.dependency_overrides[get_current_user] = lambda: {
        "user_id": "team-1",
        "email": "team@example.test",
        "role": "admin",
        "permissions": [],
    }
    application.dependency_overrides[get_database_pool] = lambda: pool
    return TestClient(application, raise_server_exceptions=False)


def _answer(client: TestClient, what: str, rows: list[Any]) -> Any:
    with (
        patch(
            "backend.app.routers.partners.verify_partner_access_with_role",
            AsyncMock(return_value=MagicMock()),
        ),
        patch("backend.app.routers.partners.PartnersService") as service,
    ):
        repo = service.return_value.repo = MagicMock()
        repo.list_referrals_for_partner = AsyncMock(return_value=rows)
        repo.list_commissions_for_partner = AsyncMock(return_value=rows)
        response = client.get(f"/api/partners/{_PARTNER_ID}/{what}")
    assert response.status_code == 200, response.text
    return response.json()


def test_the_referrals_of_a_partner_are_a_bare_list_of_the_stored_rows(client) -> None:
    assert _answer(client, "referrals", [_REFERRAL]) == [_REFERRAL_ON_THE_WIRE]


def test_the_commissions_of_a_partner_are_a_bare_list_of_the_stored_rows(client) -> None:
    assert _answer(client, "commissions", [_COMMISSION]) == [_COMMISSION_ON_THE_WIRE]


@pytest.mark.parametrize("what", ["referrals", "commissions"])
def test_a_partner_with_no_rows_gets_an_empty_list(client, what: str) -> None:
    assert _answer(client, what, []) == []
