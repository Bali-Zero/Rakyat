"""Unit tests for CRM-Portal payment parity & compliance_retainers (Swarm 2 Track C)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn

    pool.acquire = acquire
    return pool


def _billing_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 1,
        "invoice_number": "INV-202609-00001",
        "amount_idr": 10000000.0,
        "invoice_source": "local_pdf",
        "drive_file_id": None,
        "drive_web_link": None,
        "email_sent_to_client": False,
        "generated_at": None,
        "created_at": None,
        "practice_id": 10,
        "practice_name": "KITAS Investor",
        "practice_category": "visa",
        "paid_amount_idr": None,
        "payment_status": "paid",
        "paid_amount": 0,
        "quoted_price": 10000000.0,
    }
    row.update(overrides)
    return row


class TestPortalBillingParity:
    @pytest.mark.asyncio
    async def test_paid_practice_with_zero_paid_amount_credits_invoiced_total(self) -> None:
        from backend.services.portal.portal_service import PortalService

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[_billing_row(payment_status="paid", paid_amount=0)])
        pool = _make_pool(conn)

        result = await PortalService(pool).get_billing(
            client_id=1,
            current_user={"client_id": 1, "email": "portal-client"},
        )

        assert result["summary"]["total_invoiced"] == 10000000.0
        assert result["summary"]["total_paid"] == 10000000.0
        assert result["summary"]["total_pending"] == 0.0
        assert result["invoices"][0]["payment_status"] == "paid"

    @pytest.mark.asyncio
    async def test_multi_invoice_partial_practice_derives_per_invoice_badges(self) -> None:
        from backend.services.portal.portal_service import PortalService

        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[
                _billing_row(
                    id=1,
                    amount_idr=6000000.0,
                    practice_id=20,
                    payment_status="partial",
                    paid_amount=6000000.0,
                    paid_amount_idr=6000000.0,
                ),
                _billing_row(
                    id=2,
                    amount_idr=4000000.0,
                    practice_id=20,
                    payment_status="partial",
                    paid_amount=6000000.0,
                    paid_amount_idr=0.0,
                ),
            ]
        )
        pool = _make_pool(conn)

        result = await PortalService(pool).get_billing(
            client_id=1,
            current_user={"client_id": 1, "email": "portal-client"},
        )

        assert result["summary"]["total_invoiced"] == 10000000.0
        assert result["summary"]["total_paid"] == 6000000.0
        assert result["summary"]["total_pending"] == 4000000.0
        assert result["invoices"][0]["payment_status"] == "paid"
        assert result["invoices"][1]["payment_status"] == "unpaid"

    @pytest.mark.asyncio
    async def test_unrecorded_invoice_paid_amount_when_other_invoice_covers_eff(self) -> None:
        from backend.services.portal.portal_service import PortalService

        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[
                _billing_row(
                    id=1,
                    amount_idr=6000000.0,
                    practice_id=21,
                    payment_status="partial",
                    paid_amount=6000000.0,
                    paid_amount_idr=None,
                ),
                _billing_row(
                    id=2,
                    amount_idr=6000000.0,
                    practice_id=21,
                    payment_status="partial",
                    paid_amount=6000000.0,
                    paid_amount_idr=6000000.0,
                ),
            ]
        )
        pool = _make_pool(conn)

        result = await PortalService(pool).get_billing(
            client_id=1,
            current_user={"client_id": 1, "email": "portal-client"},
        )

        assert result["invoices"][0]["payment_status"] == "unpaid"
        assert result["invoices"][1]["payment_status"] == "paid"


class TestUpdatePracticePaymentSync:
    @pytest.mark.asyncio
    async def test_marking_paid_syncs_paid_amount_and_invoices_table(self) -> None:
        from backend.app.routers import crm_practices

        conn = AsyncMock()
        tx_cm = MagicMock()
        tx_cm.__aenter__ = AsyncMock(return_value=None)
        tx_cm.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=tx_cm)

        existing = {
            "id": 42,
            "client_id": 7,
            "status": "sending_invoice",
            "assigned_to": "owner-alpha",
            "created_by": "owner-alpha",
            "client_lead": "owner-alpha",
            "quoted_price": 5000000,
            "discount_amount": 0,
            "actual_price": 5000000,
            "paid_amount": 0,
            "payment_status": "unpaid",
            "metadata": {},
            "client_visible": True,
            "completion_date": None,
            "start_date": None,
        }
        updated = {**existing, "payment_status": "paid", "paid_amount": 5000000}
        conn.fetchrow = AsyncMock(side_effect=[existing, updated])
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        pool = _make_pool(conn)

        with (
            patch("backend.app.routers.crm_practices.invalidate_cache", new=AsyncMock()),
            patch(
                "backend.app.routers.crm_practices._attach_invoice_company_name",
                new=AsyncMock(),
            ),
        ):
            res = await crm_practices.update_practice(
                request=MagicMock(),
                practice_id=42,
                updates=crm_practices.PracticeUpdate(payment_status="paid"),
                current_user={"email": "admin-actor", "role": "admin"},
                db_pool=pool,
            )

        assert res["payment_status"] == "paid"
        assert res["paid_amount"] == 5000000
        update_sql = conn.fetchrow.await_args_list[1].args[0]
        assert "CASE WHEN COALESCE(paid_amount, 0) > 0 THEN paid_amount" in update_sql
        inv_sql = conn.execute.await_args_list[0].args[0]
        assert "UPDATE invoices SET paid_amount_idr = amount_idr" in inv_sql


class TestComplianceRetainersCategory:
    def test_compliance_retainers_is_in_flat_categories_and_searchable(self) -> None:
        from backend.services.pricing import pricing_service as ps

        assert "compliance_retainers" in ps._FLAT_CATEGORIES
        svc = ps.PricingService.__new__(ps.PricingService)
        svc.prices = {
            "services": {
                "compliance_retainers": {
                    "Monthly Compliance Retainer": {"price": "5.000.000 IDR"}
                }
            }
        }
        svc.loaded = True
        res = svc.search_service("retainer")
        assert "compliance_retainers" in res.get("results", {})
