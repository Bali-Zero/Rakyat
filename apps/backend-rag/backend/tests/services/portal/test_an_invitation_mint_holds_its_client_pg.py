"""An invitation mint holds its client, and only an address on record redeems (real PostgreSQL).

The mint locks the client row for its whole transaction and waits on no invitation row; both
doors answer an invitation whose address is no longer the client's as expired. A wait is
observed in pg_stat_activity, never assumed from a sleep. Invented rows only.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest

from backend.services.portal.invite_service import (
    InvitationAddressNotOnRecord,
    InviteService,
    _hash_token,
)

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://test:test@localhost:5433/test")
#: The mint's retirement statement; a paused mint stops just before it, holding the client.
_RETIRE = "UPDATE client_invitations SET expires_at = NOW()"
OLD = "old@example.test"
CURRENT = "current@example.test"
EXPIRED = {"error": "expired", "message": "This invitation has expired"}


class _Connection:
    """A real connection that can stop before one named statement until released."""

    def __init__(self, conn: Any, pool: _Pool) -> None:
        self._conn = conn
        self._pool = pool

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def execute(self, query: str, *args: Any) -> str:
        if self._pool.pause_before and self._pool.pause_before in query:
            self._pool.reached.set()
            await self._pool.release.wait()
        return await self._conn.execute(query, *args)


class _Pool:
    """Hands InviteService one connection, records its backend pid, and can pause it."""

    def __init__(self, pool: asyncpg.Pool, pause_before: str | None = None) -> None:
        self._pool = pool
        self.pause_before = pause_before
        self.acquired = asyncio.Event()
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.pid: int | None = None

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[_Connection]:
        async with self._pool.acquire() as conn:
            self.pid = await conn.fetchval("SELECT pg_backend_pid()")
            self.acquired.set()
            yield _Connection(conn, self)


@asynccontextmanager
async def _session(pool: asyncpg.Pool) -> AsyncGenerator[asyncpg.Connection]:
    """A connection outside the pool, on the test's own schema."""
    schema = await pool.fetchval("SELECT current_schema()")
    conn = await asyncpg.connect(_DSN, server_settings={"search_path": schema})
    try:
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def _released(*paused: _Pool) -> AsyncGenerator[None]:
    """Lets every paused mint go when the case ends, so a failing case cannot hang teardown."""
    try:
        yield
    finally:
        for pool in paused:
            pool.release.set()


