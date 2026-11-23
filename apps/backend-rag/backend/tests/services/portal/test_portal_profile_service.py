"""Tests for PortalProfileService — auto-creates team_members records for clients."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = cm
    return pool, conn


@pytest.mark.asyncio
async def test_ensure_portal_profile_creates_record(mock_pool):
    """When client has email, should insert team_members record."""
    pool, conn = mock_pool
    conn.fetchval.return_value = "generated-uuid-123"

    from backend.services.portal.portal_profile_service import PortalProfileService

    service = PortalProfileService(pool)
    result = await service.ensure_portal_profile(
        client_id=42,
        email="test@example.com",
        full_name="Test User",
    )

    assert result == "generated-uuid-123"
    conn.fetchval.assert_called_once()
    sql_call = conn.fetchval.call_args[0][0]
    assert "INSERT INTO team_members" in sql_call
    assert "ON CONFLICT" in sql_call


@pytest.mark.asyncio
async def test_ensure_portal_profile_skips_without_email(mock_pool):
    """When client has no email, should return None and not insert."""
    pool, conn = mock_pool

    from backend.services.portal.portal_profile_service import PortalProfileService

    service = PortalProfileService(pool)
    result = await service.ensure_portal_profile(
        client_id=42,
        email=None,
        full_name="Test User",
    )

    assert result is None
    conn.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_portal_profile_skips_empty_email(mock_pool):
    """When client has empty string email, should return None."""
    pool, conn = mock_pool

    from backend.services.portal.portal_profile_service import PortalProfileService

    service = PortalProfileService(pool)
    result = await service.ensure_portal_profile(
        client_id=42,
        email="  ",
        full_name="Test User",
    )

    assert result is None
    conn.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_portal_profile_handles_db_error(mock_pool):
    """DB errors should be caught and logged, not raised."""
    pool, conn = mock_pool
    conn.fetchval.side_effect = Exception("connection lost")

    from backend.services.portal.portal_profile_service import PortalProfileService

    service = PortalProfileService(pool)
    result = await service.ensure_portal_profile(
        client_id=42,
        email="test@example.com",
        full_name="Test User",
    )

    assert result is None


# --- real Postgres: an e-mail conflict must never re-link a registered account ---

from backend.services.portal.portal_profile_service import (  # noqa: E402
    PLACEHOLDER_PIN_HASH,
    PortalProfileService,
)

REGISTERED_HASH = "$2b$12$registeredhashregisteredhashregisteredhashregisteredha"


async def _member(pool, email):
    return await pool.fetchrow(
        "SELECT id, linked_client_id, pin_hash, name, full_name, portal_access"
        " FROM team_members WHERE email = $1",
        email,
    )


async def _registered(pool, email, client_id):
    await pool.execute(
        "INSERT INTO team_members (name, full_name, email, pin_hash, role, linked_client_id,"
        " portal_access, active) VALUES ('Client A', 'Client A', $1, $2, 'client', $3, true, true)",
        email,
        REGISTERED_HASH,
        client_id,
    )


@pytest.mark.asyncio
async def test_registered_account_of_another_client_is_never_relinked(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    client_b = await make_client("Client B")
    await _registered(pool, "shared@example.test", client_a)
    before = await _member(pool, "shared@example.test")

    result = await PortalProfileService(pool).ensure_portal_profile(
        client_b, "shared@example.test", "Client B"
    )

    assert result is None
    after = await _member(pool, "shared@example.test")
    assert dict(after) == dict(before)
    assert after["linked_client_id"] == client_a
    assert after["pin_hash"] == REGISTERED_HASH


@pytest.mark.asyncio
async def test_refusal_logs_ids_only(portal_pg_pool, make_client, caplog):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    client_b = await make_client("Client B")
    await _registered(pool, "shared@example.test", client_a)
    with caplog.at_level("WARNING"):
        await PortalProfileService(pool).ensure_portal_profile(
            client_b, "shared@example.test", "Client B"
        )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert str(client_b) in text
    assert "shared@example.test" not in text
    assert "Client B" not in text


@pytest.mark.asyncio
async def test_a_never_registered_row_of_another_client_is_not_moved(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    client_b = await make_client("Client B")
    service = PortalProfileService(pool)
    await service.ensure_portal_profile(client_a, "old@example.test", "Client A")

    member_id = await service.ensure_portal_profile(client_b, "old@example.test", "Client B")

    row = await _member(pool, "old@example.test")
    assert member_id is None
    assert row["linked_client_id"] == client_a
    assert row["pin_hash"] == PLACEHOLDER_PIN_HASH


@pytest.mark.asyncio
async def test_a_never_registered_row_linked_to_no_client_is_linked(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_b = await make_client("Client B")
    await pool.execute(
        "INSERT INTO team_members (name, full_name, email, pin_hash, role, linked_client_id,"
        " portal_access, active) VALUES ('Gone', 'Gone', 'old@example.test', $1, 'client',"
        " NULL, true, true)",
        PLACEHOLDER_PIN_HASH,
    )

    member_id = await PortalProfileService(pool).ensure_portal_profile(
        client_b, "old@example.test", "Client B"
    )

    row = await _member(pool, "old@example.test")
    assert member_id == row["id"]
    assert row["linked_client_id"] == client_b


@pytest.mark.asyncio
async def test_a_registered_row_linked_to_no_client_is_not_linked(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_b = await make_client("Client B")
    await pool.execute(
        "INSERT INTO team_members (name, full_name, email, pin_hash, role, linked_client_id,"
        " portal_access, active) VALUES ('Gone', 'Gone', 'old@example.test', $1, 'client',"
        " NULL, true, true)",
        REGISTERED_HASH,
    )

    assert (
        await PortalProfileService(pool).ensure_portal_profile(
            client_b, "old@example.test", "Client B"
        )
        is None
    )
    assert (await _member(pool, "old@example.test"))["linked_client_id"] is None


@pytest.mark.asyncio
async def test_an_e_mail_link_minted_or_used_for_one_client_never_opens_another(
    portal_pg_pool, make_client
):
    from backend.services.portal.magic_link_service import MagicLinkService

    pool = portal_pg_pool
    client_a = await make_client("Client A")
    client_b = await make_client("Client B")
    service = PortalProfileService(pool)
    await service.ensure_portal_profile(client_a, "old@example.test", "Client A")
    links = MagicLinkService(pool)
    used = await links.request_magic_link("old@example.test")
    assert (await links.verify_magic_link(used["token"]))["client_id"] == client_a
    minted = await links.request_magic_link("old@example.test")

    assert await service.ensure_portal_profile(client_b, "old@example.test", "Client B") is None

    assert (await _member(pool, "old@example.test"))["linked_client_id"] == client_a
    assert (await links.verify_magic_link(minted["token"]))["client_id"] == client_a


@pytest.mark.asyncio
async def test_same_client_is_idempotent_and_keeps_registered_pin(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    service = PortalProfileService(pool)
    first = await service.ensure_portal_profile(client_a, "a@example.test", "Client A")
    await pool.execute(
        "UPDATE team_members SET pin_hash = $1 WHERE email = 'a@example.test'", REGISTERED_HASH
    )

    second = await service.ensure_portal_profile(client_a, "A@Example.test", "Client A")

    assert second == first
    row = await _member(pool, "a@example.test")
    assert row["linked_client_id"] == client_a
    assert row["pin_hash"] == REGISTERED_HASH
    assert await pool.fetchval("SELECT count(*) FROM team_members") == 1


@pytest.mark.asyncio
async def test_a_mixed_case_login_of_the_same_client_is_reused_as_stored(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    await _registered(pool, "Mixed.Case@Example.test", client_a)
    before = await _member(pool, "Mixed.Case@Example.test")

    member_id = await PortalProfileService(pool).ensure_portal_profile(
        client_a, "mixed.case@example.test", "Client A"
    )

    assert member_id == before["id"]
    assert await pool.fetchval("SELECT count(*) FROM team_members") == 1
    after = await _member(pool, "Mixed.Case@Example.test")
    assert after["pin_hash"] == REGISTERED_HASH


@pytest.mark.asyncio
async def test_a_mixed_case_placeholder_of_the_same_client_is_updated_in_place(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    await pool.execute(
        "INSERT INTO team_members (name, full_name, email, pin_hash, role, linked_client_id,"
        " portal_access, active) VALUES ('Client A', 'Client A', 'Mixed.Case@Example.test',"
        " $1, 'client', $2, false, true)",
        PLACEHOLDER_PIN_HASH,
        client_a,
    )

    member_id = await PortalProfileService(pool).ensure_portal_profile(
        client_a, "mixed.case@example.test", "Client A"
    )

    row = await _member(pool, "Mixed.Case@Example.test")
    assert member_id == row["id"]
    assert row["portal_access"] is True
    assert await pool.fetchval("SELECT count(*) FROM team_members") == 1


@pytest.mark.asyncio
async def test_a_mixed_case_login_of_another_client_is_refused(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    client_b = await make_client("Client B")
    await _registered(pool, "Mixed.Case@Example.test", client_a)
    before = await _member(pool, "Mixed.Case@Example.test")

    result = await PortalProfileService(pool).ensure_portal_profile(
        client_b, "mixed.case@example.test", "Client B"
    )

    assert result is None
    assert await pool.fetchval("SELECT count(*) FROM team_members") == 1
    assert dict(await _member(pool, "Mixed.Case@Example.test")) == dict(before)


@pytest.mark.asyncio
async def test_new_address_creates_the_row(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")

    member_id = await PortalProfileService(pool).ensure_portal_profile(
        client_a, "new@example.test", "Client A"
    )

    row = await _member(pool, "new@example.test")
    assert member_id == row["id"]
    assert row["linked_client_id"] == client_a
    assert row["portal_access"] is True
