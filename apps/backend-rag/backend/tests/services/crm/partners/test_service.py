# tests/services/crm/partners/test_service.py
import uuid
from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.services.crm.partners.service import (
    ConflictError,
    PartnersService,
    verify_partner_access,
)


@pytest.fixture
async def svc(db_conn):
    return PartnersService(db_conn)


@pytest.mark.asyncio
async def test_create_partner_writes_audit_log(svc, user_factory):
    team = await user_factory(role="team")
    pid = await svc.create_partner(
        full_name="H",
        email="q@d.io",
        entity_type="individual",
        assigned_to=team,
        created_by=team,
    )
    audit = await svc.list_audit(pid)
    assert len(audit) == 1
    assert audit[0].action == "created"
    assert audit[0].actor_user_id == team


@pytest.mark.asyncio
async def test_activate_partner_requires_admin_and_audits(svc, user_factory):
    team = await user_factory(role="team")
    admin = await user_factory(role="admin")
    pid = await svc.create_partner(
        full_name="H",
        email="u4@d.io",
        entity_type="individual",
        created_by=team,
    )
    with pytest.raises(HTTPException) as exc:
        await svc.activate_partner(pid, actor_user=team)
    assert exc.value.status_code == 403

    await svc.activate_partner(pid, actor_user=admin)
    p = await svc.get_partner(pid, actor_user=admin)
    assert p.onboarding_status == "active"
    audit = await svc.list_audit(pid)
    assert any(a.action == "activated" for a in audit)


@pytest.mark.asyncio
async def test_verify_partner_access_admin_always_allowed(svc, user_factory, partner_factory):
    admin = await user_factory(role="admin")
    # partner_factory returns a UUID
    partner_id = await partner_factory(assigned_to=None)
    result = await verify_partner_access(svc, admin, partner_id)
    # admin can always access; result is a Partner with matching id
    assert result.id == partner_id


@pytest.mark.asyncio
async def test_verify_partner_access_team_must_own(svc, user_factory, partner_factory):
    u1 = await user_factory(role="team")
    u2 = await user_factory(role="team")
    # partner_factory returns a UUID
    partner_id = await partner_factory(assigned_to=u1)
    # u1 (owner) can access
    result = await verify_partner_access(svc, u1, partner_id)
    assert result.id == partner_id
    # u2 (non-owner) cannot
    with pytest.raises(HTTPException) as exc:
        await verify_partner_access(svc, u2, partner_id)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_reassign_requires_reason_and_records_audit(svc, user_factory):
    admin = await user_factory(role="admin")
    u1 = await user_factory(role="team")
    u2 = await user_factory(role="team")
    pid = await svc.create_partner(
        full_name="H",
        email="u5@d.io",
        entity_type="individual",
        assigned_to=u1,
        created_by=admin,
    )
    with pytest.raises(ValueError, match="reason"):
        await svc.reassign_partner(pid, new_user_id=u2, actor_user=admin, reason=None)
    await svc.reassign_partner(
        pid, new_user_id=u2, actor_user=admin, reason="u1 left bali zero 2026-04"
    )
    audit = await svc.list_audit(pid)
    reassign = next(a for a in audit if a.action == "reassigned")
    assert reassign.reason == "u1 left bali zero 2026-04"


@pytest.mark.asyncio
async def test_orphan_partners_on_team_user_deactivation(svc, user_factory):
    admin = await user_factory(role="admin")
    u = await user_factory(role="team")
    p1 = await svc.create_partner(
        full_name="A",
        email="h@e.io",
        entity_type="individual",
        assigned_to=u,
        created_by=admin,
    )
    p2 = await svc.create_partner(
        full_name="B",
        email="k@e.io",
        entity_type="individual",
        assigned_to=u,
        created_by=admin,
    )
    n = await svc.orphan_partners_of_user(u, actor_user=admin)
    assert n == 2
    for pid in (p1, p2):
        p = await svc.get_partner(pid, actor_user=admin)
        assert p.assigned_to is None
        audit = await svc.list_audit(pid)
        assert any(a.action == "orphaned" for a in audit)


