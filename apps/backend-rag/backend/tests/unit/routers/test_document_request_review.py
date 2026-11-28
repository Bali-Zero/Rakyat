"""The reviewers' side of the document requests (V2, second cut): list, decide, reopen.

The three routes are driven through a small FastAPI app that mounts the real router with a
recording fake connection (as test_document_requests.py does), so the permission matrix, the
validation, the 409 bodies, the order of the locks and the log lines are exercised end to end.
The real tables are covered in backend/tests/db/test_document_request_review_pg.py. Names and
addresses here are invented.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.dependencies import get_current_user, get_database_pool

REVIEWER = "reviewer@example.test"
ADMIN = "boss@example.test"
OUTSIDER = "outsider@example.test"
WHEN = datetime(2026, 11, 28, 9, 30, tzinfo=timezone.utc)
BASE = "/api/crm/practices/document-requests"
TYPED_LABEL = "Zz Typed Label Text"
TYPED_REASON = "Zz reason typed by a consultant"
TYPED_TEXT = "Zz client text typed by a consultant"
ITEM_KEYS = {
    "id",
    "practice_id",
    "service_name",
    "document_type",
    "label",
    "client_text",
    "reason",
    "requested_by",
    "requested_at",
    "seen_count",
    "card_state",
    "card_live",
    "decision",
    "decision_condition",
    "decided_by",
    "decided_at",
}


def _user(email: str | None, role: str | None = "member") -> dict[str, Any]:
    user: dict[str, Any] = {"id": "id-fixture", "full_name": "Fixture"}
    if email is not None:
        user["email"] = email
    if role is not None:
        user["role"] = role
    return user


def _list_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": 77,
        "practice_id": 7,
        "service_name": "Zz Service",
        "document_type": "proof_of_accommodation",
        "label": TYPED_LABEL,
        "client_text": TYPED_TEXT,
        "reason": TYPED_REASON,
        "requested_by": "creator@example.test",
        "requested_at": WHEN,
        "seen_count": 2,
        "decision": "pending",
        "decision_condition": None,
        "decided_by": None,
        "decided_at": None,
        "has_card": True,
        "card_active": True,
        "type_code": "zz_code",
        "on_card": False,
    }
    row.update(over)
    return row


class FakeConn:
    """Records every statement and answers by the shape of the SQL."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.queries: list[tuple[str, tuple]] = []
        self.request: dict[str, Any] | None = {
            "id": 77,
            "practice_type_id": 10,
            "document_type": "proof_of_accommodation",
            "label": TYPED_LABEL,
            "client_text": TYPED_TEXT,
            "decision": "pending",
        }
        self.card = True
        self.conflict = False
        self.card_document = True
        self.decided_at: Any = WHEN
        self.rows: list[dict[str, Any]] = [_list_row()]
        self.total = 1

    def sql_like(self, fragment: str) -> list[tuple[str, tuple]]:
        return [q for q in self.queries if fragment in q[0]]

    def request_updates(self) -> list[tuple[str, tuple]]:
        return [
            q
            for q in self.queries
            if q[0].lstrip().upper().startswith("UPDATE PRACTICE_DOCUMENT_REQUESTS")
        ]

    @asynccontextmanager
    async def transaction(self):
        self.events.append("BEGIN")
        try:
            yield
        except BaseException:
            self.events.append("ROLLBACK")
            raise
        self.events.append("COMMIT")

    def _log(self, sql: str, args: tuple) -> None:
        self.queries.append((sql, args))
        self.events.append("SQL " + " ".join(sql.split())[:240])

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._log(sql, args)
        if "count(*)" in sql.lower():
            return self.total
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._log(sql, args)
        if "FROM practice_document_requests" in sql and "FOR UPDATE" in sql:
            return self.request
        if "FROM service_cards" in sql and "FOR UPDATE" in sql:
            return {"practice_type_id": args[0]} if self.card else None
        if "INSERT INTO service_card_documents" in sql:
            return None if self.conflict else {"id": 900}
        if sql.lstrip().upper().startswith("UPDATE PRACTICE_DOCUMENT_REQUESTS"):
            if "'pending'" in sql:
                return {"id": args[0]}
            return {
                "id": args[0],
                "decision": args[1],
                "decision_condition": args[2],
                "decided_by": args[3],
                "decided_at": self.decided_at,
            }
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self._log(sql, args)
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list:
        self._log(sql, args)
        if sql.lstrip().upper().startswith("DELETE FROM SERVICE_CARD_DOCUMENTS"):
            return [{"id": 900, "practice_type_id": 10}] if self.card_document else []
        if "FROM practice_document_requests" in sql:
            return self.rows
        return []


