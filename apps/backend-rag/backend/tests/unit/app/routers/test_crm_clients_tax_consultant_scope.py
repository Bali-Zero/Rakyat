"""A non-admin sees the clients where they are the tax consultant, not only the assigned ones.

Guard for the CRM client read scope (list + stats overview + AI summary). Before
this fix a non-admin was scoped to ``assigned_to`` alone, so a tax consultant
whose clients are assigned to someone else saw an empty book and zero stats.

The fake connection below stands in for Postgres: it applies the ownership
clauses the router writes into its SQL (``col = $n`` / ``col IS NULL``,
OR-ed inside parentheses, AND-ed at the top) to a small in-memory book. So the
assertions are about WHO SEES WHICH CLIENT, not about a SQL spelling, and a
clause that dropped the scope entirely would show up as a leak.

All emails and client ids are fictitious.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool

STAFF = "staff@example.test"
OTHER = "other@example.test"
OTHER_TAX = "other.tax@example.test"
ADMIN = {"email": "director@example.test", "role": "admin"}
NON_ADMIN = {"email": STAFF, "role": "agent"}

_NOW = datetime.now(tz=timezone.utc)


def _client(cid: int, assigned_to: str | None, tax_consultant: str | None) -> dict[str, Any]:
    return {
        "id": cid,
        "uuid": f"uuid-{cid}",
        "full_name": f"Client {cid}",
        "status": "active",
        "client_type": "individual",
        "assigned_to": assigned_to,
        "tax_consultant": tax_consultant,
        "first_contact_date": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
        "tags": [],
    }


BOOK = [
    _client(101, OTHER, STAFF),  # staff is tax consultant only → visible
    _client(102, OTHER, None),  # neither → hidden
    _client(103, STAFF, None),  # staff is assignee → visible (unchanged)
    _client(104, OTHER, OTHER_TAX),  # someone else's on both columns → hidden
]

_COL = r"(?:c\.)?(assigned_to|tax_consultant)"
_BIND = r"(?:ANY\(\$(\d+)::text\[\]\)|\$(\d+))"
_TERM = re.compile(_COL + r"\s*(?:=\s*" + _BIND + r"|(IS NULL))")
_GROUP = re.compile(
    r"(?:AND|WHERE)\s*\(((?:\s*"
    + _COL
    + r"\s*(?:=\s*(?:ANY\(\$\d+::text\[\]\)|\$\d+)|IS NULL)\s*(?:OR)?)+)\)"
)
_BARE = re.compile(r"(?:AND|WHERE)\s+" + _COL + r"\s*=\s*" + _BIND)


def _visible(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Apply the ownership clauses found in ``sql`` to BOOK."""
    groups: list[list[tuple[str, str | None]]] = []
    for g in _GROUP.finditer(sql):
        terms = []
        for col, any_idx, plain_idx, is_null in _TERM.findall(g.group(1)):
            terms.append((col, None if is_null else params[int(any_idx or plain_idx) - 1]))
        groups.append(terms)
    for col, any_idx, plain_idx in _BARE.findall(sql):
        groups.append([(col, params[int(any_idx or plain_idx) - 1])])
    return [r for r in BOOK if all(any(_owns(r[c], v) for c, v in g) for g in groups)]


def _owns(owner: Any, bound: Any) -> bool:
    """``col = $n`` for a scalar, ``col = ANY($n::text[])`` for a list."""
    return owner in bound if isinstance(bound, list) else owner == bound


class _FakeConn:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        if "practice_types" in sql:
            return []
        rows = _visible(sql, params)
        if "GROUP BY status" in sql:
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            return [{"status": k, "count": v} for k, v in counts.items()]
        if "GROUP BY assigned_to" in sql:
            counts = {}
            for r in rows:
                counts[r["assigned_to"]] = counts.get(r["assigned_to"], 0) + 1
            return [{"assigned_to": k, "count": v} for k, v in counts.items()]
        return rows

    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any] | None:
        self.sqls.append(sql)
        if "passport_expiry" in sql:
            return {"expired": 0, "expiring_soon": 0, "silent_30d": 0}
        if "COUNT(*)" in sql:
            return {"count": len(_visible(sql, params))}
        if "ai_summary" in sql:
            rows = [r for r in _visible(sql, params) if r["id"] == params[0]]
            if not rows:
                return None
            return {
                "id": rows[0]["id"],
                "ai_summary": None,
                "ai_summary_generated_at": None,
                "ai_summary_schema_version": None,
                "ai_summary_file_hash": None,
            }
        return None


def _app(user: dict[str, Any]) -> tuple[TestClient, _FakeConn]:
    from backend.app.routers import crm_clients
    from backend.core.cache import get_cache_service

    get_cache_service()._memory_cache.clear()
    conn = _FakeConn()

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    app = FastAPI()
    app.include_router(crm_clients.router)
    app.dependency_overrides[get_database_pool] = lambda: _Pool()
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), conn


def _list_ids(user: dict[str, Any]) -> set[int]:
    client, _ = _app(user)
    resp = client.get("/api/crm/clients/")
    assert resp.status_code == 200, resp.text
    return {c["id"] for c in resp.json()}


def test_non_admin_list_includes_clients_where_they_are_tax_consultant() -> None:
    assert _list_ids(NON_ADMIN) == {101, 103}


def test_non_admin_list_still_hides_clients_owned_by_others() -> None:
    ids = _list_ids(NON_ADMIN)
    assert 102 not in ids
    assert 104 not in ids


def test_admin_list_still_sees_the_whole_book() -> None:
    assert _list_ids(ADMIN) == {101, 102, 103, 104}


def test_non_admin_stats_count_tax_consultant_clients() -> None:
    client, _ = _app(NON_ADMIN)
    resp = client.get("/api/crm/clients/stats/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["new_last_30_days"] == 2


def test_admin_stats_still_count_the_whole_book() -> None:
    client, _ = _app(ADMIN)
    resp = client.get("/api/crm/clients/stats/overview")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 4


@pytest.mark.parametrize(("cid", "visible"), [(101, True), (102, False), (104, False)])
def test_non_admin_ai_summary_follows_the_same_scope(cid: int, visible: bool) -> None:
    client, _ = _app(NON_ADMIN)
    resp = client.get(f"/api/crm/clients/{cid}/ai-summary")
    assert (resp.status_code == 200) is visible, (resp.status_code, resp.text)
