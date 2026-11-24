"""
End-to-end: process completed+paid → accrual → approve → pay → email.

Covers the full partner lifecycle:
  activate → welcome email → referral → complete+paid →
  EventBus accrual → approve → mark paid → commission email with
  client-name sterilization (UU PDP).

Placed in backend/tests/integration/ with its own conftest.py that
re-exports all partner fixtures from services/crm/partners/conftest.py
(option a — import bridge).
"""

from __future__ import annotations

import contextlib
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

import backend.services.crm.partners.events as events_mod
from backend.services.crm.partners.commission_engine import CommissionEngine
from backend.services.crm.partners.emails import (
    enqueue_commission_earned,
    enqueue_welcome,
    flush_outbox,
)
from backend.services.crm.partners.events import handle_practice_status_changed
from backend.services.crm.partners.service import PartnersService


@pytest.mark.asyncio
async def test_full_flow_process_to_paid_email(
    db_conn,
    user_factory,
    partner_factory,
    practice_factory,
    client_factory,
    referral_factory,
    monkeypatch,
):
    # ── Setup: create users ──────────────────────────────────────────────────
    # CATA-5: user_factory returns _StrWithId (team_members.id VARCHAR), not UUID.
    admin_id = await user_factory(role="admin")
    await user_factory(role="team")
    partner_id = await partner_factory(
        tax_withholding_category="pph23",
        default_commission_value=Decimal("10.0"),
        assigned_to=admin_id,  # CATA-5: str, not UUID
    )

    # ── Capture email sends ─────────────────────────────────────────────────
    send_calls: list[dict] = []

    async def fake_post(*, to, cc, subject, body):
        send_calls.append({"to": to, "cc": cc, "subject": subject, "body": body})

    import backend.services.crm.partners.emails as emails_mod

    monkeypatch.setattr(emails_mod, "_post_email", fake_post)

    # Mock PricingService to avoid heavy loading in test environment
    monkeypatch.setattr(
        emails_mod,
        "_build_pricing_services",
        lambda: [{"name": "KITAS E33G", "price_display": "Rp 12.500.000"}],
    )

    # ── Step 1: Admin activates partner → enqueue welcome → flush ───────────
    # CRIT-2: enqueue inside activation, then flush_outbox sends the email.
    svc = PartnersService(db_conn)
    # CATA-5: actor_user is str (team_members.id VARCHAR), not UUID.
    await svc.activate_partner(uuid.UUID(int=partner_id.int), actor_user=str(admin_id))
    with patch(
        "backend.services.crm.partners.emails._build_pricing_services",
        return_value=[{"name": "KITAS E33G", "price_display": "Rp 12.500.000"}],
    ):
        await enqueue_welcome(db_conn, uuid.UUID(int=partner_id.int))
    result = await flush_outbox(db_conn)
    assert result["sent"] == 1, f"Expected 1 sent, got {result}"

    assert len(send_calls) == 1, f"Expected 1 call after welcome flush, got {len(send_calls)}"
    assert "Welcome" in send_calls[0]["subject"] or "welcome" in send_calls[0]["subject"].lower(), (
        f"Welcome subject not found: {send_calls[0]['subject']}"
    )

    # ── Step 2: Create client + process + referral ───────────────────────────
    client_id = await client_factory(full_name="Mario Rossi")

    # practice_factory does not accept client_id or a practice type directly — create
    # practice then update it to link the client.
    process_id = await practice_factory(
        actual_price=Decimal("15000000"),
        status="completed",
        payment_status="paid",
    )
    # Link client and service_type to the practice
    type_id = await db_conn.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('kitas_e33g', 'KITAS E33G') "
        "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id"
    )
    await db_conn.execute(
        "UPDATE practices SET client_id = $1, practice_type_id = $2 WHERE id = $3",
        int(client_id),
        type_id,
        int(process_id),
    )

    await referral_factory(
        partner_id=uuid.UUID(int=partner_id.int),
        practice_id=int(process_id),
    )

    # ── Step 3: EventBus handler → accrual ──────────────────────────────────
    class FakePool:
        def acquire(self):
            @contextlib.asynccontextmanager
            async def _cm():
                yield db_conn

            return _cm()

    async def fake_get_pool():
        return FakePool()

    monkeypatch.setattr(events_mod, "get_pool", fake_get_pool)

    await handle_practice_status_changed(
        {
            "practice_id": str(int(process_id)),
            "new_status": "completed",
        }
    )

    # ── Step 4: Verify accrual ───────────────────────────────────────────────
    engine = CommissionEngine(db_conn)
    commissions = await engine.repo.list_commissions_for_partner(uuid.UUID(int=partner_id.int))
    assert len(commissions) == 1, f"Expected 1 commission, got {len(commissions)}"
    c = commissions[0]
    assert c.status == "accrued", f"Expected status=accrued, got {c.status}"

    # 10% of 15_000_000 = 1_500_000 gross.
    # 10% of 15_000_000 = 1_500_000 gross.
    # CRIT-7 (PPh surcharge for partners without NPWP):
    #   pph23 base = 2.0%, no-NPWP surcharge = 20% of base → effective = 2.4%
    #   withholding = 1_500_000 * 2.4% = 36_000
    #   net = 1_500_000 - 36_000 = 1_464_000
    assert c.gross_amount_idr == Decimal("1500000"), (
        f"Expected gross 1500000, got {c.gross_amount_idr}"
    )
    assert c.withholding_amount_idr == Decimal("36000"), (
        f"Expected withholding 36000 (2.4% pph23+surcharge of 1500000), got {c.withholding_amount_idr}"
    )
    assert c.net_amount_idr == Decimal("1464000"), (
        f"Expected net 1464000 (gross - withholding), got {c.net_amount_idr}"
    )

    # ── Step 5: Fast-forward cooling-off ────────────────────────────────────
    await db_conn.execute(
        "UPDATE partner_commissions SET eligible_for_approval_at = now() - interval '1 day' WHERE id = $1",
        c.id,
    )

    # ── Step 6: Approve ──────────────────────────────────────────────────────
    # CATA-5: actor is str (team_members.id VARCHAR), not UUID.
    await engine.approve(c.id, actor=str(admin_id))
    refreshed = await engine.repo.get_commission(c.id)
    assert refreshed is not None
    assert refreshed.status == "approved", f"Expected approved, got {refreshed.status}"

    # ── Step 7: Mark paid + commission email ─────────────────────────────────
    # CRIT-2: enqueue inside mark_paid txn, then flush_outbox sends the email.
    # CATA-5: actor is str (team_members.id VARCHAR), not UUID.
    await engine.mark_paid(
        c.id,
        actor=str(admin_id),
        paid_via="BCA transfer",
        payment_reference="TX-20260520-001",
    )
    await enqueue_commission_earned(db_conn, c.id)
    await flush_outbox(db_conn)

    # ── Step 8: Verify commission email was sent ─────────────────────────────
    commission_call = next(
        (
            call
            for call in send_calls
            if "Commissione" in call["subject"]
            or "commission" in call["subject"].lower()
            or "Commission" in call["subject"]
        ),
        None,
    )
    assert commission_call is not None, (
        f"Commission email not sent; all call subjects: {[c['subject'] for c in send_calls]}"
    )

    # ── Step 9: Verify sterilization (UU PDP) ────────────────────────────────
    body = commission_call["body"]
    assert "Mario Rossi" not in body, "Full client name leaked — UU PDP violation"
    assert "Mario R." in body, "Sterilized client name 'Mario R.' missing from body"