class FakePool:
    def __init__(self, world: World) -> None:
        self.world = world

    @asynccontextmanager
    async def acquire(self):
        self.world.acquires += 1
        yield self.world.conn


class World:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.conn = FakeConn(self.events)
        self.user = _user(ADMIN, "admin")
        self.acquires = 0

    def client(self) -> TestClient:
        from backend.app.routers.crm_practices import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_database_pool] = lambda: FakePool(self)
        return TestClient(app)

    def get(self, query: str = "") -> Any:
        return self.client().get(f"{BASE}/review{query}")

    def decide(self, body: dict[str, Any], request_id: int = 77) -> Any:
        return self.client().post(f"{BASE}/{request_id}/decision", json=body)

    def reopen(self, request_id: int = 77) -> Any:
        return self.client().post(f"{BASE}/{request_id}/reopen")

    def statements(self) -> str:
        return "\n".join(sql for sql, _ in self.conn.queries)


@pytest.fixture
def world() -> World:
    return World()


def _setting_fixture(name: str):
    @pytest.fixture
    def _fixture():
        missing = object()
        previous = settings.__dict__.get(name, missing)

        def _set(value: str) -> None:
            settings.__dict__[name] = value

        yield _set
        if previous is missing:
            settings.__dict__.pop(name, None)
        else:
            settings.__dict__[name] = previous

    return _fixture


reviewers = _setting_fixture("service_card_reviewer_emails")
seeding = _setting_fixture("service_cards_seeding_codes")


def _call(world: World, route: str) -> Any:
    if route == "list":
        return world.get()
    if route == "decision":
        return world.decide({"decision": "case_only"})
    return world.reopen()


ROUTES = ["list", "decision", "reopen"]


# ---------------------------------------------------------------- who may review


def test_a_crm_admin_by_role_may_use_the_three_routes(world: World, reviewers) -> None:
    reviewers("")
    world.user = _user(ADMIN, "admin")
    assert world.get().status_code == 200
    assert world.decide({"decision": "case_only"}).status_code == 200
    world.conn.request = {**world.conn.request, "decision": "case_only"}
    assert world.reopen().status_code == 200


@pytest.mark.parametrize(
    ("setting", "login"),
    [
        (f"{REVIEWER}:en", REVIEWER),
        ("  Reviewer@Example.TEST : id ,", REVIEWER),
        (REVIEWER, "  REVIEWER@example.test  "),
    ],
)
def test_a_member_in_the_setting_may_review_whatever_the_case_and_spaces(
    world: World, reviewers, setting: str, login: str
) -> None:
    reviewers(setting)
    world.user = _user(login, "member")
    assert world.get().status_code == 200
    assert world.decide({"decision": "case_only"}).status_code == 200


@pytest.mark.parametrize("role", ["client", "partner", "user", "zz_unknown_role", None])
@pytest.mark.parametrize("route", ROUTES)
def test_a_role_outside_the_team_is_403_even_with_a_listed_address(
    world: World, reviewers, route: str, role: str | None
) -> None:
    reviewers(f"{REVIEWER}:en")
    world.user = _user(REVIEWER, role)
    response = _call(world, route)
    assert response.status_code == 403
    assert response.json() == {"detail": "Card reviewers only"}
    assert world.acquires == 0 and world.conn.queries == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_member_not_in_the_setting_is_403(world: World, reviewers, route: str) -> None:
    reviewers(f"{REVIEWER}:en")
    world.user = _user(OUTSIDER, "member")
    response = _call(world, route)
    assert response.status_code == 403
    assert response.json() == {"detail": "Card reviewers only"}
    assert world.acquires == 0


