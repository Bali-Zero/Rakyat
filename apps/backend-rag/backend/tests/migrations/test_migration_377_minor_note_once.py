"""Migration 377: check_minor_client() keeps ONE current [MINORE] line at the top of the notes.

Before 377 the trigger appended one more line on every UPDATE of a card under 18, glued to the
last paragraph. The text tests run everywhere. The integration test builds its own throwaway
DATABASE from the server behind TEST_DATABASE_URL, applies the forward SQL, fires the trigger
on a minimal clients table, then applies the rollback and shows the old body duplicating
again (guilt). All values are synthetic; only the throwaway database is dropped.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql

MIGRATION = Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "377_minor_note_once.sql"
LINE = "[MINORE] Verificare collegamento a profilo genitore. Età: "


def _parts() -> tuple[str, str]:
    forward, rollback = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))
    assert rollback is not None
    return forward, rollback


def test_forward_replaces_the_function_and_rollback_restores_the_appending_body() -> None:
    forward, rollback = _parts()
    assert "CREATE OR REPLACE FUNCTION public.check_minor_client()" in forward
    assert "regexp_replace" in forward
    assert "CREATE OR REPLACE FUNCTION public.check_minor_client()" in rollback
    assert "NEW.notes = COALESCE(NEW.notes || E'\\n', '')" in rollback


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


def _dsn_for(dbname: str) -> str:
    parts = urlsplit(_dsn())
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """A connection to a brand-new throwaway database; only that database is dropped."""
    name = f"mig377_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_dsn())
    c: asyncpg.Connection | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        c = await asyncpg.connect(_dsn_for(name))
        yield c
    finally:
        if c is not None:
            await c.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "CREATE TABLE clients (id int PRIMARY KEY, notes text, date_of_birth date, tags text[])"
    )
    await conn.execute(
        "CREATE TRIGGER trg_check_minor BEFORE INSERT OR UPDATE ON clients "
        "FOR EACH ROW EXECUTE FUNCTION check_minor_client()"
    )


async def _touch_twice(conn: asyncpg.Connection) -> None:
    await conn.execute("UPDATE clients SET tags = tags")
    await conn.execute("UPDATE clients SET tags = tags")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_current_line_on_top_and_stable_across_updates(
    conn: asyncpg.Connection,
) -> None:
    forward, rollback = _parts()
    await conn.execute(forward)
    await conn.execute(forward)  # idempotent
    await _table(conn)
    child = date.today() - timedelta(days=7 * 365 + 30)
    adult = date.today() - timedelta(days=30 * 365)
    recap = "WhatsApp conversation update, 2026-01-01: 3 client messages were received."
    await conn.executemany(
        "INSERT INTO clients VALUES ($1, $2, $3, $4)",
        [
            (1, f"{recap}\n{LINE}6 anni.\n{LINE}7 anni.", child, None),
            (2, None, child, ["x"]),
            (3, "staff note", adult, None),
        ],
    )
    await _touch_twice(conn)
    rows = {r["id"]: r for r in await conn.fetch("SELECT * FROM clients")}

    first = rows[1]["notes"]
    assert first.count("[MINORE]") == 1
    assert first.startswith(LINE + "7 anni.\n\n")
    assert first.endswith(recap)  # the recap stays the last paragraph
    assert rows[1]["tags"] == ["minore"]
    assert rows[2]["notes"] == LINE + "7 anni."
    assert rows[2]["tags"] == ["x", "minore"]
    assert rows[3]["notes"] == "staff note" and rows[3]["tags"] is None

    await conn.execute(rollback)
    await _touch_twice(conn)
    again = await conn.fetchval("SELECT notes FROM clients WHERE id = 2")
    assert again is not None
    assert again.count("[MINORE]") == 3  # the old body appends on every update
