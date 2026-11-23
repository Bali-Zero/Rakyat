"""The assistant's CRM tool answers within the scope of the person who asked.

``crm_query`` used to read the whole book for anyone the assistant served. It now reads
what the desk gives the same signed-in person (the e-mail and role of the session), or,
on the WhatsApp path, what the sibling WhatsApp tools give the sender; a call that names
nobody reads nothing.

These tests run the tool's own statements on a real PostgreSQL, in a private schema with
hand-written tables. The pool the tool is handed records every acquisition and every
statement. Invented rows.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

import backend.app.deps.crm_access as crm_access
import backend.app.utils.client_pools as client_pools
import backend.app.utils.crm_utils as crm_utils
from backend.app.deps.crm_access import get_crm_user_filter, get_practices_user_filter
from backend.services.rag.agentic._crm_precall import (
    CLIENT_COUNTS,
    DATA,
    PRACTICE_COUNTS,
    ROW_FIELDS,
    judge_crm_precall,
)
from backend.services.rag.agentic.tools import CRMTool

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database"),
]

MEMBER = "member.one@example.test"
PEER = "peer.two@example.test"  # shares a pool with MEMBER
SOLO = "solo.three@example.test"  # in a pool of one
LONER = "loner.four@example.test"  # holds nothing
TAXER = "tax.five@example.test"  # tax consultant of one client, owner of none
OTHER = "other.six@example.test"
ADMIN_BY_ADDRESS = "admin.seven@example.test"
FULL_VIEW = "fullview.eight@example.test"
BOSS = "boss.nine@example.test"  # in no list; whatever the token's role says

# The columns the tool reads, as far as it reads them.
_TABLES = f"""
CREATE TABLE clients (
    id integer PRIMARY KEY, full_name text, email text, nationality text, status text,
    phone text, passport_number text, assigned_to text, tax_consultant text,
    deleted_at timestamptz, created_at timestamptz, updated_at timestamptz,
    passport_expiry date
);
CREATE TABLE client_family_members (
    id integer PRIMARY KEY, client_id integer, full_name text,
    passport_expiry date, visa_expiry date
);
CREATE TABLE documents (
    id integer PRIMARY KEY, client_id integer, document_type text, expiry_date date,
    status text, is_archived boolean
);
CREATE TABLE practice_types (id integer PRIMARY KEY, name text);
CREATE TABLE practices (
    id integer PRIMARY KEY, client_id integer, practice_type_id integer, status text,
    notes text, assigned_to text, updated_at timestamptz
);
INSERT INTO practice_types VALUES (1, 'KITAS E33'), (2, 'Company Setup'), (3, 'Visa C1');
INSERT INTO clients (id, full_name, email, status, passport_number, assigned_to,
                     tax_consultant, deleted_at) VALUES
 (1,  'Zzfind Own',     'c1@example.test',     'active',   'P0001', '{MEMBER}', NULL, NULL),
 (2,  'Client Two',     'c2@example.test',     'lead',     'P0002', '{PEER}',   NULL, NULL),
 (3,  'Client Three',   'c3@example.test',     'active',   'P0003', '{OTHER}',  '{MEMBER}', NULL),
 (4,  'Client Four',    'c4@example.test',     'inactive', 'P0004', '{OTHER}',  NULL, NULL),
 (5,  'Client Five',    'c5@example.test',     'active',   'P0005', NULL,       NULL, NULL),
 (6,  'Client Six',     'c6@example.test',     'active',   'P0006', '{MEMBER}', NULL, now()),
 (7,  'Client Seven',   'c7@example.test',     'active',   'P0007', '{OTHER}',  '{TAXER}', NULL),
 (8,  'Client Eight',   'c8@example.test',     'active',   'P0008', '{SOLO}',   NULL, NULL),
 (9,  'Client Nine',    'c9@example.test',     'active',   'P0009', 'Member.One@Example.Test',
                                                                              NULL, NULL),
 (10, 'Zzfind By Name', 'c10@example.test',    'active',   'P0010', '{OTHER}',  NULL, NULL),
 (11, 'Client Eleven',  'zzfind@example.test', 'active',   'P0011', '{OTHER}',  NULL, NULL),
 (12, 'Client Twelve',  'c12@example.test',    'active',   'ZZFIND', '{OTHER}', NULL, NULL);