@pytest.mark.parametrize("route", ROUTES)
def test_a_user_with_no_email_is_403(world: World, reviewers, route: str) -> None:
    reviewers(f"{REVIEWER}:en")
    world.user = _user(None, "member")
    response = _call(world, route)
    assert response.status_code == 403
    assert world.acquires == 0


@pytest.mark.parametrize("entry", [f"{REVIEWER}:xx", "reviewer@example", "reviewer@@example.test"])
def test_an_entry_the_setting_parser_skips_grants_nothing(
    world: World, reviewers, entry: str
) -> None:
    reviewers(entry)
    world.user = _user(REVIEWER, "member")
    response = world.get()
    assert response.status_code == 403
    assert world.acquires == 0


# ---------------------------------------------------------------- the list


def test_the_list_defaults_to_pending_first_page_of_fifty(world: World) -> None:
    response = world.get()
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["total"] == 1 and len(out["items"]) == 1
    (items_sql, items_args) = world.conn.sql_like("ORDER BY")[0]
    assert "pending" in items_sql or "pending" in items_args
    assert 50 in items_args and 0 in items_args
    assert "requested_at DESC, r.id DESC" in " ".join(items_sql.split()) or (
        "requested_at DESC, id DESC" in " ".join(items_sql.split())
    )


def test_the_strip_characters_are_exactly_what_str_strip_removes() -> None:
    """Contract 3.a: "trimmed" is what Python's str.strip() removes, no more and no less."""
    import sys

    from backend.services.crm import document_requests

    python_whitespace = {chr(code) for code in range(sys.maxunicode + 1) if chr(code).isspace()}
    assert set(document_requests.STRIP_CHARACTERS) == python_whitespace
    assert len(document_requests.STRIP_CHARACTERS) == len(python_whitespace)
    assert ("x" + document_requests.STRIP_CHARACTERS).strip() == "x"


def test_both_pending_queries_receive_the_strip_characters_as_a_parameter(world: World) -> None:
    from backend.services.crm import document_requests

    assert world.get().status_code == 200
    (count_sql, count_args) = world.conn.sql_like("count(*)")[0]
    (items_sql, items_args) = world.conn.sql_like("ORDER BY")[0]
    assert count_args == (document_requests.STRIP_CHARACTERS,)
    assert items_args == (50, 0, document_requests.STRIP_CHARACTERS)
    assert "btrim(coalesce(p.status, ''), $1::text)" in count_sql
    assert "btrim(coalesce(p.status, ''), $3::text)" in items_sql
    assert "E'" not in count_sql + items_sql  # no escape string in SQL


def test_the_decided_queries_carry_no_strip_parameter(world: World) -> None:
    assert world.get("?state=decided&limit=20&offset=40").status_code == 200
    (count_sql, count_args) = world.conn.sql_like("count(*)")[0]
    (items_sql, items_args) = world.conn.sql_like("ORDER BY")[0]
    assert count_args == () and items_args == (20, 40)
    assert "btrim" not in count_sql + items_sql


def test_the_decided_list_is_newest_decision_first_with_its_paging(world: World) -> None:
    world.conn.rows = [_list_row(decision="case_only", decided_by=ADMIN, decided_at=WHEN)]
    response = world.get("?state=decided&limit=20&offset=40")
    assert response.status_code == 200, response.text
    (items_sql, items_args) = world.conn.sql_like("ORDER BY")[0]
    assert "decided_at DESC" in items_sql
    assert 20 in items_args and 40 in items_args
    item = response.json()["items"][0]
    assert item["decision"] == "case_only" and item["decided_at"] == WHEN.isoformat()


