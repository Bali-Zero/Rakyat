"""The invitation check and the registration agree on every case (real PostgreSQL).

`validate_token` answers before the client chooses a PIN and
`complete_registration` enforces; both read the same token lookup and the same
registration-state statement. Each test runs the two doors on one invitation:
a refusal the link holder must not learn is an unknown link at the check and
its own sentence at the registration, and every other answer is the same at
both. Invented rows only.
"""

from __future__ import annotations

import pytest

from backend.services.portal.invite_service import InviteService, _hash_token
from backend.services.portal.portal_profile_service import PLACEHOLDER_PIN_HASH

REAL_PIN = "$2b$12$registeredhashregisteredhashregisteredhashregisteredha"
EMAIL_IN_USE = "Another account already uses this email address"
NOT_REGISTRABLE = "This account cannot be registered"
INVALID = "Invalid invitation token"


async def _invite(
    pool,
    client: int,
    email: str,
    token: str,
    *,
    raw: bool = False,
    used: bool = False,
    expired: bool = False,
) -> None:
    await pool.execute(
        "INSERT INTO client_invitations (client_id, email, token, expires_at, used_at, created_by)"
        " VALUES ($1, $2, $3,"
        " NOW() + CASE WHEN $5 THEN interval '-1 hour' ELSE interval '1 hour' END,"
        " CASE WHEN $4 THEN NOW() END, 'staff')",
        client,
        email,
        token if raw else _hash_token(token),
        used,
        expired,
    )


async def _login(
    pool,
    client: int | None,
    email: str,
    *,
    role: str = "client",
    pin: str = PLACEHOLDER_PIN_HASH,
    login_id: str | None = None,
    created_at: str | None = None,
) -> str:
    return await pool.fetchval(
        "INSERT INTO team_members (id, name, full_name, email, pin_hash, role,"
        " linked_client_id, portal_access, active, created_at)"
        " VALUES (COALESCE($1, gen_random_uuid()::text), 'Login', 'Login', $2, $3, $4, $5,"
        " true, true, COALESCE($6::text::timestamptz, NOW())) RETURNING id",
        login_id,
        email,
        pin,
        role,
        client,
        created_at,
    )


async def _both(pool, token: str) -> tuple[object, object]:
    validated = await InviteService(pool).validate_token(token)
    try:
        registered = await InviteService(pool).complete_registration(token, "123456")
    except ValueError as refused:
        registered = str(refused)
    return validated, registered


def _valid(validated: object) -> bool:
    return isinstance(validated, dict) and validated.get("valid") is True


@pytest.mark.asyncio
async def test_unicode_space_around_the_address_is_trimmed_once(portal_pg_pool, make_client):
    pool = portal_pg_pool
    await _login(pool, await make_client("Other"), "held@example.test", pin=REAL_PIN)
    client = await make_client("Client", "held@example.test")
    await _invite(pool, client, " held@example.test ", "nbsp")

    assert await _both(pool, "nbsp") == (None, EMAIL_IN_USE)


@pytest.mark.asyncio
async def test_an_address_starting_with_v_is_not_mangled(portal_pg_pool, make_client):
    pool = portal_pg_pool
    await _login(pool, await make_client("Other"), "victor@example.test", pin=REAL_PIN)
    taken = await make_client("Client", "victor@example.test")
    await _invite(pool, taken, " victor@example.test ", "taken-v")
    free = await make_client("Free", "vera@example.test")
    await _invite(pool, free, "\tvera@example.test\n", "free-v")

    assert await _both(pool, "taken-v") == (None, EMAIL_IN_USE)
    validated, registered = await _both(pool, "free-v")
    assert _valid(validated)
    assert registered["email"] == "vera@example.test"
    assert (
        await pool.fetchval("SELECT email FROM team_members WHERE linked_client_id = $1", free)
        == "vera@example.test"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held", "invited"),
    [
        ("holder@example.test", "Holder@Example.TEST"),
        ("Holder@Example.test", "holder@example.test"),
    ],
)
async def test_letter_case_never_hides_a_held_address(portal_pg_pool, make_client, held, invited):
    pool = portal_pg_pool
    await _login(pool, await make_client("Other"), held, pin=REAL_PIN)
    client = await make_client("Client", invited)
    await _invite(pool, client, invited, "case")

    assert await _both(pool, "case") == (None, EMAIL_IN_USE)


@pytest.mark.asyncio
async def test_without_a_linked_login_any_holder_counts(portal_pg_pool, make_client):
    pool = portal_pg_pool
    await _login(pool, None, "staff.member@example.test", role="staff", pin=REAL_PIN)
    client = await make_client("Client", "staff.member@example.test")
    await _invite(pool, client, "staff.member@example.test", "unlinked")

    assert await _both(pool, "unlinked") == (None, EMAIL_IN_USE)


