"""POST /api/crm/practices/{id}/document-requests (V2: ask the client for one more document).

The endpoint is driven through a small FastAPI app that mounts the real router with a recording
fake connection, so the permission cases, the validation, the 409 bodies and the order of the
transaction and the emails are exercised end to end. The real tables are covered in
backend/tests/db/test_document_requests_pg.py. Names and addresses here are invented.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.dependencies import get_current_user, get_database_pool

CREATOR = "creator@example.test"
ASSIGNEE = "assignee@example.test"
OUTSIDER = "outsider@example.test"
ADMIN = "boss@example.test"
CLIENT_NAME = "Zz Fixture Clientname"
EMAIL_SEND = "backend.app.services.internal_email.send_internal_email"
DOC_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
WHEN = datetime(2026, 11, 28, 9, 30, tzinfo=timezone.utc)


def _user(email: str, role: str = "team") -> dict[str, Any]:
    return {"id": f"id-{email}", "email": email, "role": role, "full_name": "Fixture"}


class FakeConn:
    """Records every statement and answers by the shape of the SQL."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.queries: list[tuple[str, tuple]] = []
        self.visible = True
        self.status = "inquiry"
        self.type_id: int | None = 10
        self.type_name: str | None = "Fixture Service"
        self.created_by = CREATOR
        self.assigned_to = ASSIGNEE
        self.duplicate = False
        self.recent = 3
        self.practice_id = 7

    def writes(self) -> list[tuple[str, tuple]]:
        return [q for q in self.queries if q[0].lstrip().upper().startswith(("INSERT", "UPDATE"))]

    def sql_like(self, fragment: str) -> list[tuple[str, tuple]]:
        return [q for q in self.queries if fragment in q[0]]

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
        self.events.append("SQL " + " ".join(sql.split())[:60])

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._log(sql, args)
        if "FROM practices p WHERE p.id" in sql:
            return 1 if self.visible else None
        if "FROM practice_types" in sql:
            return self.type_name
        if "count(*)" in sql.lower():
            return self.recent
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._log(sql, args)
        if "FOR UPDATE" in sql:
            return {"status": self.status, "practice_type_id": self.type_id}
        if "created_by, assigned_to" in sql:
            return {
                "id": self.practice_id,
                "created_by": self.created_by,
                "assigned_to": self.assigned_to,
                "client_name": CLIENT_NAME,
            }
        if "INSERT INTO practice_required_documents" in sql:
            if self.duplicate:
                return None
            return {
                "id": 55,
                "practice_id": args[0],
                "document_type": args[1],
                "document_label": args[2],
                "description": args[3],
                "is_required": True,
                "uploaded_by_client": False,
                "uploaded_file_id": None,
                "uploaded_at": None,
                "client_notes": None,
                "team_member_notes": None,
                "status": "pending",
                "created_at": WHEN,
                "updated_at": WHEN,
            }
        if "INSERT INTO practice_document_requests" in sql:
            return {"id": 77, "requested_at": WHEN}
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self._log(sql, args)
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list:
        self._log(sql, args)
        return []


class FakePool:
    """Counts every acquire of a request; can refuse from the Nth on (a pool that times out)."""

    def __init__(self, world: World) -> None:
        self.world = world

    @asynccontextmanager
    async def acquire(self):
        self.world.acquires += 1
        refuse_from = self.world.refuse_acquire_from
        if refuse_from is not None and self.world.acquires >= refuse_from:
            raise TimeoutError("pool exhausted")
        yield self.world.conn