@pytest.mark.parametrize(
    "query", ["?state=other", "?limit=0", "?limit=101", "?offset=-1", f"?offset={2**63}"]
)
def test_a_malformed_query_is_422_and_takes_no_connection(world: World, query: str) -> None:
    response = world.get(query)
    assert response.status_code == 422
    assert world.acquires == 0


def test_an_item_has_exactly_the_keys_of_the_contract(world: World) -> None:
    item = world.get().json()["items"][0]
    assert set(item) == ITEM_KEYS
    assert item["requested_at"] == WHEN.isoformat()
    assert item["decided_at"] is None and item["seen_count"] == 2
    assert item["service_name"] == "Zz Service"


@pytest.mark.parametrize(
    ("over", "state"),
    [
        ({"has_card": False, "on_card": False, "type_code": None}, "no_card"),
        ({"has_card": True, "on_card": False}, "can_add"),
        ({"has_card": True, "on_card": True}, "on_card"),
        ({"has_card": True, "on_card": True, "card_active": False}, "on_card"),
    ],
)
def test_card_state_of_each_case(world: World, over: dict[str, Any], state: str) -> None:
    world.conn.rows = [_list_row(**over)]
    assert world.get().json()["items"][0]["card_state"] == state


@pytest.mark.parametrize(
    ("codes", "over", "live"),
    [
        ("zz_code", {}, True),
        ("*", {}, True),
        ("zz_code", {"card_active": False}, False),
        ("zz_code", {"has_card": False, "type_code": None}, False),
        ("zz_other", {}, False),
        ("", {}, False),
    ],
)
def test_card_live_needs_card_active_and_armed_code(
    world: World, seeding, codes: str, over: dict[str, Any], live: bool
) -> None:
    seeding(codes)
    world.conn.rows = [_list_row(**over)]
    assert world.get().json()["items"][0]["card_live"] is live


def test_the_list_selects_no_client_datum(world: World) -> None:
    response = world.get()
    assert "Zz Fixture Clientname" not in response.text
    for key in response.json()["items"][0]:
        assert "client_name" not in key and key != "client_id"
    assert not re.search(r"\bclients\b", world.statements(), re.IGNORECASE)


# ---------------------------------------------------------------- decide: case_only


def test_case_only_updates_the_request_and_never_touches_the_card(world: World) -> None:
    response = world.decide({"decision": "case_only"})
    assert response.status_code == 200, response.text
    out = response.json()
    assert set(out) == {"id", "decision", "decision_condition", "decided_by", "decided_at"}
    assert out["id"] == 77 and out["decision"] == "case_only"
    assert out["decision_condition"] is None and out["decided_by"] == ADMIN
    assert out["decided_at"] == WHEN.isoformat()
    assert len(world.conn.request_updates()) == 1
    assert "service_card_documents" not in world.statements()
    assert world.events.count("BEGIN") == 1 and world.events[-1] == "COMMIT"


@pytest.mark.parametrize(
    "extra", [{"condition": "only for married couples"}, {"client_text": "A text"}]
)
def test_case_only_with_a_condition_or_a_client_text_is_422(world: World, extra: dict) -> None:
    response = world.decide({"decision": "case_only", **extra})
    assert response.status_code == 422
    assert world.acquires == 0


def test_an_unknown_decision_value_is_422(world: World) -> None:
    assert world.decide({"decision": "pending"}).status_code == 422
    assert world.decide({"decision": "maybe"}).status_code == 422


# ---------------------------------------------------------------- decide: added_to_card


def _subsequence(world: World, fragments: list[str]) -> list[int]:
    """Indexes in world.events of the first statement for each fragment, in order."""
    found: list[int] = []
    start = 0
    for fragment in fragments:
        for index in range(start, len(world.events)):
            if fragment in world.events[index]:
                found.append(index)
                start = index + 1
                break
        else:
            pytest.fail(f"statement not found in order: {fragment!r}\n{world.events}")
    return found


