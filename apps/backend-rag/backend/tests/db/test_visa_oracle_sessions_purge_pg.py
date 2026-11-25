"""Real-Postgres coverage for Visa Oracle session retention."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest

pytestmark = pytest.mark.integration


class _SingleConnectionPool:
    """Yield the fixture transaction so purge writes remain rollback-scoped."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner) -> asyncpg.Connection:
                return conn

            async def __aexit__(self_inner, *exc: object) -> bool:
                return False

        return _Ctx()


_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations_v2"
    / "317_visa_oracle_sessions_retention_30d.sql"
)
_TABLE_HEAD = "CREATE TABLE IF NOT EXISTS public.visa_oracle_sessions"


async def _create_sessions_table(conn: asyncpg.Connection) -> None:
    """A transaction-local copy of the migration's own table: no shared row is touched."""
    sql = _MIGRATION.read_text(encoding="utf-8")
    start = sql.index(_TABLE_HEAD)
    end = sql.index(");", start) + 1
    ddl = sql[start:end].replace(_TABLE_HEAD, "CREATE TEMP TABLE visa_oracle_sessions", 1)
    await conn.execute(ddl + " ON COMMIT DROP")


async def _session_ids(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT session_id FROM visa_oracle_sessions")
    return {row["session_id"] for row in rows}


@pytest.mark.asyncio
async def test_purge_keeps_expired_handed_off_and_unexpired_sessions(
    db_tx: asyncpg.Connection,
) -> None:
    """Guilt: expired FALSE and NULL rows must purge, but TRUE must remain."""
    from backend.app.routers import visa_oracle as mod

    await _create_sessions_table(db_tx)
    await db_tx.executemany(
        """
        INSERT INTO visa_oracle_sessions (session_id, handoff_triggered, expires_at)
        VALUES ($1, $2, NOW() + $3)
        """,
        [
            ("test-expired-handed-off", True, timedelta(hours=-1)),
            ("test-expired-not-handed-off", False, timedelta(hours=-1)),
            ("test-expired-null-handoff", None, timedelta(hours=-1)),
            ("test-live-not-handed-off", False, timedelta(hours=1)),
            ("test-live-null-handoff", None, timedelta(hours=1)),
        ],
    )

    deleted = await mod._purge_expired_sessions(_SingleConnectionPool(db_tx))

    assert deleted == 2
    assert await _session_ids(db_tx) == {
        "test-expired-handed-off",
        "test-live-not-handed-off",
        "test-live-null-handoff",
    }


@pytest.mark.asyncio
async def test_purge_honours_limit_across_repeated_calls(
    db_tx: asyncpg.Connection,
) -> None:
    """Guilt: a bounded call removes only one eligible expired row."""
    from backend.app.routers import visa_oracle as mod

    await _create_sessions_table(db_tx)
    await db_tx.executemany(
        """
        INSERT INTO visa_oracle_sessions (session_id, handoff_triggered, expires_at)
        VALUES ($1, FALSE, NOW() - INTERVAL '1 hour')
        """,
        [("test-limited-expired-1",), ("test-limited-expired-2",), ("test-limited-expired-3",)],
    )
    pool = _SingleConnectionPool(db_tx)

    assert await mod._purge_expired_sessions(pool, limit=1) == 1
    assert len(await _session_ids(db_tx)) == 2
    assert await mod._purge_expired_sessions(pool, limit=1) == 1
    assert await mod._purge_expired_sessions(pool, limit=1) == 1
    assert await _session_ids(db_tx) == set()


@pytest.mark.asyncio
async def test_purge_returns_zero_when_no_session_is_expired(
    db_tx: asyncpg.Connection,
) -> None:
    """Innocence: live sessions are left untouched and report no deletion."""
    from backend.app.routers import visa_oracle as mod

    await _create_sessions_table(db_tx)
    await db_tx.executemany(
        """
        INSERT INTO visa_oracle_sessions (session_id, handoff_triggered, expires_at)
        VALUES ($1, $2, NOW() + INTERVAL '1 hour')
        """,
        [("test-live-false", False), ("test-live-null", None)],
    )

    deleted = await mod._purge_expired_sessions(_SingleConnectionPool(db_tx))

    assert deleted == 0
    assert await _session_ids(db_tx) == {"test-live-false", "test-live-null"}