class World:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.conn = FakeConn(self.events)
        self.user = _user(CREATOR)
        self.sent: list[dict[str, Any]] = []
        self.send_results: list[Any] = []
        self.cache = AsyncMock()
        self.acquires = 0
        self.refuse_acquire_from: int | None = None

    def client(self) -> TestClient:
        from backend.app.routers.crm_practices import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_database_pool] = lambda: FakePool(self)
        return TestClient(app)

    async def _send(self, **kwargs: Any) -> bool:
        self.events.append("EMAIL")
        self.sent.append(kwargs)
        result = self.send_results.pop(0) if self.send_results else True
        if isinstance(result, Exception):
            raise result
        return bool(result)

    def post(self, body: dict[str, Any], practice_id: int = 7) -> Any:
        with (
            patch(EMAIL_SEND, new=self._send),
            patch("backend.app.routers.crm_practices.invalidate_cache", new=self.cache),
        ):
            return self.client().post(
                f"/api/crm/practices/{practice_id}/document-requests", json=body
            )


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def reviewers():
    """Set Settings.service_card_reviewer_emails without validation (absent on older bases)."""
    missing = object()
    previous = settings.__dict__.get("service_card_reviewer_emails", missing)

    def _set(value: str) -> None:
        settings.__dict__["service_card_reviewer_emails"] = value

    yield _set
    if previous is missing:
        settings.__dict__.pop("service_card_reviewer_emails", None)
    else:
        settings.__dict__["service_card_reviewer_emails"] = previous