def test_added_to_card_issues_the_statements_in_lock_order_in_one_transaction(
    world: World,
) -> None:
    response = world.decide(
        {"decision": "added_to_card", "condition": "  only married couples ", "client_text": " T "}
    )
    assert response.status_code == 200, response.text
    found = _subsequence(
        world,
        [
            "FROM practice_document_requests WHERE id = $1 FOR UPDATE",
            "FROM service_cards WHERE practice_type_id = $1 FOR UPDATE",
            "INSERT INTO service_card_documents",
            "UPDATE practice_document_requests",
            "UPDATE service_cards",
        ],
    )
    begin = world.events.index("BEGIN")
    commit = world.events.index("COMMIT")
    assert begin < found[0] and found[-1] < commit
    assert world.events.count("BEGIN") == 1 and "ROLLBACK" not in world.events


def test_the_inserted_card_row_carries_the_decided_values(world: World) -> None:
    world.user = _user(ADMIN, "admin")
    world.decide(
        {"decision": "added_to_card", "condition": "  only married couples ", "client_text": " T "}
    )
    (sql, args) = world.conn.sql_like("INSERT INTO service_card_documents")[0]
    flat = " ".join(sql.split())
    assert "ON CONFLICT (practice_type_id, document_type) DO NOTHING" in flat
    assert "'client'" in flat and "'fresh'" in flat and "'request'" in flat
    assert "true" in flat.lower() and "RETURNING id" in flat
    assert "sort_order" in flat and "MAX(sort_order)" in flat.replace("max(", "MAX(")
    assert 10 in args and "proof_of_accommodation" in args and TYPED_LABEL in args
    assert "T" in args and "only married couples" in args and ADMIN in args and 77 in args
    assert TYPED_TEXT not in args


def test_without_a_client_text_the_card_row_takes_the_one_of_the_request(world: World) -> None:
    response = world.decide({"decision": "added_to_card"})
    assert response.status_code == 200, response.text
    (_, args) = world.conn.sql_like("INSERT INTO service_card_documents")[0]
    assert TYPED_TEXT in args and None in args


def test_the_decision_answer_is_built_from_the_update(world: World) -> None:
    out = world.decide({"decision": "added_to_card", "condition": "  when X  "}).json()
    assert out == {
        "id": 77,
        "decision": "added_to_card",
        "decision_condition": "when X",
        "decided_by": ADMIN,
        "decided_at": WHEN.isoformat(),
    }


def test_the_answer_is_built_before_the_commit(world: World) -> None:
    """Contract 3.b: nothing that can fail runs after the commit. A value the answer cannot
    render rolls the decision back; it never answers 5xx for a decision already written."""
    world.conn.decided_at = object()  # no isoformat()
    response = world.decide({"decision": "case_only"})
    assert response.status_code >= 500
    assert "ROLLBACK" in world.events
    assert "COMMIT" not in world.events


@pytest.mark.parametrize("route", ["decision", "reopen"])
def test_a_request_id_past_bigint_is_422_and_takes_no_connection(world: World, route: str) -> None:
    too_big = 2**63
    if route == "decision":
        response = world.decide({"decision": "case_only"}, too_big)
    else:
        response = world.reopen(too_big)
    assert response.status_code == 422
    assert world.acquires == 0


def test_a_blank_condition_counts_as_absent(world: World) -> None:
    response = world.decide({"decision": "added_to_card", "condition": "   "})
    assert response.status_code == 200, response.text
    assert response.json()["decision_condition"] is None


@pytest.mark.parametrize(
    ("extra", "ok"),
    [
        ({"condition": "c" * 500}, True),
        ({"condition": "c" * 501}, False),
        ({"client_text": "   "}, False),
        ({"client_text": "t" * 2000}, True),
        ({"client_text": "t" * 2001}, False),
    ],
)
def test_the_length_rules_of_the_body(world: World, extra: dict, ok: bool) -> None:
    response = world.decide({"decision": "added_to_card", **extra})
    assert response.status_code == (200 if ok else 422), response.text


