"""The practice row says where its request stands; its client text can be corrected (V2, third cut).

Two routes of the practices router, driven through a small FastAPI app that mounts the real router
with a recording fake connection (as test_document_request_review.py does):

    GET   /api/crm/practices/{id}/document-requests
    PATCH /api/crm/practices/{id}/document-requests/{request_id}

The real tables are covered in backend/tests/db/test_document_request_slot_state_pg.py. Names and
addresses here are invented.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool

CREATOR = "creator@example.test"
ASSIGNEE = "assignee@example.test"
OUTSIDER = "outsider@example.test"
ADMIN = "boss@example.test"
TYPED_TEXT = "Zz corrected text typed by a consultant"
WHEN = datetime(2026, 11, 28, 9, 30, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 28, 10, 45, tzinfo=timezone.utc)
ITEM_KEYS = {"id", "required_document_id", "state", "requested_by", "requested_at"}
DOCUMENT_KEYS = {
    "id",
    "practice_id",
    "document_type",
    "document_label",
    "description",
    "is_required",
    "uploaded_by_client",
    "uploaded_file_id",
    "uploaded_at",
    "client_notes",
    "team_member_notes",
    "status",
    "created_at",
    "updated_at",
}
FORBIDDEN_SET_COLUMNS = (
    "decision",
    "decided_by",
    "decided_at",
    "decision_condition",
    "reviewers_notified_at",
    "client_notification_sent",
)


def _user(email: str, role: str = "team") -> dict[str, Any]:
    return {"id": f"id-{email}", "email": email, "role": role, "full_name": "Fixture"}


def _request_row(**over: Any) -> dict[str, Any]:
    row = {"id": 77, "practice_id": 7, "required_document_id": 55, "decision": "pending"}
    row.update(over)
    return row


def _item_row(request_id: int, decision: str, slot: int | None) -> dict[str, Any]:
    return {
        "id": request_id,
        "required_document_id": slot,
        "decision": decision,
        "requested_by": CREATOR,
        "requested_at": WHEN,
    }


class FakeConn:
    """Records every statement, tracks whether a transaction is open, answers by SQL shape."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.queries: list[tuple[str, tuple]] = []
        self.in_tx = False
        self.tx_statements: list[str] = []
        self.visible = True
        self.practice_exists = True
        self.status = "inquiry"
        self.created_by = CREATOR
        self.assigned_to = ASSIGNEE
        self.request: dict[str, Any] | None = _request_row()
        self.slot_status: str | None = "pending"  # None: the slot lock finds nothing
        self.rows: list[dict[str, Any]] = []

    def sql_like(self, fragment: str) -> list[tuple[str, tuple]]:
        return [q for q in self.queries if fragment in q[0]]

    def writes(self) -> list[tuple[str, tuple]]:
        return [
            q
            for q in self.queries
            if q[0].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]

    @asynccontextmanager
    async def transaction(self):
        self.events.append("BEGIN")
        self.in_tx = True
        try:
            yield
        except BaseException:
            self.events.append("ROLLBACK")
            raise
        finally:
            self.in_tx = False
        self.events.append("COMMIT")

    def _log(self, sql: str, args: tuple) -> None:
        self.queries.append((sql, args))
        if self.in_tx:
            self.tx_statements.append(sql)
        self.events.append("SQL " + " ".join(sql.split())[:90])

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._log(sql, args)
        if "FROM practices p WHERE p.id" in sql:
            return 1 if self.visible else None
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._log(sql, args)
        flat = " ".join(sql.split())
        if "created_by, assigned_to" in flat:
            if not self.practice_exists:
                return None
            return {
                "id": args[0],
                "created_by": self.created_by,
                "assigned_to": self.assigned_to,
            }
        if "FROM practices" in flat and "FOR UPDATE" in flat:
            return {"status": self.status} if self.practice_exists else None
        if "FROM practice_document_requests" in flat and "FOR UPDATE" in flat:
            request = self.request
            if request is None or (args[0], args[1]) != (request["practice_id"], request["id"]):
                return None
            return request
        if "FROM practice_required_documents" in flat and "FOR UPDATE" in flat:
            if self.slot_status is None or args[1] is None:
                return None
            return {"status": self.slot_status}
        if flat.upper().startswith("UPDATE PRACTICE_REQUIRED_DOCUMENTS"):
            return {
                "id": args[1],
                "practice_id": args[0],
                "document_type": "proof_of_accommodation",
                "document_label": "Proof of accommodation",
                "description": args[2],
                "is_required": True,
                "uploaded_by_client": self.slot_status == "uploaded",
                "uploaded_file_id": None,
                "uploaded_at": None,
                "client_notes": None,
                "team_member_notes": None,
                "status": self.slot_status,
                "created_at": WHEN,
                "updated_at": LATER,
            }
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self._log(sql, args)
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list:
        self._log(sql, args)
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
        self.user = _user(CREATOR)
        self.acquires = 0
        self.cache = AsyncMock()

    def client(self) -> TestClient:
        from backend.app.routers.crm_practices import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_database_pool] = lambda: FakePool(self)
        return TestClient(app)

    def read(self, practice_id: int = 7) -> Any:
        return self.client().get(f"/api/crm/practices/{practice_id}/document-requests")

    def correct(self, body: Any, practice_id: int = 7, request_id: int = 77) -> Any:
        mails = AsyncMock(side_effect=AssertionError("a correction sends no email"))
        with (
            patch("backend.services.crm.document_requests.notify_reviewers", new=mails),
            patch(
                "backend.services.crm.document_requests.notify_reviewers_after_commit", new=mails
            ),
            patch("backend.app.routers.crm_practices.notify_reviewers_after_commit", new=mails),
            patch("backend.app.routers.crm_practices.invalidate_cache", new=self.cache),
        ):
            return self.client().patch(
                f"/api/crm/practices/{practice_id}/document-requests/{request_id}", json=body
            )


