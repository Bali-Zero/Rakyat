"""Invitation addresses are case-insensitive end to end (real Postgres)."""

import pytest

from backend.services.portal.invite_service import InviteService
from backend.services.portal.portal_profile_service import PortalProfileService

MIXED = "  Mixed.Case@Example.test "
LOWER = "mixed.case@example.test"


async def _logins(pool, client_id):
    return await pool.fetch(
        "SELECT id, email FROM team_members WHERE linked_client_id = $1", client_id
    )


@pytest.mark.asyncio
async def test_mixed_case_invitation_is_stored_lowercase_and_redeemable(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client = await make_client("Client A", MIXED.strip())
    service = InviteService(pool)

    invite = await service.create_invitation(client, MIXED, "staff@example.test")

    assert invite["email"] == LOWER
    assert await pool.fetchval("SELECT email FROM client_invitations") == LOWER
    done = await service.complete_registration(invite["token"], "123456")
    assert done["email"] == LOWER
    assert [r["email"] for r in await _logins(pool, client)] == [LOWER]


@pytest.mark.asyncio
async def test_later_profile_provisioning_reuses_the_registered_login(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client = await make_client("Client A", MIXED.strip())
    service = InviteService(pool)
    invite = await service.create_invitation(client, MIXED, "staff@example.test")
    done = await service.complete_registration(invite["token"], "123456")

    again = await PortalProfileService(pool).ensure_portal_profile(
        client, MIXED.strip(), "Client A"
    )

    assert again == done["user_id"]
    assert len(await _logins(pool, client)) == 1


@pytest.mark.asyncio
async def test_invitation_stored_with_mixed_case_before_the_fix_still_redeems_lowercase(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client = await make_client("Client A")
    service = InviteService(pool)
    token = "legacy-mixed-case-token"
    from backend.services.portal.invite_service import _hash_token

    await pool.execute(
        "INSERT INTO client_invitations (client_id, email, token, expires_at, created_by)"
        " VALUES ($1, 'Legacy.Mixed@Example.test', $2, NOW() + interval '1 hour', 'staff')",
        client,
        _hash_token(token),
    )

    done = await service.complete_registration(token, "123456")

    assert done["email"] == "legacy.mixed@example.test"
    assert [r["email"] for r in await _logins(pool, client)] == ["legacy.mixed@example.test"]
    assert (
        await pool.fetchval("SELECT email FROM client_invitations") == "Legacy.Mixed@Example.test"
    )


@pytest.mark.asyncio
async def test_mixed_case_invitation_redeemed_over_a_provisioned_login_keeps_it_lowercase(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client = await make_client("Client A")
    provisioned = await PortalProfileService(pool).ensure_portal_profile(
        client, "legacy.mixed@example.test", "Client A"
    )
    token = "legacy-mixed-case-token-over-a-provisioned-login"
    from backend.services.portal.invite_service import _hash_token

    await pool.execute(
        "INSERT INTO client_invitations (client_id, email, token, expires_at, created_by)"
        " VALUES ($1, 'Legacy.Mixed@Example.test', $2, NOW() + interval '1 hour', 'staff')",
        client,
        _hash_token(token),
    )

    done = await InviteService(pool).complete_registration(token, "123456")

    assert str(done["user_id"]) == str(provisioned)
    assert [r["email"] for r in await _logins(pool, client)] == ["legacy.mixed@example.test"]


@pytest.mark.asyncio
async def test_lowercase_invitation_is_unchanged(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client = await make_client("Client A", "plain@example.test")
    service = InviteService(pool)

    invite = await service.create_invitation(client, "plain@example.test", "staff@example.test")
    done = await service.complete_registration(invite["token"], "123456")

    assert done["email"] == "plain@example.test"