# ── The partner update, through its handler ──────────────────────────────────


class _OneConnectionPool:
    """The handler asks a pool for a connection: it gets the test's own."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


# What the edit page sends: every field of the form, an emptied one as null, the
# commission value as a number.
_EDIT_PAGE_BODY = {
    "full_name": "Zz Partner",
    "phone": None,
    "company_name": "Zz Agency",
    "work_role": None,
    "npwp": None,
    "bank_name": "Bank Baru",
    "bank_account_number": None,
    "bank_account_holder": "Zz Holder",
    "tax_withholding_category": "pph23",
    "default_commission_type": "percentage",
    "default_commission_value": 12.5,
}


async def _partner_with_a_bank_account(db_conn, partner_factory, admin_id):
    partner_id = await partner_factory(
        default_commission_value=Decimal("10.0"), assigned_to=admin_id
    )
    await db_conn.execute(
        "UPDATE partners SET bank_name = 'Bank Lama', bank_account_number = '1234567890' "
        "WHERE id = $1",
        partner_id,
    )
    return partner_id


async def _stored(db_conn, partner_id):
    return await db_conn.fetchrow(
        "SELECT full_name, bank_name, bank_account_number, default_commission_value "
        "FROM partners WHERE id = $1",
        partner_id,
    )


@pytest.mark.asyncio
async def test_the_edit_pages_request_reaches_the_row_and_its_audit_row(
    db_conn, user_factory, partner_factory
):
    import json

    # Imported here: the router pulls in the whole CRM stack.
    from backend.app.routers.partners import PartnerUpdate, update_partner

    admin_id = await user_factory(role="admin")
    partner_id = await _partner_with_a_bank_account(db_conn, partner_factory, admin_id)

    answer = await update_partner(
        partner_id,
        body=PartnerUpdate.model_validate(_EDIT_PAGE_BODY),
        user={"user_id": admin_id, "role": "admin"},
        pool=_OneConnectionPool(db_conn),
    )

    row = await _stored(db_conn, partner_id)
    assert row["bank_account_number"] is None
    assert row["bank_name"] == "Bank Baru"
    assert row["default_commission_value"] == Decimal("12.5")
    assert answer["bank_account_number"] is None
    audit = await db_conn.fetch(
        "SELECT before_json, after_json FROM partner_audit_log "
        "WHERE partner_id = $1 AND action = 'updated'",
        partner_id,
    )
    assert len(audit) == 1
    before, after = json.loads(audit[0]["before_json"]), json.loads(audit[0]["after_json"])
    assert before["bank_account_number"] == "1234567890"
    assert Decimal(before["default_commission_value"]) == Decimal("10")
    assert after == {**_EDIT_PAGE_BODY, "default_commission_value": "12.5"}


@pytest.mark.asyncio
async def test_a_change_whose_audit_row_cannot_be_written_is_not_kept(
    db_conn, user_factory, partner_factory, monkeypatch
):
    from fastapi import HTTPException

    from backend.app.routers.partners import PartnerUpdate, update_partner
    from backend.services.crm.partners.repository import PartnersRepository

    admin_id = await user_factory(role="admin")
    partner_id = await _partner_with_a_bank_account(db_conn, partner_factory, admin_id)
    kept = await _stored(db_conn, partner_id)

    async def no_audit(self, **kwargs):
        raise RuntimeError("the audit table is away")

    monkeypatch.setattr(PartnersRepository, "insert_audit", no_audit)

    with pytest.raises(HTTPException) as refused:
        await update_partner(
            partner_id,
            body=PartnerUpdate.model_validate(_EDIT_PAGE_BODY),
            user={"user_id": admin_id, "role": "admin"},
            pool=_OneConnectionPool(db_conn),
        )

    assert refused.value.status_code == 500
    assert await _stored(db_conn, partner_id) == kept