@pytest.fixture
def world() -> World:
    return World()


def text_body(text: str = TYPED_TEXT) -> dict[str, Any]:
    return {"client_text": text}


def tx_between(world: World) -> list[str]:
    start = world.events.index("BEGIN")
    return world.events[start:]


# ------------------------------------------------------------------------------- the read


@pytest.mark.parametrize("state", ["pending", "added_to_card", "case_only"])
def test_read_item_has_exactly_the_five_keys_for_each_state(world: World, state: str) -> None:
    world.conn.rows = [_item_row(77, state, 55)]
    response = world.read()
    assert response.status_code == 200, response.text
    (item,) = response.json()
    assert set(item) == ITEM_KEYS
    assert item["state"] == state
    assert item["id"] == 77 and item["required_document_id"] == 55
    assert item["requested_by"] == CREATOR
    assert item["requested_at"] == WHEN.isoformat()


def test_read_empty_list(world: World) -> None:
    response = world.read()
    assert response.status_code == 200
    assert response.json() == []


def test_read_returns_a_request_whose_slot_is_gone(world: World) -> None:
    world.conn.rows = [_item_row(78, "pending", None), _item_row(77, "case_only", 55)]
    response = world.read()
    assert response.status_code == 200
    items = response.json()
    assert [i["id"] for i in items] == [78, 77]
    assert items[0]["required_document_id"] is None


def test_read_issues_one_statement_after_the_visibility_check(world: World) -> None:
    world.conn.rows = [_item_row(77, "pending", 55)]
    assert world.read().status_code == 200
    visible = [i for i, (sql, _) in enumerate(world.conn.queries) if "FROM practices p" in sql]
    mine = [
        i for i, (sql, _) in enumerate(world.conn.queries) if "practice_document_requests" in sql
    ]
    assert visible and len(mine) == 1 and mine[0] > visible[-1]
    sql, args = world.conn.queries[mine[0]]
    flat = " ".join(sql.split())
    assert "WHERE practice_id = $1" in flat
    assert "ORDER BY id DESC" in flat and "LIMIT 200" in flat
    assert args == (7,)
    assert "clients" not in flat and "practice_required_documents" not in flat
    assert not re.search(r"\bJOIN\b", flat, re.IGNORECASE)
    assert not re.search(r"client_text|reason|label", flat)
    assert world.conn.writes() == []


def test_read_refused_by_the_visibility_check_touches_no_request(world: World) -> None:
    world.conn.visible = False
    response = world.read()
    assert response.status_code == 404
    assert world.conn.sql_like("practice_document_requests") == []