# ---------------------------------------------------------------- the refusals


def _no_update_of_the_request(world: World) -> None:
    assert world.conn.request_updates() == []
    assert "ROLLBACK" in world.events or world.events.count("COMMIT") == 0


def test_an_unknown_request_is_404_with_its_code(world: World) -> None:
    world.conn.request = None
    response = world.decide({"decision": "case_only"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "request_not_found"
    assert response.json()["detail"]["message"]
    _no_update_of_the_request(world)


def test_a_decided_request_is_409_already_decided(world: World) -> None:
    world.conn.request = {**world.conn.request, "decision": "case_only"}
    response = world.decide({"decision": "added_to_card"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_decided"
    _no_update_of_the_request(world)
    assert world.conn.sql_like("INSERT INTO service_card_documents") == []


def test_no_type_is_409_no_card_and_the_card_is_not_even_looked_up(world: World) -> None:
    world.conn.request = {**world.conn.request, "practice_type_id": None}
    response = world.decide({"decision": "added_to_card"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "no_card"
    assert world.conn.sql_like("FROM service_cards") == []
    _no_update_of_the_request(world)


def test_a_type_without_a_card_is_409_no_card_and_a_card_is_never_created(world: World) -> None:
    world.conn.card = False
    response = world.decide({"decision": "added_to_card"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "no_card"
    assert world.conn.sql_like("INSERT INTO service_cards") == []
    assert world.conn.sql_like("INSERT INTO service_card_documents") == []
    _no_update_of_the_request(world)


def test_a_conflict_on_insert_is_409_already_on_card(world: World) -> None:
    world.conn.conflict = True
    response = world.decide({"decision": "added_to_card"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_on_card"
    _no_update_of_the_request(world)
    assert world.conn.sql_like("UPDATE service_cards") == []


# ---------------------------------------------------------------- reopen


def test_a_reopen_from_case_only_deletes_nothing(world: World) -> None:
    world.conn.request = {**world.conn.request, "decision": "case_only"}
    response = world.reopen()
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 77, "decision": "pending", "card_document_removed": False}
    assert world.conn.sql_like("DELETE") == []
    assert len(world.conn.request_updates()) == 1


def test_a_reopen_from_added_to_card_locks_the_card_before_the_delete(world: World) -> None:
    world.conn.request = {**world.conn.request, "decision": "added_to_card"}
    response = world.reopen()
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 77, "decision": "pending", "card_document_removed": True}
    found = _subsequence(
        world,
        [
            "FROM practice_document_requests WHERE id = $1 FOR UPDATE",
            "FROM service_cards WHERE practice_type_id = $1 FOR UPDATE",
            "DELETE FROM service_card_documents",
            "UPDATE service_cards",
            "UPDATE practice_document_requests",
        ],
    )
    assert world.events.index("BEGIN") < found[0] and found[-1] < world.events.index("COMMIT")
    (sql, args) = world.conn.sql_like("DELETE FROM service_card_documents")[0]
    flat = " ".join(sql.split())
    assert "source = 'request'" in flat and "source_request_id = $1" in flat
    assert args == (77,)


def test_a_reopen_whose_card_row_is_already_gone_still_succeeds(world: World) -> None:
    world.conn.request = {**world.conn.request, "decision": "added_to_card"}
    world.conn.card_document = False
    out = world.reopen().json()
    assert out == {"id": 77, "decision": "pending", "card_document_removed": False}
    assert world.conn.sql_like("UPDATE service_cards") == []


def test_a_reopen_of_a_pending_request_is_409_not_decided(world: World) -> None:
    response = world.reopen()
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_decided"
    assert world.conn.request_updates() == [] and world.conn.sql_like("DELETE") == []


def test_a_reopen_of_an_unknown_request_is_404(world: World) -> None:
    world.conn.request = None
    response = world.reopen()
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "request_not_found"


def test_the_reopen_sets_the_request_back_to_pending_and_clears_who_decided(
    world: World,
) -> None:
    world.conn.request = {**world.conn.request, "decision": "case_only"}
    world.reopen()
    (sql, _) = world.conn.request_updates()[0]
    flat = " ".join(sql.split())
    assert "decision = 'pending'" in flat
    for column in ("decision_condition", "decided_by", "decided_at"):
        assert f"{column} = NULL" in flat


# ---------------------------------------------------------------- what no route may write


def test_no_route_writes_a_practice_or_its_slots(world: World) -> None:
    world.get()
    world.decide({"decision": "added_to_card", "condition": "c", "client_text": "t"})
    world.conn.request = {**world.conn.request, "decision": "added_to_card"}
    world.reopen()
    world.conn.request = {**world.conn.request, "decision": "pending"}
    world.decide({"decision": "case_only"})
    assert world.conn.queries, "no statement was issued at all"
    for sql, _ in world.conn.queries:
        verb = sql.lstrip().split()[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            assert "practice_required_documents" not in sql
            assert not re.search(r"\b(UPDATE|INSERT INTO|DELETE FROM)\s+practices\b", sql)
    assert "SET client_text" not in world.statements()
    assert "SET label" not in world.statements()


# ---------------------------------------------------------------- log lines


def test_no_log_line_carries_an_address_a_label_a_reason_or_a_typed_text(
    world: World, reviewers, caplog: pytest.LogCaptureFixture
) -> None:
    private = "private.reviewer@example.test"
    reviewers(f"{private}:en")
    world.user = _user(private, "member")
    typed = "Zz typed client text from the reviewer"
    condition = "Zz typed condition from the reviewer"
    with caplog.at_level(logging.DEBUG):
        world.get()
        world.decide({"decision": "added_to_card", "condition": condition, "client_text": typed})
        world.conn.request = {**world.conn.request, "decision": "added_to_card"}
        world.reopen()
        world.conn.request = None
        world.decide({"decision": "case_only"})
    text = "\n".join(
        record.getMessage() + " " + str(record.args) + " " + (record.exc_text or "")
        for record in caplog.records
    )
    assert any("77" in record.getMessage() for record in caplog.records), "nothing was logged"
    for secret in (private, TYPED_LABEL, TYPED_REASON, TYPED_TEXT, typed, condition):
        assert secret not in text


# ---------------------------------------------------------------- the email of the first cut


def test_the_email_carries_both_links_each_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.services.crm import document_requests

    monkeypatch.setattr(
        document_requests,
        "KITA_PRACTICE_URL",
        'https://k.example.test/p/{id}?a=1&b="2"',
        raising=False,
    )
    monkeypatch.setattr(
        document_requests, "KITA_REVIEW_URL", 'https://k.example.test/r?a=1&b="2"', raising=False
    )
    for lang, label in (("en", "Decide in Kita"), ("id", "Putuskan di Kita")):
        _, body = document_requests.build_email(
            lang=lang,
            document_label="A label",
            service_name="A service",
            practice_id=5,
            requested_by="asker@example.test",
            requested_at=WHEN,
            client_text="text",
            reason="reason",
            seen_count=1,
        )
        assert 'href="https://k.example.test/p/5?a=1&amp;b=&quot;2&quot;"' in body
        assert 'href="https://k.example.test/r?a=1&amp;b=&quot;2&quot;"' in body
        assert f">{label}</a>" in body
        assert body.index("https://k.example.test/p/5") < body.index("https://k.example.test/r")


def test_the_review_link_constant_is_the_page_of_the_review(monkeypatch) -> None:
    from backend.services.crm import document_requests

    assert (
        getattr(document_requests, "KITA_REVIEW_URL", None)
        == "https://kita.balizero.com/review/document-requests"
    )