def body(**over: Any) -> dict[str, Any]:
    base = {
        "document_label": "Proof of accommodation",
        "client_text": "Your booking for the first nights.",
        "reason": "Stays with a friend, not in a hotel.",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- who may ask


@pytest.mark.parametrize(
    ("email", "role"), [(CREATOR, "team"), (ASSIGNEE, "team"), (ADMIN, "admin")]
)
def test_creator_assignee_and_admin_may_ask(world: World, reviewers, email: str, role: str) -> None:
    reviewers("")
    world.user = _user(email, role)
    # Visibility under the grant `own` follows assigned_to: give it explicitly to the creator.
    world.conn.visible = True
    response = world.post(body())
    assert response.status_code == 201, response.text
    assert len(world.conn.sql_like("INSERT INTO practice_document_requests")) == 1


def test_another_member_gets_403_and_nothing_is_written(world: World, reviewers) -> None:
    reviewers("a@example.test:en")
    world.user = _user(OUTSIDER)
    response = world.post(body())
    assert response.status_code == 403
    assert world.conn.sql_like("created_by, assigned_to"), "the ownership check never ran"
    assert world.conn.writes() == []
    assert world.sent == []


def test_a_member_who_cannot_see_the_practice_gets_404_and_nothing_is_written(
    world: World,
) -> None:
    world.conn.visible = False
    response = world.post(body())
    assert response.status_code == 404
    assert world.conn.sql_like("FROM practices p WHERE p.id"), "visible_process_target never ran"
    assert world.conn.writes() == []


# ---------------------------------------------------------------- the two 409s


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_a_closed_practice_is_409_practice_closed(world: World, reviewers, status: str) -> None:
    reviewers("a@example.test:en")
    world.conn.status = status
    response = world.post(body())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "practice_closed" and detail["message"]
    assert world.conn.writes() == []
    assert world.sent == []


def test_a_slot_of_the_same_type_is_409_already_on_practice(world: World, reviewers) -> None:
    reviewers("a@example.test:en")
    world.conn.duplicate = True
    response = world.post(body())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "already_on_practice" and detail["message"]
    assert world.conn.sql_like("INSERT INTO practice_document_requests") == []
    assert world.conn.sql_like("UPDATE practices") == []
    assert world.sent == []


# ---------------------------------------------------------------- happy path


def test_happy_path_writes_slot_and_request_in_one_transaction(world: World, reviewers) -> None:
    reviewers("")
    response = world.post(body(document_label="  Proof of accommodation  "))
    assert response.status_code == 201, response.text
    out = response.json()
    assert out["request_id"] == 77
    doc = out["document"]
    assert set(doc) == {
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
    assert doc["document_label"] == "Proof of accommodation"
    assert doc["description"] == "Your booking for the first nights."
    assert doc["is_required"] is True and doc["status"] == "pending"
    assert doc["document_type"] == "proof_of_accommodation"

    (slot_sql, slot_args) = world.conn.sql_like("INSERT INTO practice_required_documents")[0]
    assert "ON CONFLICT (practice_id, document_type) DO NOTHING" in slot_sql
    assert "true" in slot_sql.lower()
    assert slot_args == (
        7,
        "proof_of_accommodation",
        "Proof of accommodation",
        "Your booking for the first nights.",
        CREATOR,
    )
    (_, req_args) = world.conn.sql_like("INSERT INTO practice_document_requests")[0]
    assert req_args[0] == 7
    assert 55 in req_args and 10 in req_args and CREATOR in req_args
    assert "Stays with a friend, not in a hotel." in req_args
    assert "pending" in world.conn.sql_like("INSERT INTO practice_document_requests")[0][0]
    flag = world.conn.sql_like("UPDATE practices")[0][0]
    assert "client_notification_sent = FALSE" in flag

    begin = world.events.index("BEGIN")
    commit = world.events.index("COMMIT")
    inserted = [i for i, e in enumerate(world.events) if "INSERT INTO practice_" in e]
    flagged = [i for i, e in enumerate(world.events) if "UPDATE practices" in e]
    assert begin < min(inserted) and max(inserted) < commit and begin < flagged[0] < commit
    assert world.events.count("BEGIN") == 1 and "ROLLBACK" not in world.events
    world.cache.assert_awaited_once_with("zantara:crm_practices_stats:*")


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("field", ["document_label", "client_text", "reason"])
@pytest.mark.parametrize("value", ["", "   \n\t "])
def test_an_empty_or_whitespace_field_is_422(world: World, field: str, value: str) -> None:
    response = world.post(body(**{field: value}))
    assert response.status_code == 422
    assert world.conn.writes() == []


@pytest.mark.parametrize(
    ("field", "limit"), [("document_label", 100), ("client_text", 2000), ("reason", 1000)]
)
def test_length_limits_after_strip(world: World, reviewers, field: str, limit: int) -> None:
    reviewers("")
    assert world.post(body(**{field: "x" * (limit + 1)})).status_code == 422
    assert world.conn.writes() == []
    assert world.post(body(**{field: " " + "x" * limit + " "})).status_code == 201


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Proof of accommodation", "proof_of_accommodation"),
        ("2 photos", "doc_2_photos"),
        ("Café Résumé (2nd)", "cafe_resume_2nd"),
        ("--A--B--", "a_b"),
        ("X", "doc_x"),
        ("***", "doc_" + hashlib.sha1(b"***").hexdigest()[:8]),  # pragma: allowlist secret
        ("a" * 80, "a" * 50),
    ],
)
def test_the_document_type_is_derived_from_the_name(
    world: World, reviewers, name: str, expected: str
) -> None:
    reviewers("")
    response = world.post(body(document_label=name))
    assert response.status_code == 201, response.text
    derived = world.conn.sql_like("INSERT INTO practice_required_documents")[0][1][1]
    assert derived == expected and DOC_TYPE_PATTERN.match(derived)


def test_a_given_document_type_is_kept_and_a_bad_one_is_422(world: World, reviewers) -> None:
    reviewers("")
    assert world.post(body(document_type="cv")).status_code == 201
    assert world.conn.sql_like("INSERT INTO practice_required_documents")[0][1][1] == "cv"
    for bad in ("Bad Type", "1abc", "a", "x" * 51, "UPPER"):
        assert world.post(body(document_type=bad)).status_code == 422, bad


# ---------------------------------------------------------------- emails