UPDATE clients SET created_at = timestamptz '2026-01-01' + id * interval '1 day',
                   updated_at = timestamptz '2026-02-01' + id * interval '1 day';
INSERT INTO practices (id, client_id, practice_type_id, status, assigned_to) VALUES
 (1,  1, 1,    'on_process',        '{MEMBER}'),
 (2,  4, 1,    'waiting_documents', '{MEMBER}'),
 (3,  1, 3,    'on_process',        '{OTHER}'),
 (4,  4, 2,    'completed',         '{OTHER}'),
 (5,  6, 1,    'sending_invoice',   '{MEMBER}'),
 (6,  2, NULL, 'inquiry',           '{PEER}'),
 (7,  4, 2,    'on_process',        '{OTHER}'),
 (8,  4, 2,    'cancelled',         '{OTHER}'),
 (9,  4, 2,    'on_process',        '{OTHER}'),
 (10, 4, 2,    'on_process',        '{OTHER}'),
 (11, 9, 3,    'completed',         '{OTHER}');
UPDATE practices SET updated_at = timestamptz '2026-03-01' + id * interval '1 day';
"""

_LIVE = {1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12}  # every client that is not deleted

# The expiry dates of record are read through the production view, as migrated.
_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "db"
    / "migrations_v2"
    / "352_client_expiry_alerts_view_finite_dates.sql"
).read_text()
_EXPIRY_VIEW = _MIGRATION[
    _MIGRATION.index("CREATE OR REPLACE VIEW client_expiry_alerts_view") : _MIGRATION.index(
        "-- === ROLLBACK ==="
    )
]

# Days from today. In the default 30 days: 10, 12, 15, 20, 25. Left out: the passport of
# an inactive client (the view's rule), a deleted client, today, yesterday, a rejected and
# an archived document, and 45 days out (inside 60).
_EXPIRIES = """
UPDATE clients SET passport_expiry = CURRENT_DATE + 10 WHERE id = 1;
UPDATE clients SET passport_expiry = CURRENT_DATE + 3 WHERE id = 4;
UPDATE clients SET passport_expiry = CURRENT_DATE + 5 WHERE id = 6;
INSERT INTO client_family_members VALUES (1, 4, 'Family Of Four', NULL, CURRENT_DATE + 20);
INSERT INTO documents VALUES
 (1, 3, 'KITAS', CURRENT_DATE + 15, 'verified', false),
 (2, 1, 'KITAS', CURRENT_DATE + 45, 'verified', NULL),
 (3, 1, 'Visa', CURRENT_DATE - 1, 'verified', false),
 (4, 1, 'Visa', CURRENT_DATE, 'verified', false),
 (5, 2, 'KITAS', CURRENT_DATE + 7, 'rejected', false),
 (6, 2, 'KITAS', CURRENT_DATE + 8, 'verified', true),
 (7, 2, 'NPWP', CURRENT_DATE + 25, 'verified', false),
 (8, 9, 'KITAS', CURRENT_DATE + 12, 'verified', false);
"""


class _Recording:
    """The pool handed to the tool: the real one, recording what the tool does with it."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.acquisitions = 0
        self.statements: list[str] = []

    def __bool__(self) -> bool:
        return True

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[_Recording, None]:
        self.acquisitions += 1
        async with self._pool.acquire() as conn:
            self._conn = conn
            yield self

    async def fetch(self, statement: str, *args: Any) -> list[asyncpg.Record]:
        self.statements.append(statement)
        return await self._conn.fetch(statement, *args)

    async def fetchrow(self, statement: str, *args: Any) -> asyncpg.Record | None:
        self.statements.append(statement)
        return await self._conn.fetchrow(statement, *args)


