"""Completed-stage automation against a PROD-SHAPED schema (real Postgres).

The bug this guards: ``_fetch_client_data`` selected ``drive_documents_folder_id`` and
``drive_final_folder_id``, columns prod ``clients`` does not have, so ``trigger_on_completed``
died on every Completed (1,012 completed practices, 0 ``process_completed`` activity rows).
A mock cannot see that; a table shaped like prod ``clients`` can. Fixtures are placeholders only.

Requires the mandate-owned disposable PostgreSQL (``NUZ_UNIVERSAL_PG_SYSTEM_ID`` /
``NUZ_UNIVERSAL_PG_DSN``); skipped otherwise.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.crm.completed_process_service import CompletedProcessService

_SCHEMA_SQL = """
CREATE TABLE clients (
    id serial PRIMARY KEY, full_name text, email text, phone text,
    drive_folder_id text, drive_folder_url text
);
CREATE TABLE practice_types (id serial PRIMARY KEY, code text, name text);
CREATE TABLE practices (
    id serial PRIMARY KEY, client_id integer, practice_type_id integer,
    assigned_to text, created_by text, status text
);
CREATE TABLE activity_log (
    id serial PRIMARY KEY, entity_type text, entity_id integer, action text,
    performed_by text, description text
);
"""


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    schema = "completed_" + uuid4().hex
    created = None
    try:
        identity = await admin.fetchrow(
            "SELECT current_database() AS db, system_identifier::text AS system_id "
            "FROM pg_control_system()"
        )
        if dict(identity) != {"db": "universal_db_test", "system_id": expected}:
            pytest.fail("Refusing a database outside this disposable test cluster")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = await asyncpg.create_pool(
            dsn, min_size=1, max_size=3, server_settings={"search_path": schema}
        )
        await created.execute(_SCHEMA_SQL)
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _seed(pool: asyncpg.Pool, *, email: str | None, assigned: str | None) -> int:
    await pool.execute(
        "INSERT INTO practice_types (code, name) VALUES ('CODE_X', 'Service Display Name')"
    )
    client = await pool.fetchval(
        "INSERT INTO clients (full_name, email) VALUES ('Placeholder Client', $1) RETURNING id",
        email,
    )
    return await pool.fetchval(
        "INSERT INTO practices (client_id, practice_type_id, assigned_to, created_by, status)"
        " VALUES ($1, 1, $2, 'creator.placeholder@example.test', 'completed') RETURNING id",
        client,
        assigned,
    )


def _service(pool: asyncpg.Pool) -> CompletedProcessService:
    svc = CompletedProcessService(pool)
    svc._send_with_brevo_fallback = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_completed_writes_the_activity_row_on_a_prod_shaped_schema(
    pool: asyncpg.Pool,
) -> None:
    practice = await _seed(pool, email="client.placeholder@example.test", assigned="c@example.test")
    svc = _service(pool)
    result = await svc.trigger_on_completed(practice, "staff@example.test")
    assert result == {"success": True, "team_notified": False}
    rows = await pool.fetch("SELECT entity_type, entity_id, action, performed_by FROM activity_log")
    assert [tuple(r) for r in rows] == [
        ("practice", practice, "process_completed", "staff@example.test")
    ]
    svc._send_with_brevo_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_without_email_gets_the_team_mail_and_the_row(pool: asyncpg.Pool) -> None:
    practice = await _seed(pool, email=None, assigned="consultant.placeholder@example.test")
    svc = _service(pool)
    result = await svc.trigger_on_completed(practice, "staff@example.test")
    assert result == {"success": True, "team_notified": True}
    (call,) = svc._send_with_brevo_fallback.await_args_list
    assert call.args[0] == "consultant.placeholder@example.test"
    assert "Service Display Name" in call.args[1]
    assert await pool.fetchval("SELECT count(*) FROM activity_log") == 1


@pytest.mark.asyncio
async def test_one_row_per_completion_even_when_the_mail_fails(pool: asyncpg.Pool) -> None:
    practice = await _seed(pool, email=None, assigned="consultant.placeholder@example.test")
    svc = _service(pool)
    svc._send_with_brevo_fallback.side_effect = RuntimeError("Brevo down")
    result = await svc.trigger_on_completed(practice, "staff@example.test")
    assert result == {"success": True, "team_notified": False}
    assert await pool.fetchval("SELECT count(*) FROM activity_log") == 1