@pytest.mark.asyncio
async def test_a_client_role_login_is_read_before_a_staff_one(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client = await make_client("Client", "portal@example.test")
    await _login(
        pool,
        client,
        "staff.card@example.test",
        role="group_staff",
        pin=REAL_PIN,
        created_at="2026-01-01T00:00:00Z",
    )
    portal = await _login(pool, client, "portal@example.test", created_at="2026-06-01T00:00:00Z")
    await _invite(pool, client, "portal@example.test", "two-linked")

    validated, registered = await _both(pool, "two-linked")

    assert _valid(validated)
    assert registered["user_id"] == portal


@pytest.mark.asyncio
async def test_a_staff_login_alone_is_not_registrable(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client = await make_client("Client", "card@example.test")
    await _login(pool, client, "card@example.test", role="group_staff", pin=REAL_PIN)
    await _invite(pool, client, "card@example.test", "staff")

    assert await _both(pool, "staff") == (None, NOT_REGISTRABLE)


@pytest.mark.asyncio
async def test_two_linked_logins_created_together_resolve_to_the_lowest_id(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client = await make_client("Client", "c@example.test")
    same = "2026-03-01T00:00:00Z"
    await _login(pool, client, "b@example.test", login_id="tm-b", created_at=same)
    await _login(pool, client, "a@example.test", login_id="tm-a", pin=REAL_PIN, created_at=same)
    await _invite(pool, client, "c@example.test", "tie")

    validated, registered = await _both(pool, "tie")

    assert validated == {
        "error": "already_registered",
        "message": "This client already has an active portal account",
    }
    assert registered == validated["message"]


@pytest.mark.asyncio
async def test_the_linked_login_holding_the_address_is_not_another_account(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client = await make_client("Client", "self@example.test")
    login = await _login(pool, client, "self@example.test")
    await _invite(pool, client, "Self@Example.test", "self")

    validated, registered = await _both(pool, "self")

    assert _valid(validated)
    assert registered["user_id"] == login


@pytest.mark.asyncio
async def test_token_state_comes_before_every_refusal(portal_pg_pool, make_client):
    pool = portal_pg_pool
    plain = await make_client("Plain")
    await _invite(pool, plain, "plain@example.test", "used-expired", used=True, expired=True)
    staff = await make_client("Staff")
    await _login(pool, staff, "card@example.test", role="group_staff", pin=REAL_PIN)
    await _invite(pool, staff, "card@example.test", "staff-used", used=True)
    held = await make_client("Held")
    await _login(pool, await make_client("Other"), "held@example.test", pin=REAL_PIN)
    await _invite(pool, held, "held@example.test", "held-expired", expired=True)

    used = {"error": "already_used", "message": "This invitation has already been used"}
    expired = {"error": "expired", "message": "This invitation has expired"}
    assert await _both(pool, "used-expired") == (used, "Invitation already used")
    assert await _both(pool, "staff-used") == (used, "Invitation already used")
    assert await _both(pool, "held-expired") == (expired, "Invitation expired")


@pytest.mark.asyncio
async def test_the_token_forms(portal_pg_pool, make_client):
    pool = portal_pg_pool
    digest_client = await make_client("Digest", "digest@example.test")
    await _invite(pool, digest_client, "digest@example.test", "digest-token")
    legacy_client = await make_client("Legacy", "legacy@example.test")
    await _invite(pool, legacy_client, "legacy@example.test", "legacy-token", raw=True)
    archived = await make_client("Archived", deleted=True)
    await _invite(pool, archived, "archived@example.test", "archived-token")

    validated, registered = await _both(pool, "digest-token")
    assert validated["client_id"] == digest_client and registered["client_id"] == digest_client
    validated, registered = await _both(pool, "legacy-token")
    assert validated["client_id"] == legacy_client and registered["client_id"] == legacy_client
    assert await _both(pool, _hash_token("archived-token")) == (None, INVALID)
    assert await _both(pool, "archived-token") == (None, INVALID)
    assert await _both(pool, "never-issued") == (None, INVALID)


@pytest.mark.asyncio
async def test_a_raw_row_and_a_digest_row_for_one_token_read_the_digest_row(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    legacy_client = await make_client("Legacy", "legacy@example.test")
    await _invite(pool, legacy_client, "legacy@example.test", "shared-token", raw=True)
    digest_client = await make_client("Digest", "digest@example.test")
    await _invite(pool, digest_client, "digest@example.test", "shared-token")

    validated, registered = await _both(pool, "shared-token")

    assert validated["client_id"] == digest_client
    assert registered["client_id"] == digest_client


@pytest.mark.asyncio
async def test_a_blank_address_is_required_at_both_doors(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client = await make_client("Client")
    await _invite(pool, client, "   ", "blank")

    validated, registered = await _both(pool, "blank")

    assert validated == {"error": "expired", "message": "This invitation has expired"}
    assert registered == "Invitation expired"