def test_read_rejects_a_practice_id_of_zero(world: World) -> None:
    assert world.read(practice_id=0).status_code == 422
    assert world.conn.writes() == [] and "BEGIN" not in world.events


# ------------------------------------------------------------------- the correction: who


@pytest.mark.parametrize(
    ("email", "role"), [(CREATOR, "team"), (ASSIGNEE, "team"), (ADMIN, "admin")]
)
def test_admin_creator_and_assignee_may_correct(world: World, email: str, role: str) -> None:
    world.user = _user(email, role)
    response = world.correct(text_body())
    assert response.status_code == 200, response.text
    assert len(world.conn.sql_like("UPDATE practice_required_documents")) == 1


def test_another_member_gets_403_and_no_transaction_is_opened(world: World) -> None:
    world.user = _user(OUTSIDER)
    response = world.correct(text_body())
    assert response.status_code == 403
    assert world.conn.sql_like("created_by, assigned_to"), "the ownership check never ran"
    assert "BEGIN" not in world.events
    assert world.conn.writes() == []


def test_a_member_who_cannot_see_the_practice_gets_404(world: World) -> None:
    world.conn.visible = False
    response = world.correct(text_body())
    assert response.status_code == 404
    assert "BEGIN" not in world.events and world.conn.writes() == []


def test_a_practice_that_does_not_exist_is_404(world: World) -> None:
    world.conn.practice_exists = False
    response = world.correct(text_body())
    assert response.status_code == 404
    assert response.json()["detail"] == "Practice not found"
    assert "BEGIN" not in world.events and world.conn.writes() == []


# ----------------------------------------------------------------- the correction: order


def test_one_transaction_issues_the_five_statements_in_order(world: World) -> None:
    response = world.correct(text_body("  " + TYPED_TEXT + "  "))
    assert response.status_code == 200, response.text
    assert world.events.count("BEGIN") == 1 and world.events.count("COMMIT") == 1
    assert "ROLLBACK" not in world.events
    inside = tx_between(world)
    wanted = [
        "FROM practices",
        "FROM practice_document_requests",
        "FROM practice_required_documents",
        "UPDATE practice_required_documents",
        "UPDATE practice_document_requests",
    ]
    cursor = 0
    for event in inside:
        if cursor < len(wanted) and wanted[cursor] in event:
            cursor += 1
    assert cursor == len(wanted), inside
    assert inside[-1] == "COMMIT"


def test_the_locks_are_for_update_and_the_values_written_are_the_stripped_text(
    world: World,
) -> None:
    assert world.correct(text_body("  " + TYPED_TEXT + "  ")).status_code == 200
    (practice_lock,) = [
        q for q in world.conn.queries if "FROM practices" in q[0] and "FOR UPDATE" in q[0]
    ]
    assert practice_lock[1] == (7,)
    (request_lock,) = [q for q in world.conn.queries if "FROM practice_document_requests" in q[0]]
    assert "FOR UPDATE" in request_lock[0] and request_lock[1] == (7, 77)
    (slot_lock,) = [
        q
        for q in world.conn.queries
        if "FROM practice_required_documents" in q[0] and "FOR UPDATE" in q[0]
    ]
    assert slot_lock[1] == (7, 55)
    (slot_sql, slot_args) = world.conn.sql_like("UPDATE practice_required_documents")[0]
    assert slot_args == (7, 55, TYPED_TEXT)
    assert "RETURNING *" in slot_sql
    (request_sql, request_args) = world.conn.sql_like("UPDATE practice_document_requests")[0]
    assert request_args == (77, TYPED_TEXT)
    assert "client_text" in request_sql


def test_the_slot_update_sets_description_and_updated_at(world: World) -> None:
    assert world.correct(text_body()).status_code == 200
    sql = " ".join(world.conn.sql_like("UPDATE practice_required_documents")[0][0].split())
    clause = re.search(r"\bSET\b(.*?)\bWHERE\b", sql, re.IGNORECASE | re.DOTALL)
    assert clause is not None
    assert "description" in clause.group(1)
    assert "updated_at" in clause.group(1)
    assert "NOW()" in clause.group(1).upper()
    assert "practice_id" in sql.split("WHERE", 1)[1]