@pytest.mark.asyncio
async def test_update_partner_does_not_reset_welcome_email_sent(svc, user_factory):
    """update_partner must NOT touch welcome_email_sent_at."""
    admin = await user_factory(role="admin")
    pid = await svc.create_partner(
        full_name="WelcomeTest",
        email="al@d.io",
        entity_type="individual",
        created_by=admin,
    )
    # Mark welcome sent
    await svc.mark_welcome_sent(pid)

    # Fetch and confirm welcome_email_sent_at is set
    p_before = await svc.get_partner(pid, actor_user=admin)
    assert p_before.welcome_email_sent_at is not None

    # Update some field
    await svc.update_partner(
        pid,
        actor_user=admin,
        actor_role="admin",
        full_name="WelcomeTest Updated",
    )

    # welcome_email_sent_at must still be set
    p_after = await svc.get_partner(pid, actor_user=admin)
    assert p_after.welcome_email_sent_at is not None
    assert p_after.welcome_email_sent_at == p_before.welcome_email_sent_at


@pytest.mark.asyncio
async def test_deactivate_partner_soft_delete_preserves_history(
    svc, user_factory, referral_factory, practice_factory
):
    """Deactivation keeps all referrals and commissions readable."""
    admin = await user_factory(role="admin")
    pid = await svc.create_partner(
        full_name="SoftDelete",
        email="u8@d.io",
        entity_type="individual",
        created_by=admin,
    )
    # Add a referral
    await referral_factory(partner_id=pid)

    # Deactivate
    await svc.deactivate_partner(pid, actor_user=admin)

    # Partner status is inactive
    p = await svc.get_partner(pid, actor_user=admin)
    assert p.onboarding_status == "inactive"

    # Referrals still readable
    referrals = await svc.repo.list_referrals_for_partner(pid)
    assert len(referrals) >= 1

    # Audit log has deactivated entry
    audit = await svc.list_audit(pid)
    assert any(a.action == "deactivated" for a in audit)


