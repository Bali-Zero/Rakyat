"""Real PostgreSQL coverage for the team deadlines look-ahead interval."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.rag.agentic.team_crm_tools import TeamMyDeadlinesTool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL is not configured", allow_module_level=True)


CALLER_PROFILE = {"role": "team", "email": "deadlines@example.test"}


@pytest_asyncio.fixture
async def deadline_pool(monkeypatch):
    schema = f"team_deadlines_{uuid4().hex}"
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(
        f'SET search_path TO "{schema}";'
        "CREATE TABLE client_expiry_alerts_view ("
        "client_name TEXT, document_type TEXT, expiry_date DATE, "
        "days_until_expiry INTEGER, assigned_to TEXT);"
        "CREATE TABLE clients (id INTEGER PRIMARY KEY, full_name TEXT, assigned_to TEXT);"
        "CREATE TABLE compliance_alerts ("
        "client_id INTEGER, category TEXT, deadline DATE, days_until INTEGER, status TEXT);"
    )
    pool = await asyncpg.create_pool(
        TEST_DATABASE_URL,
        min_size=1,
        max_size=1,
        server_settings={"search_path": schema},
    )
    monkeypatch.setenv("WA_TEAM_CRM_TOOLS_ENABLED", "true")
    try:
        yield pool, conn
    finally:
        await pool.close()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


@pytest.mark.asyncio
async def test_team_my_deadlines_returns_expiry_and_compliance_rows(deadline_pool):
    pool, conn = deadline_pool
    await conn.execute(
        "INSERT INTO client_expiry_alerts_view VALUES "
        "('Synthetic Expiry', 'visa', CURRENT_DATE + 10, 10, 'deadlines@example.test')"
    )
    await conn.execute(
        "INSERT INTO clients VALUES (1, 'Synthetic Compliance', 'deadlines@example.test');"
        "INSERT INTO compliance_alerts VALUES "
        "(1, 'tax', CURRENT_DATE + 10, 10, 'open')"
    )

    answer = json.loads(
        await TeamMyDeadlinesTool(pool).execute(
            days_ahead=30, _caller_profile=CALLER_PROFILE
        )
    )

    assert {deadline["client_name"] for deadline in answer["deadlines"]} == {
        "Synthetic Expiry",
        "Synthetic Compliance",
    }


@pytest.mark.asyncio
async def test_team_my_deadlines_respects_look_ahead_window(deadline_pool):
    pool, conn = deadline_pool
    await conn.execute(
        "INSERT INTO client_expiry_alerts_view VALUES "
        "('Synthetic Later', 'passport', CURRENT_DATE + 200, 200, 'deadlines@example.test')"
    )

    tool = TeamMyDeadlinesTool(pool)
    within_thirty_days = json.loads(
        await tool.execute(days_ahead=30, _caller_profile=CALLER_PROFILE)
    )
    within_year = json.loads(
        await tool.execute(days_ahead=365, _caller_profile=CALLER_PROFILE)
    )

    assert within_thirty_days["deadlines"] == []
    assert [deadline["client_name"] for deadline in within_year["deadlines"]] == [
        "Synthetic Later"
    ]
