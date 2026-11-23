"""A view-as session reads the notification preferences of the client's own login (real PostgreSQL).

A superuser viewing the portal as a client used to get the preferences of whichever
linked client-role row had the lowest id: a portal-disabled account, or a row of the
viewer's own. Invented rows only.
"""

from __future__ import annotations

import pytest

from backend.app.routers.portal_notification_prefs import get_prefs


async def _login(pool, client: int, email: str, *, created: str, portal: bool = True) -> str:
    return await pool.fetchval(
        "INSERT INTO team_members (name, full_name, email, pin_hash, role, linked_client_id,"
        " portal_access, active, created_at)"
        " VALUES ('Login', 'Login', $1, 'x', 'client', $2, $3, true, $4::text::timestamptz)"
        " RETURNING id",
        email,
        client,
        portal,
        created,
    )


async def _prefs(pool, user_id: str, phone: str) -> None:
    await pool.execute(
        "INSERT INTO notification_prefs (user_id, email_enabled, wa_enabled, wa_phone)"
        " VALUES ($1, true, true, $2)",
        user_id,
        phone,
    )


@pytest.mark.asyncio
async def test_a_view_as_session_reads_the_clients_own_login(portal_pg_pool, make_client):
    pool = portal_pg_pool
    await pool.execute(
        "CREATE TABLE notification_prefs (user_id TEXT PRIMARY KEY, email_enabled BOOLEAN,"
        " wa_enabled BOOLEAN, wa_phone TEXT)"
    )
    client = await make_client("Client")
    viewer = await _login(pool, client, "viewer@example.test", created="2025-01-01T00:00:00Z")
    disabled = await _login(
        pool, client, "old@example.test", created="2025-06-01T00:00:00Z", portal=False
    )
    own = await _login(pool, client, "own@example.test", created="2026-01-01T00:00:00Z")
    await _prefs(pool, viewer, "+620000000001")
    await _prefs(pool, disabled, "+620000000002")
    await _prefs(pool, own, "+620000000003")

    seen = await get_prefs(
        client={"client_id": client, "user_id": viewer, "impersonating": True}, pool=pool
    )

    assert seen["wa_phone"] == "+620000000003"