@pytest.mark.asyncio
async def test_create_rejects_partner_with_internal_email(svc, user_factory):
    """create_partner raises ConflictError (409) when email matches a team/admin user.

    CATA-5: user_factory uses @test.invalid domain so the row is cleaned up
    automatically by db_conn teardown.
    """
    conflict_email = "conflict-internal@test.invalid"
    admin = await user_factory(role="admin", email=conflict_email)
    with pytest.raises(ConflictError) as exc:
        await svc.create_partner(
            full_name="Conflict Test",
            email=conflict_email,
            entity_type="individual",
            created_by=admin,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_reassign_nonexistent_partner_raises_404(db_conn, user_factory):
    """reassign_partner raises HTTPException(404) when partner_id is unknown."""
    admin = await user_factory(role="admin")
    svc = PartnersService(db_conn)
    with pytest.raises(HTTPException) as exc:
        await svc.reassign_partner(
            uuid4(),
            new_user_id=None,
            actor_user=admin,
            reason="cleanup",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_partner_rejects_partner_role_self_update(
    db_conn,
    user_factory,
    partner_factory,
):
    """update_partner rejects partner-role actor at service layer (spec §7.2)."""
    partner_id = await partner_factory()
    # Link a partner-role user to this partner
    # CATA-5: user_factory returns string ID; update team_members, not users
    partner_user = await user_factory(role="partner")
    await db_conn.execute(
        "UPDATE team_members SET partner_id = $2 WHERE id = $1",
        str(partner_user),
        uuid.UUID(int=partner_id.int),
    )
    svc = PartnersService(db_conn)
    with pytest.raises(HTTPException) as exc:
        await svc.update_partner(
            partner_id,
            actor_user=partner_user,
            actor_role="partner",
            full_name="New Name",
        )
    assert exc.value.status_code == 403
    assert "may not update" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_create_partner_blocks_entity_type_foreign(db_conn, user_factory):
    """CRIT-5: entity_type='foreign' must be rejected at service layer (NB-2 compliance).

    Paying commission to a foreign partner on a standard KITAS violates KITAS
    conditions against Indonesian-source income. Blocked until v1.1 splits into
    foreign_kitap / foreign_offshore variants.
    """
    admin = await user_factory(role="admin")
    svc = PartnersService(db_conn)
    with pytest.raises(ConflictError) as exc:
        await svc.create_partner(
            full_name="Hotel X",
            email="uxxx10@dxxxxx4.io",
            entity_type="foreign",
            created_by=admin,
        )
    assert exc.value.status_code == 409
    assert "foreign" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_update_partner_empties_every_field_that_can_be_empty(svc, user_factory):
    """None reaches the row as NULL, on the real table, and the audit keeps what was there."""
    can_be_emptied = {
        "work_role": "Agent",
        "company_name": "Zz Agency",
        "office_address": "Jl. Contoh 1",
        "phone": "+62 811 000 000",
        "npwp": "00.000.000.0-000.000",
        "nik": "0000000000000000",
        "fiscal_address": "Jl. Contoh 2",
        "bank_name": "Bank Lama",
        "bank_account_holder": "Zz Holder",
        "bank_account_number": "1234567890",
        "ewallet_type": "ovo",
        "ewallet_number": "0811000000",
        "iban": "ZZ00 0000 0000",
        "payment_notes": "old rail",
    }
    admin = await user_factory(role="admin")
    pid = await svc.create_partner(
        full_name="EmptyTest",
        email="empty@d.io",
        entity_type="individual",
        created_by=admin,
        **can_be_emptied,
    )

    await svc.update_partner(
        pid, actor_user=admin, actor_role="admin", **dict.fromkeys(can_be_emptied)
    )

    after = await svc.get_partner(pid, actor_user=admin)
    assert {name: getattr(after, name) for name in can_be_emptied} == dict.fromkeys(can_be_emptied)
    assert after.full_name == "EmptyTest"
    updated = [entry for entry in await svc.list_audit(pid) if entry.action == "updated"]
    assert updated and updated[-1].before_json["bank_account_number"] == "1234567890"
    assert updated[-1].after_json["bank_account_number"] is None


@pytest.mark.asyncio
async def test_update_partner_with_a_commission_value_writes_its_audit_row(svc, user_factory):
    """The edit page always sends the commission value, a Decimal after validation: the
    audit row is written with it, as its exact digits."""
    from decimal import Decimal

    admin = await user_factory(role="admin")
    pid = await svc.create_partner(
        full_name="AuditTest",
        email="audit@d.io",
        entity_type="individual",
        created_by=admin,
        bank_account_number="1234567890",
    )

    await svc.update_partner(
        pid,
        actor_user=admin,
        actor_role="admin",
        full_name="AuditTest",
        bank_account_number=None,
        default_commission_type="percentage",
        default_commission_value=Decimal("12.5"),
    )

    after = await svc.get_partner(pid, actor_user=admin)
    assert after.default_commission_value == Decimal("12.5")
    assert after.bank_account_number is None
    updated = [entry for entry in await svc.list_audit(pid) if entry.action == "updated"]
    assert len(updated) == 1
    assert Decimal(updated[0].before_json["default_commission_value"]) == Decimal("10")
    assert updated[0].after_json["default_commission_value"] == "12.5"
    assert updated[0].before_json["bank_account_number"] == "1234567890"
    assert updated[0].after_json["bank_account_number"] is None


@pytest.mark.asyncio
async def test_update_partner_cannot_set_the_entity_type_creation_refuses(svc, user_factory):
    admin = await user_factory(role="admin")
    pid = await svc.create_partner(
        full_name="Type Edit",
        email="type1@d.io",
        entity_type="individual",
        created_by=admin,
    )
    with pytest.raises(ConflictError) as exc:
        await svc.update_partner(pid, actor_user=admin, actor_role="admin", entity_type="foreign")
    assert exc.value.status_code == 409
    assert "foreign" in exc.value.detail.lower()
    p = await svc.get_partner(pid, actor_user=admin)
    assert p.entity_type == "individual"
    assert [a for a in await svc.list_audit(pid) if a.action == "updated"] == []


@pytest.mark.asyncio
async def test_update_partner_with_legacy_foreign_type_keeps_other_edits(
    svc, user_factory, partner_factory, db_conn
):
    admin = await user_factory(role="admin")
    legacy = await partner_factory(entity_type="foreign")
    await svc.update_partner(
        legacy.id, actor_user=admin, actor_role="admin", full_name="Legacy Renamed"
    )
    p = await svc.get_partner(legacy.id, actor_user=admin)
    assert p.full_name == "Legacy Renamed"
    assert p.entity_type == "foreign"
    await svc.update_partner(
        legacy.id, actor_user=admin, actor_role="admin", entity_type="foreign", phone="000"
    )
    p = await svc.get_partner(legacy.id, actor_user=admin)
    assert p.phone == "000"
    await svc.update_partner(
        legacy.id, actor_user=admin, actor_role="admin", entity_type="individual"
    )
    p = await svc.get_partner(legacy.id, actor_user=admin)
    assert p.entity_type == "individual"