def test_emails_go_out_after_the_commit_and_mark_the_request_notified(
    world: World, reviewers
) -> None:
    reviewers("a@example.test:en,b@example.test")
    response = world.post(body())
    assert response.status_code == 201
    assert world.events.index("COMMIT") < world.events.index("EMAIL")
    assert world.events.count("EMAIL") == 2
    marks = world.conn.sql_like("UPDATE practice_document_requests")
    assert len(marks) == 1 and "reviewers_notified_at" in marks[0][0] and 77 in marks[0][1]
    assert world.events.index("EMAIL") < world.events.index(
        next(e for e in world.events if "UPDATE practice_document_requests" in e)
    )


def test_notified_at_stays_null_when_no_send_returned_true(world: World, reviewers) -> None:
    reviewers("a@example.test:en,b@example.test")
    world.send_results = [False, False]
    assert world.post(body()).status_code == 201
    assert world.conn.sql_like("UPDATE practice_document_requests") == []


def test_one_true_send_is_enough_to_mark_the_request(world: World, reviewers) -> None:
    reviewers("a@example.test:en,b@example.test")
    world.send_results = [False, True]
    assert world.post(body()).status_code == 201
    assert len(world.conn.sql_like("UPDATE practice_document_requests")) == 1


def test_an_empty_setting_sends_nothing_and_the_request_succeeds(world: World, reviewers) -> None:
    reviewers("")
    response = world.post(body())
    assert response.status_code == 201
    assert world.sent == []
    assert world.conn.sql_like("UPDATE practice_document_requests") == []


def test_an_email_that_raises_does_not_fail_the_request(world: World, reviewers) -> None:
    reviewers("a@example.test:en,b@example.test:en")
    world.send_results = [RuntimeError("provider down"), True]
    response = world.post(body())
    assert response.status_code == 201
    assert response.json()["request_id"] == 77
    # The first send raised: the second reviewer is still told, and that one True marks the row.
    assert [m["to"] for m in world.sent] == ["a@example.test", "b@example.test"]
    assert len(world.conn.sql_like("UPDATE practice_document_requests")) == 1


def test_a_pool_that_fails_after_the_commit_does_not_fail_the_request(reviewers) -> None:
    reviewers("a@example.test:en")
    healthy = World()
    assert healthy.post(body()).status_code == 201
    assert healthy.sent, "the healthy run must reach the email"

    # The last acquire of a request is the one that tells the reviewers: refuse exactly that one.
    broken = World()
    broken.refuse_acquire_from = healthy.acquires
    response = broken.post(body())
    assert response.status_code == 201
    assert response.json()["request_id"] == 77
    assert "COMMIT" in broken.events and broken.sent == []


def test_a_cache_that_cannot_be_cleared_does_not_fail_the_request(world: World, reviewers) -> None:
    reviewers("")
    world.cache = AsyncMock(side_effect=RuntimeError("cache down"))
    response = world.post(body())
    assert response.status_code == 201
    assert response.json()["request_id"] == 77


@pytest.mark.parametrize(
    "entry",
    [
        "user+tag@example.test",
        "user@sub.domain.example.test",
        "User.Name@Example.TEST",
        "a@example.test.",
    ],
)
def test_a_legitimate_address_is_kept(world: World, reviewers, entry: str) -> None:
    reviewers(f"{entry}:en")
    assert world.post(body()).status_code == 201
    assert [m["to"] for m in world.sent] == [entry]


def test_an_empty_setting_takes_no_connection_to_notify(reviewers) -> None:
    reviewers("a@example.test:en")
    armed = World()
    assert armed.post(body()).status_code == 201
    reviewers("")
    idle = World()
    assert idle.post(body()).status_code == 201
    assert idle.acquires == armed.acquires - 1


@pytest.mark.parametrize(
    "entry",
    [
        "@:en",
        "a@:en",
        "@example.test",
        "a\tb@example.test:en",
        "a b@example.test",
        "a@b@example.test",
        "a@.example.test",
        "a@example..test",
        "a@example.test..",
    ],
)
def test_an_entry_that_is_not_an_address_is_skipped(world: World, reviewers, entry: str) -> None:
    reviewers(f"{entry},ok@example.test:en")
    assert world.post(body()).status_code == 201
    assert [m["to"] for m in world.sent] == ["ok@example.test"]