# -------------------------------------------------------------- the correction: refusals


def assert_nothing_written(world: World) -> None:
    assert world.conn.sql_like("UPDATE practice_required_documents") == []
    assert world.conn.sql_like("UPDATE practice_document_requests") == []
    assert world.conn.writes() == []


def test_a_request_of_another_practice_is_404_request_not_found(world: World) -> None:
    response = world.correct(text_body(), practice_id=8)
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "request_not_found" and detail["message"]
    assert_nothing_written(world)


def test_an_unknown_request_is_404_request_not_found(world: World) -> None:
    world.conn.request = None
    response = world.correct(text_body())
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "request_not_found"
    assert_nothing_written(world)


@pytest.mark.parametrize("status", ["completed", " Cancelled ", "CANCELED"])
def test_a_closed_practice_is_409_practice_closed(world: World, status: str) -> None:
    world.conn.status = status
    response = world.correct(text_body())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "practice_closed" and detail["message"]
    assert_nothing_written(world)
    assert "ROLLBACK" in world.events


def test_a_request_with_no_slot_is_409_slot_withdrawn(world: World) -> None:
    """The column is nullable: with no slot id there is nothing to lock, so no statement at all
    names the slot table (the fake's slot is left `pending`, to prove it is never reached)."""
    world.conn.request = _request_row(required_document_id=None)
    response = world.correct(text_body())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "slot_withdrawn"
    assert not [sql for sql, _ in world.conn.queries if "practice_required_documents" in sql]
    assert_nothing_written(world)


def test_a_deleted_slot_is_409_slot_withdrawn(world: World) -> None:
    world.conn.slot_status = None
    response = world.correct(text_body())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "slot_withdrawn" and detail["message"]
    assert_nothing_written(world)


@pytest.mark.parametrize("status", ["uploaded", "verified"])
def test_an_answered_slot_is_409_slot_answered(world: World, status: str) -> None:
    world.conn.slot_status = status
    response = world.correct(text_body())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "slot_answered" and detail["message"]
    assert_nothing_written(world)


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_a_pending_or_rejected_slot_is_corrected(world: World, status: str) -> None:
    world.conn.slot_status = status
    response = world.correct(text_body())
    assert response.status_code == 200, response.text
    assert response.json()["document"]["status"] == status
    assert len(world.conn.sql_like("UPDATE practice_required_documents")) == 1
    assert len(world.conn.sql_like("UPDATE practice_document_requests")) == 1


# ------------------------------------------------------------- the correction: validation


def test_a_blank_text_is_422(world: World) -> None:
    assert world.correct(text_body("   \n ")).status_code == 422
    assert world.conn.writes() == [] and "BEGIN" not in world.events


def test_a_missing_text_is_422(world: World) -> None:
    assert world.correct({}).status_code == 422


def test_a_text_of_2001_characters_is_422_and_2000_is_accepted(world: World) -> None:
    assert world.correct(text_body("x" * 2001)).status_code == 422
    assert world.conn.writes() == [] and "BEGIN" not in world.events
    assert world.correct(text_body("x" * 2000)).status_code == 200


def test_surrounding_whitespace_is_stripped(world: World) -> None:
    response = world.correct(text_body("\t " + TYPED_TEXT + " \n"))
    assert response.status_code == 200
    assert response.json()["document"]["description"] == TYPED_TEXT


def test_an_extra_key_is_ignored(world: World) -> None:
    response = world.correct({"client_text": TYPED_TEXT, "label": "ignored", "decision": "x"})
    assert response.status_code == 200
    assert world.conn.sql_like("UPDATE practice_document_requests")[0][1] == (77, TYPED_TEXT)


def test_a_request_id_of_two_to_the_63_is_422(world: World) -> None:
    assert world.correct(text_body(), request_id=2**63).status_code == 422
    assert world.conn.writes() == [] and "BEGIN" not in world.events


def test_a_request_id_of_zero_is_422(world: World) -> None:
    assert world.correct(text_body(), request_id=0).status_code == 422


# ------------------------------------------------------------- what the correction must not do


