"""The handoff's customer lookup ignores soft-deleted rows.

``clients.deleted_at`` is the soft-delete marker (migration 136). The lookup
used "first row by id" with no filter, so a soft-deleted duplicate with a lower
id won and the alert went to that old row's consultant. Live duplicates are
ordered as the WhatsApp identity lookup orders them (status priority, then id).

This module's tests do not use PostgreSQL, so the connection is a fake that
answers by what the SQL actually says: a query without the soft-delete filter
sees the deleted row first. Synthetic data only.
"""

from __future__ import annotations

import re

import pytest

from backend.services import whatsapp_identity
from backend.services.integrations import wa_human_handoff
from backend.services.integrations.wa_human_handoff import _resolve_assignee

PHONE = "628400002000"

_DELETED_LOWER_ID = {"id": 10, "assigned_to": "old.consultant@example.test"}
_LIVE_HIGHER_ID = {"id": 20, "assigned_to": "live.consultant@example.test"}


class _Conn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.sql: str | None = None

    async def fetchrow(self, sql: str, *args: object) -> dict | None:
        self.sql = sql
        filtered = "deleted_at IS NULL" in re.sub(r"\s+", " ", sql)
        visible = [r for r in self._rows if not (filtered and r.get("deleted"))]
        return visible[0] if visible else None


class _Pool:
    def __init__(self, rows: list[dict]) -> None:
        self.conn = _Conn(rows)

    def acquire(self) -> object:
        conn = self.conn

        class _CM:
            async def __aenter__(self) -> _Conn:
                return conn

            async def __aexit__(self, *exc: object) -> bool:
                return False

        return _CM()


def _order_by(sql: str) -> str:
    tail = re.split(r"ORDER BY", sql, maxsplit=1)[1]
    return re.sub(r"\s+", " ", tail.split("LIMIT")[0]).strip()


class TestGuiltASoftDeletedDuplicateDoesNotWin:
    @pytest.mark.asyncio
    async def test_the_live_rows_consultant_is_notified(self) -> None:
        pool = _Pool([{**_DELETED_LOWER_ID, "deleted": True}, _LIVE_HIGHER_ID])

        assert await _resolve_assignee(pool, PHONE) == (20, "live.consultant@example.test")

    def test_the_sql_filters_soft_deleted_rows(self) -> None:
        assert "deleted_at IS NULL" in re.sub(r"\s+", " ", wa_human_handoff._ASSIGNEE_LOOKUP_SQL)

    def test_live_duplicates_are_ordered_as_the_identity_lookup_orders_them(self) -> None:
        assert _order_by(wa_human_handoff._ASSIGNEE_LOOKUP_SQL) == _order_by(
            whatsapp_identity._CLIENT_LOOKUP_SQL
        )


class TestInnocenceOtherShapesBehaveAsBefore:
    @pytest.mark.asyncio
    async def test_a_single_live_row_is_found(self) -> None:
        assert await _resolve_assignee(_Pool([_LIVE_HIGHER_ID]), PHONE) == (
            20,
            "live.consultant@example.test",
        )

    @pytest.mark.asyncio
    async def test_a_live_row_without_a_consultant_keeps_a_client_id(self) -> None:
        pool = _Pool([{"id": 30, "assigned_to": "  "}])

        assert await _resolve_assignee(pool, PHONE) == (30, None)

    @pytest.mark.asyncio
    async def test_no_row_resolves_to_nothing(self) -> None:
        assert await _resolve_assignee(_Pool([]), PHONE) == (None, None)

    @pytest.mark.asyncio
    async def test_only_a_soft_deleted_row_resolves_to_nothing(self) -> None:
        pool = _Pool([{**_DELETED_LOWER_ID, "deleted": True}])

        assert await _resolve_assignee(pool, PHONE) == (None, None)
