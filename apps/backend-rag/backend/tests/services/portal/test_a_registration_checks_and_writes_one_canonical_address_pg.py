"""A registration checks and writes the invitation address in one canonical spelling (real PostgreSQL).

An invitation stored before addresses were normalised can hold mixed case and spaces. The
address the login would be written with is the one every collision check reads. Invented
rows only.
"""

from __future__ import annotations

import pytest

from backend.services.portal.invite_service import InviteService, _hash_token
from backend.services.portal.portal_profile_service import PortalProfileService

REGISTERED_HASH = "$2b$12$registeredhashregisteredhashregisteredhashregisteredha"
TAKEN = "Mixed.Case@Example.test"


async def _legacy_invitation(pool, client: int, email: str, token: str) -> None:
    await pool.execute(
        "INSERT INTO client_invitations (client_id, email, token, expires_at, created_by)"
        " VALUES ($1, $2, $3, NOW() + interval '1 hour', 'staff')",
        client,
        email,
        _hash_token(token),
    )


async def _another_clients_login(pool, make_client) -> None:
    other = await make_client("Client A")
    await pool.execute(
        "INSERT INTO team_members (name, full_name, email, pin_hash, role, linked_client_id,"
        " portal_access, active) VALUES ('Client A', 'Client A', $1, $2, 'client', $3, true, true)",
        TAKEN,
        REGISTERED_HASH,
        other,
    )


async def _logins(pool) -> list[tuple[str, int | None]]:
    rows = await pool.fetch("SELECT email, linked_client_id FROM team_members ORDER BY email")
    return [(row["email"], row["linked_client_id"]) for row in rows]


@pytest.mark.asyncio
async def test_a_first_login_at_an_address_taken_in_another_case_is_refused(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    await _another_clients_login(pool, make_client)
    client_c = await make_client("Client C", TAKEN)
    await _legacy_invitation(pool, client_c, f" {TAKEN} ", "legacy-insert-branch")
    before = await _logins(pool)

    with pytest.raises(ValueError, match="Another account already uses this email address"):
        await InviteService(pool).complete_registration("legacy-insert-branch", "123456")

    assert await _logins(pool) == before


@pytest.mark.asyncio
async def test_a_placeholder_moved_to_an_address_taken_in_another_case_is_refused(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    await _another_clients_login(pool, make_client)
    client_c = await make_client("Client C", TAKEN)
    await PortalProfileService(pool).ensure_portal_profile(client_c, "c@example.test", "Client C")
    await _legacy_invitation(pool, client_c, f" {TAKEN} ", "legacy-update-branch")
    before = await _logins(pool)

    with pytest.raises(ValueError, match="Another account already uses this email address"):
        await InviteService(pool).complete_registration("legacy-update-branch", "123456")

    assert await _logins(pool) == before


@pytest.mark.asyncio
async def test_a_placeholder_redeemed_at_the_address_it_holds_registers(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client_c = await make_client("Client C", "c@example.test")
    login = await PortalProfileService(pool).ensure_portal_profile(
        client_c, "c@example.test", "Client C"
    )
    await _legacy_invitation(pool, client_c, " C@Example.test ", "same-address")

    done = await InviteService(pool).complete_registration("same-address", "123456")

    assert str(done["user_id"]) == str(login)
    assert await _logins(pool) == [("c@example.test", client_c)]


@pytest.mark.asyncio
async def test_an_invitation_with_no_address_creates_no_login(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_c = await make_client("Client C", "c@example.test")
    await _legacy_invitation(pool, client_c, "   ", "blank-insert-branch")

    with pytest.raises(ValueError, match="Invitation expired"):
        await InviteService(pool).complete_registration("blank-insert-branch", "123456")

    assert await _logins(pool) == []


@pytest.mark.asyncio
async def test_an_invitation_with_no_address_keeps_the_stored_one(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_c = await make_client("Client C", "c@example.test")
    await PortalProfileService(pool).ensure_portal_profile(client_c, "c@example.test", "Client C")
    await _legacy_invitation(pool, client_c, "   ", "blank-update-branch")

    with pytest.raises(ValueError, match="Invitation expired"):
        await InviteService(pool).complete_registration("blank-update-branch", "123456")

    assert await _logins(pool) == [("c@example.test", client_c)]
