"""The portal profile backfill never moves a login to another client (real Postgres)."""

import importlib.util
from pathlib import Path

import pytest

from backend.services.portal.portal_profile_service import PLACEHOLDER_PIN_HASH

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "backfill_portal_profiles.py"
_SPEC = importlib.util.spec_from_file_location("backfill_portal_profiles_under_test", _SCRIPT)
backfill_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill_script)

REGISTERED_HASH = "$2b$12$registeredhashregisteredhashregisteredhashregisteredha"


async def _login(pool, email):
    return await pool.fetchrow(
        "SELECT id, linked_client_id, pin_hash FROM team_members WHERE email = $1", email
    )


@pytest.mark.asyncio
async def test_the_backfill_never_moves_another_client_s_login(portal_pg_pool, make_client):
    pool = portal_pg_pool
    client_a = await make_client("Client A", "a@example.test")
    await pool.execute(
        "INSERT INTO team_members (name, full_name, email, pin_hash, role, linked_client_id,"
        " portal_access, active) VALUES ('Client A', 'Client A', 'shared@example.test', $1,"
        " 'client', $2, true, true)",
        REGISTERED_HASH,
        client_a,
    )
    await make_client("Client B", "shared@example.test")
    client_c = await make_client("Client C", "c@example.test")

    counts = await backfill_script.backfill(pool)

    shared = await _login(pool, "shared@example.test")
    assert shared["linked_client_id"] == client_a
    assert shared["pin_hash"] == REGISTERED_HASH
    fresh = await _login(pool, "c@example.test")
    assert fresh["linked_client_id"] == client_c
    assert fresh["pin_hash"] == PLACEHOLDER_PIN_HASH
    assert counts == {"to_backfill": 2, "created": 1, "skipped": 1}


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(portal_pg_pool, make_client):
    pool = portal_pg_pool
    await make_client("Client C", "c@example.test")

    counts = await backfill_script.backfill(pool, dry_run=True)

    assert counts["to_backfill"] == 1
    assert await pool.fetchval("SELECT count(*) FROM team_members") == 0