def test_no_statement_names_the_card_tables(world: World) -> None:
    world.conn.rows = [_item_row(77, "added_to_card", 55)]
    world.read()
    world.correct(text_body())
    for sql, _ in world.conn.queries:
        assert "service_cards" not in sql and "service_card_documents" not in sql


def test_no_statement_writes_practices_and_inside_the_transaction_only_the_lock_names_it(
    world: World,
) -> None:
    assert world.correct(text_body()).status_code == 200
    for sql, _ in world.conn.queries:
        flat = " ".join(sql.split())
        assert not re.search(r"(INSERT INTO|UPDATE|DELETE FROM)\s+practices\b", flat, re.IGNORECASE)
    naming = [s for s in world.conn.tx_statements if re.search(r"\bpractices\b", s)]
    assert len(naming) == 1
    assert "FOR UPDATE" in naming[0] and naming[0].lstrip().upper().startswith("SELECT")
    assert world.conn.tx_statements, "the fake never saw the transaction"


def test_no_set_clause_names_a_decision_or_notification_column(world: World) -> None:
    assert world.correct(text_body()).status_code == 200
    for sql, _ in world.conn.writes():
        flat = " ".join(sql.split())
        clause = re.search(r"\bSET\b(.*?)(\bWHERE\b|$)", flat, re.IGNORECASE | re.DOTALL)
        assert clause is not None
        for column in FORBIDDEN_SET_COLUMNS:
            assert column not in clause.group(1), (column, flat)


def test_only_the_two_rows_are_written_and_no_cache_or_mail_side_effect(world: World) -> None:
    assert world.correct(text_body()).status_code == 200
    tables = sorted(
        re.match(r"\s*UPDATE\s+(\w+)", sql, re.IGNORECASE).group(1)
        for sql, _ in world.conn.writes()
    )
    assert tables == ["practice_document_requests", "practice_required_documents"]
    assert world.acquires == 2  # the visibility dependency, then the handler


def test_a_text_equal_to_the_current_one_is_written_again_and_answers_200(world: World) -> None:
    for _ in range(2):
        assert world.correct(text_body()).status_code == 200
    assert len(world.conn.sql_like("UPDATE practice_required_documents")) == 2


# ------------------------------------------------------------------------ the 200 body


def test_the_200_body_is_the_slot_with_the_new_text(world: World) -> None:
    response = world.correct(text_body("  " + TYPED_TEXT))
    assert response.status_code == 200, response.text
    out = response.json()
    assert set(out) == {"request_id", "document"}
    assert out["request_id"] == 77
    assert set(out["document"]) == DOCUMENT_KEYS
    assert out["document"]["description"] == TYPED_TEXT
    assert out["document"]["id"] == 55 and out["document"]["practice_id"] == 7
    assert out["document"]["updated_at"] == LATER.isoformat()
    assert out["document"]["uploaded_at"] is None


# ----------------------------------------------------------------------------- log lines


def test_no_log_line_carries_the_address_or_the_typed_text(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.user = _user(ADMIN, "admin")
    world.conn.rows = [_item_row(77, "pending", 55)]
    caplog.set_level(logging.DEBUG)
    assert world.read().status_code == 200
    assert world.correct(text_body()).status_code == 200
    world.conn.slot_status = "verified"
    assert world.correct(text_body()).status_code == 409
    world.conn.request = None
    assert world.correct(text_body()).status_code == 404
    mine = [r for r in caplog.records if "ocument request" in r.getMessage()]
    assert mine, "the correction logged nothing"
    for record in caplog.records:
        rendered = record.getMessage() + " " + str(record.__dict__)
        for forbidden in (ADMIN, CREATOR, ASSIGNEE, TYPED_TEXT):
            assert forbidden not in rendered, rendered


def test_an_unexpected_error_logs_the_class_name_only(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(sql: str, *args: Any) -> Any:
        raise RuntimeError(f"driver said {TYPED_TEXT} for {CREATOR}")

    world.conn.fetchrow = boom  # type: ignore[method-assign]
    caplog.set_level(logging.DEBUG)
    response = world.correct(text_body())
    assert response.status_code >= 500 or response.status_code == 400
    for record in caplog.records:
        rendered = record.getMessage() + " " + str(record.__dict__)
        assert TYPED_TEXT not in rendered and CREATOR not in rendered