def test_one_email_per_valid_reviewer_in_their_language_to_that_address_only(
    world: World, reviewers
) -> None:
    reviewers("a@example.test:en, b@example.test ,c@example.test:fr,d@example.test:ID")
    assert world.post(body()).status_code == 201
    assert [m["to"] for m in world.sent] == [
        "a@example.test",
        "b@example.test",
        "d@example.test",
    ]
    for message in world.sent:
        assert "cc" not in message and "bcc" not in message
        assert set(message) <= {"to", "subject", "body", "log_context"}
    english, bahasa = world.sent[0], world.sent[1]
    assert "Asked by" in english["body"] and "Asked by" not in bahasa["body"]
    assert "Diminta oleh" in bahasa["body"]
    assert world.sent[2]["body"] == bahasa["body"]
    assert english["subject"] != bahasa["subject"]


def test_the_email_carries_the_facts_and_no_client_datum(world: World, reviewers) -> None:
    reviewers("a@example.test:en")
    assert world.post(body()).status_code == 201
    (message,) = world.sent
    text = message["subject"] + "\n" + message["body"]
    assert CLIENT_NAME not in text
    assert world.conn.sql_like("clients") == [], "the service must not read the client"
    assert "Proof of accommodation" in message["subject"]
    assert "Fixture Service" in text
    assert "#7" in message["body"]
    assert CREATOR in message["body"] and "2026-11-28" in message["body"]
    assert "Your booking for the first nights." in message["body"]
    assert "Stays with a friend, not in a hotel." in message["body"]
    assert "3 times" in message["body"]
    assert "Review in Kita" not in message["body"]
    assert "/process/7" in message["body"]


def test_every_consultant_text_is_escaped_in_the_body_and_plain_in_the_subject(
    world: World, reviewers
) -> None:
    reviewers("a@example.test:en")
    hostile = "<script>alert(1)</script> & <b>bold</b>"
    response = world.post(
        body(document_label="A & B <c>\r\nBcc: x", client_text=hostile, reason=hostile)
    )
    assert response.status_code == 201
    (message,) = world.sent
    assert "<script>" not in message["body"] and "<b>bold</b>" not in message["body"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in message["body"]
    assert "A &amp; B &lt;c&gt;" in message["body"]
    assert "A & B <c>" in message["subject"] and "&amp;" not in message["subject"]
    assert "\r" not in message["subject"] and "\n" not in message["subject"]


def test_a_practice_without_type_says_so_and_counts_zero(world: World, reviewers) -> None:
    reviewers("a@example.test:en")
    world.conn.type_id = None
    world.conn.type_name = None
    assert world.post(body()).status_code == 201
    (message,) = world.sent
    assert "no service" in message["body"].lower()
    assert "Fixture Service" not in message["body"]
    assert world.conn.sql_like("count(*)") == []
    assert "not asked before" in message["body"].lower()


def test_the_90_day_count_excludes_the_new_row_and_uses_type_and_document(
    world: World, reviewers
) -> None:
    reviewers("a@example.test:en")
    world.conn.recent = 4
    assert world.post(body()).status_code == 201
    ((sql, args),) = world.conn.sql_like("count(*)")
    assert "practice_document_requests" in sql
    assert "practice_type_id" in sql and "document_type" in sql and "requested_at" in sql
    assert "90" in sql and "id <> " in sql.replace("!=", "<>")
    assert 10 in args and "proof_of_accommodation" in args and 77 in args
    assert "4 times" in world.sent[0]["body"]


def test_the_subject_names_the_service_not_the_practice_title(world: World, reviewers) -> None:
    reviewers("a@example.test:en")
    assert world.post(body()).status_code == 201
    subject = world.sent[0]["subject"]
    assert "Fixture Service" in subject
    assert world.conn.sql_like("title") == []