async def _waits_on_a_lock(
    probe: asyncpg.Connection, pid: int | None, running: asyncio.Task
) -> bool:
    """True once the backend is seen waiting on a lock; False if it finishes first or 10 s pass."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while loop.time() < deadline and not running.done():
        if await probe.fetchval(
            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", pid
        ):
            return True
        await asyncio.sleep(0.02)
    return False


async def _invitation(pool: asyncpg.Pool, client_id: int, email: str, token: str) -> int:
    return await pool.fetchval(
        "INSERT INTO client_invitations (client_id, email, token, expires_at, created_by) "
        "VALUES ($1, $2, $3, $4, 'staff') RETURNING id",
        client_id,
        email,
        _hash_token(token),
        datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def _live(pool: asyncpg.Pool, invitation_id: int) -> bool:
    return await pool.fetchval(
        "SELECT used_at IS NULL AND expires_at > NOW() FROM client_invitations WHERE id = $1",
        invitation_id,
    )


async def _rows_for(pool: asyncpg.Pool, email: str) -> int:
    return await pool.fetchval("SELECT count(*) FROM client_invitations WHERE email = $1", email)


async def _logins(pool: asyncpg.Pool, client_id: int) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM team_members WHERE linked_client_id = $1", client_id
    )


async def _mint(pool: Any, client_id: int, email: str) -> dict[str, Any]:
    return await InviteService(pool).create_invitation(client_id, email, "staff")


@pytest.mark.asyncio
async def test_an_address_edited_first_is_the_one_the_mint_compares(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_id = await make_client("Synthetic Client", OLD)
    other = await _invitation(pool, client_id, "other@example.test", "other-token")
    minting = _Pool(pool)

    async with _session(pool) as edit, _session(pool) as probe:
        edit_tx = edit.transaction()
        await edit_tx.start()
        await edit.execute("UPDATE clients SET email = $2 WHERE id = $1", client_id, CURRENT)
        mint = asyncio.create_task(_mint(minting, client_id, OLD))
        await asyncio.wait_for(minting.acquired.wait(), 10)
        assert await _waits_on_a_lock(probe, minting.pid, mint)
        await edit_tx.commit()
        with pytest.raises(InvitationAddressNotOnRecord):
            await mint

    assert await _rows_for(pool, OLD) == 0
    assert await _live(pool, other)


@pytest.mark.asyncio
async def test_an_edit_rolled_back_leaves_the_mint_its_address(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_id = await make_client("Synthetic Client", OLD)
    earlier = await _invitation(pool, client_id, OLD, "earlier-token")
    minting = _Pool(pool)

    async with _session(pool) as edit, _session(pool) as probe:
        edit_tx = edit.transaction()
        await edit_tx.start()
        await edit.execute("UPDATE clients SET email = $2 WHERE id = $1", client_id, CURRENT)
        mint = asyncio.create_task(_mint(minting, client_id, OLD))
        await asyncio.wait_for(minting.acquired.wait(), 10)
        assert await _waits_on_a_lock(probe, minting.pid, mint)
        await edit_tx.rollback()
        minted = await mint

    assert minted["email"] == OLD
    assert await _rows_for(pool, OLD) == 2
    assert await _live(pool, earlier)


@pytest.mark.asyncio
async def test_a_mint_holds_the_client_until_it_commits(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_id = await make_client("Synthetic Client", CURRENT)
    minting = _Pool(pool, pause_before=_RETIRE)

    async with _released(minting), _session(pool) as edit, _session(pool) as probe:
        mint = asyncio.create_task(_mint(minting, client_id, CURRENT))
        await asyncio.wait_for(minting.reached.wait(), 10)
        edit_pid = await edit.fetchval("SELECT pg_backend_pid()")
        edit_tx = edit.transaction()
        await edit_tx.start()
        update = asyncio.create_task(
            edit.execute(
                "UPDATE clients SET email = $2 WHERE id = $1", client_id, "next@example.test"
            )
        )
        assert await _waits_on_a_lock(probe, edit_pid, update)
        assert not update.done()
        minting.release.set()
        minted = await mint
        await update
        await edit_tx.commit()

    assert minted["email"] == CURRENT
    assert await pool.fetchval("SELECT email FROM clients WHERE id = $1", client_id) == (
        "next@example.test"
    )


@pytest.mark.asyncio
async def test_a_redemption_in_flight_and_a_mint_never_deadlock(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_id = await make_client("Synthetic Client", CURRENT)
    stale = await _invitation(pool, client_id, OLD, "stale-token")
    minting = _Pool(pool, pause_before=_RETIRE)
    redeeming = _Pool(pool)

    async with _released(minting), _session(pool) as probe:
        mint = asyncio.create_task(_mint(minting, client_id, CURRENT))
        await asyncio.wait_for(minting.reached.wait(), 10)
        redeem = asyncio.create_task(
            InviteService(redeeming).complete_registration("stale-token", "123456")
        )
        await asyncio.wait_for(redeeming.acquired.wait(), 10)
        assert await _waits_on_a_lock(probe, redeeming.pid, redeem)
        minting.release.set()
        minted, refused = await asyncio.gather(mint, redeem, return_exceptions=True)

    assert not isinstance(minted, BaseException), repr(minted)
    assert minted["email"] == CURRENT
    assert isinstance(refused, ValueError), repr(refused)
    assert str(refused) == "Invitation expired"
    # The mint skipped the row the redemption held; it stays as it was, and unredeemable.
    assert await _live(pool, stale)
    assert await _logins(pool, client_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [CURRENT, None, "   "], ids=["changed", "none", "blank"])
async def test_a_stale_invitation_is_expired_at_both_doors(portal_pg_pool, make_client, stored):
    pool = portal_pg_pool
    client_id = await make_client("Synthetic Client", stored)
    stale = await _invitation(pool, client_id, OLD, "stale-token")
    service = InviteService(pool)

    assert await service.validate_token("stale-token") == EXPIRED
    with pytest.raises(ValueError, match="Invitation expired"):
        await service.complete_registration("stale-token", "123456")
    assert await _logins(pool, client_id) == 0
    assert await _live(pool, stale)


@pytest.mark.asyncio
async def test_a_contending_mint_for_the_stale_address_rolls_back_and_redeems_nothing(
    portal_pg_pool, make_client
):
    pool = portal_pg_pool
    client_id = await make_client("Synthetic Client", CURRENT)
    stale = await _invitation(pool, client_id, OLD, "stale-token")
    first = _Pool(pool, pause_before=_RETIRE)
    second = _Pool(pool)

    async with _released(first), _session(pool) as holder, _session(pool) as probe:
        # Held as a redemption would hold it, so the first mint's retirement skips it.
        holder_tx = holder.transaction()
        await holder_tx.start()
        await holder.execute("SELECT id FROM client_invitations WHERE id = $1 FOR UPDATE", stale)
        first_mint = asyncio.create_task(_mint(first, client_id, CURRENT))
        await asyncio.wait_for(first.reached.wait(), 10)
        second_mint = asyncio.create_task(_mint(second, client_id, OLD))
        await asyncio.wait_for(second.acquired.wait(), 10)
        assert await _waits_on_a_lock(probe, second.pid, second_mint)
        first.release.set()
        await first_mint
        with pytest.raises(InvitationAddressNotOnRecord):
            await second_mint
        await holder_tx.rollback()

    assert await _rows_for(pool, OLD) == 1
    assert await _live(pool, stale)
    service = InviteService(pool)
    assert await service.validate_token("stale-token") == EXPIRED
    with pytest.raises(ValueError, match="Invitation expired"):
        await service.complete_registration("stale-token", "123456")
    assert await _logins(pool, client_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "invited", "redeemable"),
    [
        ("Client@Example.TEST", "client@example.test", True),
        (" client@example.test ", "client@example.test", True),
        (" Client@BÜCHER.example ", "client@xn--bcher-kva.example", True),
        ("client@example.test", "client+other@example.test", False),
    ],
    ids=["case", "unicode-space", "idna", "plus-address"],
)
async def test_only_a_spelling_of_the_address_on_record_is_redeemable(
    portal_pg_pool, make_client, stored, invited, redeemable
):
    pool = portal_pg_pool
    client_id = await make_client("Synthetic Client", stored)
    await _invitation(pool, client_id, invited, "spelling-token")
    service = InviteService(pool)

    validated = await service.validate_token("spelling-token")
    if redeemable:
        assert validated["valid"] is True
        registered = await service.complete_registration("spelling-token", "123456")
        assert registered["client_id"] == client_id
    else:
        assert validated == EXPIRED
        with pytest.raises(ValueError, match="Invitation expired"):
            await service.complete_registration("spelling-token", "123456")
        assert await _logins(pool, client_id) == 0


@pytest.mark.asyncio
async def test_a_mint_that_fails_to_insert_retires_nothing(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_id = await make_client("Synthetic Client", CURRENT)
    other = await _invitation(pool, client_id, OLD, "other-token")

    with (
        patch("backend.services.portal.invite_service._hash_token", return_value=None),
        pytest.raises(asyncpg.NotNullViolationError),
    ):
        await _mint(pool, client_id, CURRENT)

    assert await _live(pool, other)
    assert await pool.fetchval("SELECT count(*) FROM client_invitations") == 1