@pytest_asyncio.fixture
async def pool(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[asyncpg.Pool, None]:
    # The desk's lists, as this file needs them: one admin by address, one full-view
    # address, and one pool of two with one pool of one.
    monkeypatch.setattr(crm_utils, "_crm_admin_emails", lambda: frozenset({ADMIN_BY_ADDRESS}))
    monkeypatch.setattr(crm_utils, "_practices_full_view_emails", lambda: frozenset({FULL_VIEW}))
    monkeypatch.setattr(crm_access, "_clients_full_view_emails", lambda: frozenset({FULL_VIEW}))
    monkeypatch.setattr(
        client_pools,
        "_VALIDATED",
        (frozenset({MEMBER, PEER}), frozenset({SOLO})),
        raising=False,
    )
    admin = await asyncpg.connect(_DB_URL)
    schema = "assistant_crm_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as c:
            await c.execute(_TABLES)
            await c.execute(_EXPIRY_VIEW)
            await c.execute(_EXPIRIES)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest_asyncio.fixture
async def tool(pool: asyncpg.Pool) -> CRMTool:
    return CRMTool(db_pool=_Recording(pool))


def _desk(email: str, role: str | None = "team") -> dict[str, Any]:
    return {"_desk_user": {"email": email, "role": role}}


def _whatsapp(role: str, email: str | None = None) -> dict[str, Any]:
    profile: dict[str, Any] = {"role": role}
    if email is not None:
        profile["email"] = email
    return {"_caller_profile": profile}


async def _ask(tool: CRMTool, query_type: str, who: dict[str, Any], **more: Any) -> Any:
    return json.loads(await tool.execute(query_type=query_type, **more, **who))


async def _ids(tool: CRMTool, query_type: str, who: dict[str, Any], **more: Any) -> list[int]:
    return [row["id"] for row in await _ask(tool, query_type, who, **more)]


async def _all_matching(tool: CRMTool, who: dict[str, Any]) -> set[int]:
    """Every client the caller is given: all e-mails here end the same way."""
    return set(await _ids(tool, "search_clients", who, search_term="example.test", limit=50))


_WHOLE_BOOK = [
    pytest.param(_desk(ADMIN_BY_ADDRESS), id="an admin by address"),
    pytest.param(_desk(FULL_VIEW), id="a full-view address"),
    pytest.param(_desk(BOSS, "admin"), id="token role admin"),
    pytest.param(_desk(BOSS, "Board Member"), id="token role board member"),
    pytest.param(_desk(BOSS, "CEO"), id="token role ceo"),
    pytest.param(_desk(BOSS, "founder"), id="token role founder"),
    pytest.param(_whatsapp("creator"), id="the WhatsApp creator"),
    pytest.param(_whatsapp("team", ADMIN_BY_ADDRESS), id="a WhatsApp sender who is a CRM admin"),
]

_SCOPED = [
    pytest.param(
        _desk(MEMBER), {1, 2, 3}, id="a member of a pool: own, the peer's, and as tax consultant"
    ),
    pytest.param(_desk(PEER), {1, 2, 3}, id="the other member of that pool"),
    pytest.param(_desk(SOLO), {8}, id="a pool of one"),
    pytest.param(_desk(LONER), set(), id="a member who holds nothing"),
    pytest.param(_desk(TAXER), {7}, id="a tax consultant only"),
    pytest.param(_desk(OTHER), {3, 4, 7, 10, 11, 12}, id="a member with no pool"),
    pytest.param(
        _desk("Member.One@Example.Test"), {1, 2, 3}, id="the caller's e-mail in mixed case"
    ),
    pytest.param(_desk(MEMBER, ""), {1, 3}, id="a role that is no team role: no pool"),
    pytest.param(_desk(MEMBER, None), {1, 3}, id="no role at all: no pool"),
    pytest.param(_desk(BOSS, "team"), set(), id="the token says member"),
    pytest.param(_whatsapp("team", MEMBER), {1, 9}, id="a WhatsApp sender: the own address only"),
    pytest.param(_whatsapp("team", PEER), {2}, id="a WhatsApp sender in a desk pool: no pool"),
]


@pytest.mark.parametrize("who", _WHOLE_BOOK)
async def test_who_holds_the_whole_book_is_given_every_client_that_is_not_deleted(
    tool: CRMTool, who: dict[str, Any]
) -> None:
    assert await _all_matching(tool, who) == _LIVE


@pytest.mark.parametrize(("who", "held"), _SCOPED)
async def test_a_scoped_caller_is_given_the_clients_held_and_no_other(
    tool: CRMTool, who: dict[str, Any], held: set[int]
) -> None:
    assert await _all_matching(tool, who) == held


@pytest.mark.parametrize(("who", "held"), _SCOPED)
async def test_recent_clients_are_the_active_ones_among_the_clients_held_newest_first(
    tool: CRMTool, who: dict[str, Any], held: set[int]
) -> None:
    active = held - {2, 4}  # client 2 is a lead, client 4 inactive

    assert await _ids(tool, "recent_clients", who, limit=50) == sorted(active, reverse=True)


@pytest.mark.parametrize(("who", "held"), _SCOPED)
async def test_client_counts_are_counts_of_the_clients_held(
    tool: CRMTool, who: dict[str, Any], held: set[int]
) -> None:
    counts = (await _ask(tool, "client_stats", who))["clients"]

    assert counts == {
        "active_clients": len(held - {2, 4}),
        "leads": len(held & {2}),
        "inactive": len(held & {4}),
        "total": len(held),
    }


async def test_search_keeps_its_own_term_order_and_limit_inside_the_scope(tool: CRMTool) -> None:
    # Newest update first, as the tool has always ordered; the limit cuts after the scope.
    assert await _ids(tool, "search_clients", _desk(OTHER), search_term="example.test") == [
        12,
        11,
        10,
        7,
        4,
        3,
    ]
    assert await _ids(
        tool, "search_clients", _desk(OTHER), search_term="example.test", limit=2
    ) == [12, 11]
    assert await _ids(tool, "recent_clients", _desk(OTHER), limit=2) == [12, 11]


async def test_a_client_of_someone_else_stays_out_whichever_field_the_term_matches(
    tool: CRMTool,
) -> None:
    # 10 matches by name, 11 by e-mail, 12 by passport; all three belong to another member.
    assert await _ids(tool, "search_clients", _desk(MEMBER), search_term="zzfind") == [1]
    assert await _ids(tool, "search_clients", _desk(OTHER), search_term="zzfind") == [12, 11, 10]
    assert await _ids(tool, "search_clients", _desk(BOSS, "admin"), search_term="zzfind") == [
        12,
        11,
        10,
        1,
    ]


async def test_an_owner_stored_in_mixed_case_is_not_found_by_the_desk_and_is_found_by_whatsapp(
    tool: CRMTool,
) -> None:
    # The desk compares the stored owner as it is written; so does the tool on that path.
    assert 9 not in await _all_matching(tool, _desk(MEMBER))
    assert 9 in await _all_matching(tool, _whatsapp("team", MEMBER))


async def test_a_deleted_client_is_given_to_nobody(tool: CRMTool) -> None:
    for who in (_desk(MEMBER), _desk(BOSS, "founder"), _whatsapp("creator")):
        assert 6 not in await _all_matching(tool, who)


def _groups(rows: list[dict[str, Any]]) -> list[tuple[str, int, int, int]]:
    return [(r["service_type"], r["count"], r["on_process"], r["completed"]) for r in rows]


async def test_the_desk_counts_practices_by_their_own_assignee(tool: CRMTool) -> None:
    # MEMBER's owners are MEMBER and PEER. Practice 2 is MEMBER's on another owner's
    # client: counted. Practice 3 is another assignee's on MEMBER's client: not counted.
    # Practice 5 is on a deleted client: the desk counts practices without looking at
    # the client, and so does the tool on this path.
    nested = (await _ask(tool, "client_stats", _desk(MEMBER)))["practices"]
    assert nested == {
        "on_process": 1,
        "completed": 0,
        "sending_invoice": 1,
        "waiting_documents": 1,
        "inquiry": 1,
        "cancelled": 0,
        "total": 4,
    }
    assert _groups(await _ask(tool, "practice_stats", _desk(MEMBER))) == [
        ("KITAS E33", 3, 1, 0),
        ("Unknown", 1, 0, 0),
    ]


async def test_practice_groups_are_counted_inside_the_scope_then_ordered_then_limited(
    tool: CRMTool,
) -> None:
    whole = [
        ("Company Setup", 5, 3, 1),
        ("KITAS E33", 3, 1, 0),
        ("Visa C1", 2, 1, 1),
        ("Unknown", 1, 0, 0),
    ]
    assert _groups(await _ask(tool, "practice_stats", _desk(BOSS, "admin"))) == whole
    assert _groups(await _ask(tool, "practice_stats", _desk(FULL_VIEW))) == whole
    assert _groups(await _ask(tool, "practice_stats", _desk(BOSS, "admin"), limit=2)) == whole[:2]
    # The largest group of the book is not MEMBER's: its first group is its own largest.
    assert _groups(await _ask(tool, "practice_stats", _desk(MEMBER), limit=1)) == [
        ("KITAS E33", 3, 1, 0)
    ]
    assert (await _ask(tool, "client_stats", _desk(BOSS, "admin")))["practices"]["total"] == 11
    assert await _ask(tool, "practice_stats", _desk(LONER)) == []


async def test_whatsapp_holds_a_practice_through_its_client_s_assignee(tool: CRMTool) -> None:
    # The sender's clients are 1 and 9. Practices 1 and 3 are on client 1 (3 is another
    # assignee's: counted here, not on the desk), practice 11 on client 9. Practice 2 is
    # the sender's own on another owner's client: not counted here, counted on the desk.
    who = _whatsapp("team", MEMBER)
    assert (await _ask(tool, "client_stats", who))["practices"] == {
        "on_process": 2,
        "completed": 1,
        "sending_invoice": 0,
        "waiting_documents": 0,
        "inquiry": 0,
        "cancelled": 0,
        "total": 3,
    }
    assert _groups(await _ask(tool, "practice_stats", who)) == [
        ("Visa C1", 2, 1, 1),
        ("KITAS E33", 1, 1, 0),
    ]
    # The whole book on this path leaves out the practice of the deleted client.
    assert (await _ask(tool, "client_stats", _whatsapp("creator")))["practices"]["total"] == 10


def _expiring(rows: list[dict[str, Any]]) -> list[tuple[int, str, str, int]]:
    return [
        (r["client_id"], r["entity_type"], r["document_type"], r["days_until_expiry"]) for r in rows
    ]


_EVERYONE_IN_30_DAYS = [
    (1, "client", "passport", 10),
    (9, "document", "KITAS", 12),
    (3, "document", "KITAS", 15),
    (4, "family_member", "visa", 20),
    (2, "document", "NPWP", 25),
]


async def test_expiring_documents_are_the_expiry_dates_of_record_inside_the_window(
    tool: CRMTool,
) -> None:
    admin = _desk(BOSS, "ceo")

    rows = await _ask(tool, "expiring_documents", admin)
    assert _expiring(rows) == _EVERYONE_IN_30_DAYS
    assert {frozenset(row) for row in rows} == {ROW_FIELDS["expiring_documents"]}
    assert rows[2]["client_name"] == "Client Three"
    assert rows[3]["entity_name"] == "Family Of Four"

    wider = _expiring(await _ask(tool, "expiring_documents", admin, days_ahead=60))
    assert wider == [*_EVERYONE_IN_30_DAYS, (1, "document", "KITAS", 45)]
    assert (
        _expiring(await _ask(tool, "expiring_documents", admin, days_ahead=12))
        == (_EVERYONE_IN_30_DAYS[:2])
    )
    assert (
        _expiring(await _ask(tool, "expiring_documents", admin, limit=2))
        == (_EVERYONE_IN_30_DAYS[:2])
    )


async def test_expiring_documents_follow_the_scope_and_leave_deleted_clients_out(
    tool: CRMTool,
) -> None:
    # The desk holds an expiry row by its client's assignee under the practices rule: the
    # member's pool (the member and the peer), exactly as stored.
    assert get_practices_user_filter({"email": MEMBER, "role": "team"}) == [MEMBER, PEER]
    assert _expiring(await _ask(tool, "expiring_documents", _desk(MEMBER))) == [
        (1, "client", "passport", 10),
        (2, "document", "NPWP", 25),
    ]
    assert _expiring(await _ask(tool, "expiring_documents", _desk(OTHER))) == [
        (3, "document", "KITAS", 15),
        (4, "family_member", "visa", 20),
    ]
    # WhatsApp: the sender's own address, in any letter case, as its client rule.
    assert _expiring(await _ask(tool, "expiring_documents", _whatsapp("team", MEMBER))) == [
        (1, "client", "passport", 10),
        (9, "document", "KITAS", 12),
    ]
    assert await _ask(tool, "expiring_documents", _desk(LONER)) == []
    assert await _ask(tool, "expiring_documents", _whatsapp("team", LONER)) == []


_NOBODY = [
    pytest.param({}, id="no identity at all"),
    pytest.param({"_user_id": MEMBER}, id="a user id alone"),
    pytest.param({"_desk_user": {"email": "", "role": "admin"}}, id="a desk user with no e-mail"),
    pytest.param(
        {"_desk_user": {"email": "member.one", "role": "admin"}}, id="an e-mail with no at sign"
    ),
    pytest.param({"_desk_user": {"role": "admin"}}, id="a desk user with no e-mail key"),
    pytest.param({"_desk_user": "member.one@example.test"}, id="a desk user that is not a mapping"),
    pytest.param(
        {"_desk_user": {"email": "", "role": "team"}, "_caller_profile": {"role": "creator"}},
        id="a broken desk user is not rescued by a profile",
    ),
    pytest.param(_whatsapp("team"), id="a WhatsApp team profile with no e-mail"),
    pytest.param(_whatsapp("client", MEMBER), id="a WhatsApp profile that is not team"),
    pytest.param({"_caller_profile": "creator"}, id="a profile that is not a mapping"),
]
_QUERY_TYPES = [
    "client_stats",
    "search_clients",
    "expiring_documents",
    "practice_stats",
    "recent_clients",
]


@pytest.mark.parametrize("who", _NOBODY)
@pytest.mark.parametrize("query_type", _QUERY_TYPES)
async def test_a_call_that_names_nobody_is_refused_and_reads_nothing(
    tool: CRMTool, who: dict[str, Any], query_type: str
) -> None:
    answer = await _ask(tool, query_type, who, search_term="example.test")

    assert answer == {"error": "CRM access needs a signed-in team member"}
    assert tool.db_pool.acquisitions == 0
    assert tool.db_pool.statements == []


async def test_the_token_s_role_decides_and_the_roster_is_never_asked(tool: CRMTool) -> None:
    # One address, in no list. What the session says is what the tool gives: there is no
    # roster row here to disagree with it, and no statement that would look for one.
    assert await _all_matching(tool, _desk(BOSS, "team")) == set()
    assert await _all_matching(tool, _desk(BOSS, "founder")) == _LIVE
    for query_type in _QUERY_TYPES:
        await _ask(tool, query_type, _desk(MEMBER), search_term="example.test")
    assert tool.db_pool.statements
    assert not [s for s in tool.db_pool.statements if "team_members" in s]


class _RosterThatCannotBeRead:
    async def fetch(self, *_: Any) -> list[Any]:
        raise OSError("the database cannot be reached")


class _RosterWithNobody:
    async def fetch(self, *_: Any) -> list[Any]:
        return []


@pytest.mark.parametrize(
    "state",
    [
        "no pools",
        "one pool of two",
        "a pool of one",
        "never validated",
        "validation failed",
        "an invalid entry",
    ],
)
async def test_whatever_the_state_of_the_pools_the_tool_gives_what_the_desk_filters_give(
    tool: CRMTool, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    two = (frozenset({MEMBER, PEER}),)
    if state == "no pools":
        monkeypatch.setattr(client_pools, "_VALIDATED", ())
    elif state == "one pool of two":
        monkeypatch.setattr(client_pools, "_VALIDATED", two)
    elif state == "a pool of one":
        monkeypatch.setattr(client_pools, "_VALIDATED", (frozenset({MEMBER}),))
    elif state == "never validated":
        monkeypatch.setattr(client_pools, "_VALIDATED", None)
    else:
        # Driven through the application's own validation, on a connection that is not
        # the tool's.
        monkeypatch.setattr(client_pools, "crm_client_pools", lambda: two)
        roster = _RosterThatCannotBeRead() if state == "validation failed" else _RosterWithNobody()
        await client_pools.validate_client_pools(roster)
        assert client_pools._VALIDATED == (None if state == "validation failed" else ())
    user = {"email": MEMBER, "role": "team"}
    owners = get_crm_user_filter(user)
    practice_owners = get_practices_user_filter(user)
    assert owners == ([MEMBER, PEER] if state == "one pool of two" else [MEMBER])

    expected_clients = await pool.fetch(
        "SELECT id FROM clients WHERE deleted_at IS NULL"
        " AND (assigned_to = ANY($1::text[]) OR tax_consultant = ANY($1::text[]))",
        owners,
    )
    expected_practices = await pool.fetchval(
        "SELECT count(*) FROM practices WHERE assigned_to = ANY($1::text[])", practice_owners
    )

    assert await _all_matching(tool, _desk(MEMBER)) == {row["id"] for row in expected_clients}
    counts = await _ask(tool, "client_stats", _desk(MEMBER))
    assert counts["practices"]["total"] == expected_practices
    assert not [s for s in tool.db_pool.statements if "team_members" in s]


async def test_the_fields_the_pre_call_expects_are_the_fields_the_tool_returns(
    tool: CRMTool,
) -> None:
    who = _desk(BOSS, "admin")
    for query_type, fields in ROW_FIELDS.items():
        answer = await tool.execute(query_type=query_type, search_term="example.test", **who)
        rows = json.loads(answer)
        assert rows, query_type
        assert {frozenset(row) for row in rows} == {fields}, query_type
        assert judge_crm_precall(query_type, answer) == (DATA, ""), query_type
    answer = await tool.execute(query_type="client_stats", **who)
    stats = json.loads(answer)
    assert set(stats) == {"clients", "practices"}
    assert set(stats["clients"]) == CLIENT_COUNTS
    assert set(stats["practices"]) == PRACTICE_COUNTS
    assert judge_crm_precall("client_stats", answer) == (DATA, "")


async def test_search_still_needs_a_term_and_an_unknown_query_is_still_an_error(
    tool: CRMTool,
) -> None:
    assert await _ask(tool, "search_clients", _desk(MEMBER)) == {"error": "search_term required"}
    assert await _ask(tool, "everything", _desk(MEMBER)) == {
        "error": "Unknown query_type: everything"
    }
