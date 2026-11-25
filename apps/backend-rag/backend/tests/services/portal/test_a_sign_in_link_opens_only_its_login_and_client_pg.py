"""A sign-in link opens only the login and the client it was minted for (real PostgreSQL).

The raw token is `<login id>~<client id>~<secret>`, both ids as unpadded base64url, and the
stored hash covers a fixed prefix plus the whole string. Every expected digest here is
computed with hashlib, never through the service's own helper. Invented rows only.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.portal.invite_service import InviteService
from backend.services.portal.magic_link_service import MagicLinkService
from backend.services.portal.portal_profile_service import PortalProfileService

OLD = "old@example.test"
NEW = "new@example.test"


def _digest(raw: str) -> str:
    return hashlib.sha256(b"portal-magic-v2\x00" + raw.encode("utf-8")).hexdigest()


def _bare_digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _part(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")


def _unpart(part: str) -> str:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8")


async def _used_at(pool, token: str):
    return await pool.fetchval(
        "SELECT used_at FROM magic_link_tokens WHERE token_hash = $1", _digest(token)
    )


async def _redeem(pool, client: int, email: str) -> None:
    # An invitation goes only to the address on the client record, so the
    # address moves on the record first, as staff move it.
    await pool.execute("UPDATE clients SET email = $2 WHERE id = $1", client, email)
    service = InviteService(pool)
    invite = await service.create_invitation(client, email, "staff@example.test")
    await service.complete_registration(invite["token"], "123456")


async def _moved_and_retaken(pool, make_client):
    """A at old@ gets link L; A's address moves to new@; B is provisioned at old@."""
    client_a = await make_client("Client A")
    client_b = await make_client("Client B")
    profiles = PortalProfileService(pool)
    links = MagicLinkService(pool)
    login_a = await profiles.ensure_portal_profile(client_a, OLD, "Client A")
    token = (await links.request_magic_link(OLD))["token"]
    await _redeem(pool, client_a, NEW)
    login_b = await profiles.ensure_portal_profile(client_b, OLD, "Client B")
    assert login_b is not None and login_b != login_a
    return links, token, (login_a, client_a), (login_b, client_b)


# 1 ---------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_link_never_opens_the_login_that_took_its_address(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    client_b = await make_client("Client B")
    profiles = PortalProfileService(pool)
    links = MagicLinkService(pool)
    login_a = await profiles.ensure_portal_profile(client_a, OLD, "Client A")

    token = (await links.request_magic_link(OLD))["token"]

    assert token and token.count("~") == 2
    login_part, client_part, _secret = token.split("~")
    assert _unpart(login_part) == str(login_a)
    assert _unpart(client_part) == str(client_a)
    stored = await pool.fetchval("SELECT token_hash FROM magic_link_tokens")
    assert stored == _digest(token)
    assert stored != _bare_digest(token)

    await _redeem(pool, client_a, NEW)
    login_b = await profiles.ensure_portal_profile(client_b, OLD, "Client B")

    assert await links.verify_magic_link(token) is None
    opened = await links.verify_magic_link((await links.request_magic_link(NEW))["token"])
    assert (opened["id"], opened["client_id"]) == (str(login_a), client_a)
    opened = await links.verify_magic_link((await links.request_magic_link(OLD))["token"])
    assert (opened["id"], opened["client_id"]) == (str(login_b), client_b)


@pytest.mark.asyncio
async def test_a_link_never_opens_another_login_of_its_client_at_its_address(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    profiles = PortalProfileService(pool)
    links = MagicLinkService(pool)
    login_x = await profiles.ensure_portal_profile(client_a, OLD, "Client A")
    token = (await links.request_magic_link(OLD))["token"]
    await _redeem(pool, client_a, NEW)

    login_y = await profiles.ensure_portal_profile(client_a, OLD, "Client A")

    assert login_y is not None and login_y != login_x
    assert await links.verify_magic_link(token) is None


# 2 ---------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_link_whose_login_left_the_address_opens_nothing_and_is_spent(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    await PortalProfileService(pool).ensure_portal_profile(client_a, OLD, "Client A")
    links = MagicLinkService(pool)
    token = (await links.request_magic_link(OLD))["token"]

    await _redeem(pool, client_a, NEW)

    assert await links.verify_magic_link(token) is None
    assert await _used_at(pool, token) is not None


# 3 ---------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_link_with_another_login_or_client_in_it_opens_nothing(portal_pg_pool, make_client):
    pool = portal_pg_pool
    links, token, (login_a, client_a), (login_b, client_b) = await _moved_and_retaken(
        pool, make_client
    )
    _login, _client, secret = token.split("~")

    # a. both of B's ids around L's secret: B holds the address L was sent to.
    assert (
        await links.verify_magic_link(f"{_part(login_b)}~{_part(str(client_b))}~{secret}") is None
    )
    # b. B's login, A's client.
    assert (
        await links.verify_magic_link(f"{_part(login_b)}~{_part(str(client_a))}~{secret}") is None
    )
    # c. A's login, B's client.
    assert (
        await links.verify_magic_link(f"{_part(login_a)}~{_part(str(client_b))}~{secret}") is None
    )
    # d. a link minted for B, with A's ids put in.
    for_b = (await links.request_magic_link(OLD))["token"]
    b_secret = for_b.split("~")[2]
    assert (
        await links.verify_magic_link(f"{_part(login_a)}~{_part(str(client_a))}~{b_secret}") is None
    )
    # Nothing above found a row, so nothing was spent.
    assert await _used_at(pool, token) is None
    assert await _used_at(pool, for_b) is None


# 4 ---------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_token_of_the_old_shape_opens_nothing(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    await PortalProfileService(pool).ensure_portal_profile(client_a, OLD, "Client A")
    links = MagicLinkService(pool)
    legacy = secrets.token_urlsafe(32)
    assert "~" not in legacy
    for stored in (_bare_digest(legacy), _digest(legacy)):
        await pool.execute(
            "INSERT INTO magic_link_tokens (email, token_hash, expires_at)"
            " VALUES ($1, $2, NOW() + interval '10 minutes')",
            OLD,
            stored,
        )

    assert await links.verify_magic_link(legacy) is None

    assert await pool.fetchval("SELECT count(*) FROM magic_link_tokens WHERE used_at IS NULL") == 2


# 5 ---------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_provisioning_the_same_client_again_keeps_its_link(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    profiles = PortalProfileService(pool)
    login_a = await profiles.ensure_portal_profile(client_a, OLD, "Client A")
    links = MagicLinkService(pool)
    token = (await links.request_magic_link(OLD))["token"]

    assert await profiles.ensure_portal_profile(client_a, OLD, "Client A") == login_a

    opened = await links.verify_magic_link(token)
    assert (opened["id"], opened["client_id"]) == (str(login_a), client_a)


# 6 ---------------------------------------------------------------------------------------
_LOGIN = _part("tm-0001")
_CLIENT = _part("42")
_MALFORMED = [
    None,
    123,
    b"bytes",
    "",
    "no-separator-at-all",
    f"{_LOGIN}~{_CLIENT}",
    f"{_LOGIN}~{_CLIENT}~secret~more",
    f"~{_CLIENT}~secret",
    f"{_LOGIN}~~secret",
    f"{_LOGIN}~{_CLIENT}~",
    f"{_part('tm-1')}==~{_CLIENT}~secret",
    f"dG0/MQ~{_CLIENT}~secret",
    f"{_LOGIN}~{_CLIENT}~sec+ret",
    f"QR~{_CLIENT}~secret",
    f"A~{_CLIENT}~secret",
    f"_w~{_CLIENT}~secret",
    f"{_part('a' + chr(0) + 'b')}~{_CLIENT}~secret",
    f"{_LOGIN}~{_part('0')}~secret",
    f"{_LOGIN}~{_part('007')}~secret",
    f"{_LOGIN}~{_part('-1')}~secret",
    f"{_LOGIN}~{_part('12a')}~secret",
    f"{_LOGIN}~{_part('2147483648')}~secret",
    f"{_LOGIN} ~{_CLIENT}~secret",
    f"{_LOGIN}~{_CLIENT}~sec ret",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("token", _MALFORMED)
async def test_a_malformed_token_is_refused_before_the_database(token):
    pool = MagicMock()
    pool.acquire.side_effect = AssertionError("the pool was acquired for a malformed token")

    assert await MagicLinkService(pool).verify_magic_link(token) is None


@pytest.mark.asyncio
async def test_a_well_formed_token_is_parsed_and_hashed_as_one_stripped_string():
    token = f"{_LOGIN}~{_CLIENT}~secret"
    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    conn.fetchrow = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    assert await MagicLinkService(pool).verify_magic_link(f"  {token}\n") is None

    assert conn.fetchrow.await_args.args[1] == _digest(token)


# 8 ---------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_link_whose_login_was_moved_to_another_client_opens_nothing(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    client_b = await make_client("Client B")
    login_x = await PortalProfileService(pool).ensure_portal_profile(client_a, OLD, "Client A")
    links = MagicLinkService(pool)
    token = (await links.request_magic_link(OLD))["token"]

    # What an older provisioning or backfill could do: move the login to another client.
    await pool.execute(
        "UPDATE team_members SET linked_client_id = $1 WHERE id = $2", client_b, login_x
    )

    # Before L is spent: X's login, B's client, L's secret.
    secret = token.split("~")[2]
    assert (
        await links.verify_magic_link(f"{_part(login_x)}~{_part(str(client_b))}~{secret}") is None
    )
    assert await _used_at(pool, token) is None

    assert await links.verify_magic_link(token) is None
    assert await _used_at(pool, token) is not None


# 9 ---------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_lookup_by_the_bare_hash_finds_no_new_token(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A")
    await PortalProfileService(pool).ensure_portal_profile(client_a, OLD, "Client A")
    token = (await MagicLinkService(pool).request_magic_link(OLD))["token"]

    # The lookup the code before this change makes.
    assert (
        await pool.fetchrow(
            "SELECT id FROM magic_link_tokens WHERE token_hash = $1", _bare_digest(token)
        )
        is None
    )
    assert await pool.fetchval("SELECT count(*) FROM magic_link_tokens") == 1
