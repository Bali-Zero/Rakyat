"""
Unit tests for the "who added this document for your case" line of the portal
(V2 fourth cut): GET /api/portal/process/required-documents.

The route gains one optional key per item, ``case_request``, built from the newest
request row that created the slot and the house name of whoever asked. A staff address
must never reach the client, so every case also checks the serialised body for "@".

``app`` and ``client`` are local to this module (the ones of test_portal.py are local to
that one); ``mock_db_pool`` comes from the shared conftest. Invented names and
``example.test`` addresses only.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BASE_KEYS = [
    "id",
    "practice_id",
    "process_name",
    "process_status",
    "document_type",
    "document_label",
    "description",
    "is_required",
    "uploaded_by_client",
    "status",
    "client_notes",
]
ASKED_AT = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
STAFF_ADDRESS = "zz.consultant@example.test"


@pytest.fixture
def mock_portal_client():
    return {
        "client_id": 42,
        "user_id": "client-uuid-456",
        "email": "client@example.test",
        "name": "Zz Client",
    }


@pytest.fixture
def app(mock_portal_client, mock_db_pool):
    from backend.app.dependencies import get_database_pool
    from backend.app.routers.portal import get_current_client, router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_client] = lambda: mock_portal_client
    app.dependency_overrides[get_database_pool] = lambda: mock_db_pool
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _row(doc_id: int = 1, **extra) -> dict:
    row = {
        "id": doc_id,
        "practice_id": 81,
        "process_name": "Zz Visa",
        "process_status": "waiting_documents",
        "document_type": f"zz_doc_{doc_id}",
        "document_label": f"Zz Document {doc_id}",
        "description": "What the client reads",
        "is_required": True,
        "uploaded_by_client": False,
        "status": "pending",
        "client_notes": None,
        # What a careless handler could leak: the fixtures carry a staff address.
        "team_member_notes": "INTERNAL: chase the agent",
        "requested_by": STAFF_ADDRESS,
        "email": STAFF_ADDRESS,
    }
    row.update(extra)
    return row


def _asked(name, doc_id: int = 1, **extra) -> dict:
    return _row(
        doc_id,
        case_requested_at=ASKED_AT,
        case_added_by_name=name,
        **extra,
    )


def _get(client, mock_db_pool, rows):
    mock_db_pool._mock_conn.fetch.return_value = rows
    response = client.get("/api/portal/process/required-documents")
    assert "@" not in response.text
    assert STAFF_ADDRESS not in response.text
    return response


def _only_item(response) -> dict:
    assert response.status_code == 200
    (item,) = response.json()["data"]
    return item


def test_a_row_with_a_request_time_and_a_name_carries_case_request(client, mock_db_pool):
    item = _only_item(_get(client, mock_db_pool, [_asked("Zz Maya")]))
    assert item["case_request"] == {
        "added_by": "Zz Maya",
        "added_at": ASKED_AT.isoformat(),
    }
    assert list(item)[:11] == BASE_KEYS
    assert list(item) == BASE_KEYS + ["case_request"]
    assert item["description"] == "What the client reads"
    assert item["status"] == "pending"


def test_a_name_with_surrounding_whitespace_is_trimmed(client, mock_db_pool):
    item = _only_item(_get(client, mock_db_pool, [_asked("  Zz Maya \n")]))
    assert item["case_request"]["added_by"] == "Zz Maya"


def test_no_member_gives_a_null_name_and_keeps_the_time(client, mock_db_pool):
    item = _only_item(_get(client, mock_db_pool, [_asked(None)]))
    assert item["case_request"] == {"added_by": None, "added_at": ASKED_AT.isoformat()}


def test_a_blank_name_gives_null(client, mock_db_pool):
    item = _only_item(_get(client, mock_db_pool, [_asked("  \t ")]))
    assert item["case_request"]["added_by"] is None
    assert item["case_request"]["added_at"] == ASKED_AT.isoformat()


def test_a_name_containing_an_at_sign_gives_null(client, mock_db_pool):
    item = _only_item(_get(client, mock_db_pool, [_asked("zz.maya@example.test")]))
    assert item["case_request"]["added_by"] is None
    assert item["case_request"]["added_at"] == ASKED_AT.isoformat()


def test_a_row_whose_two_new_columns_are_null_is_the_item_of_base(client, mock_db_pool):
    row = _row(case_requested_at=None, case_added_by_name=None)
    item = _only_item(_get(client, mock_db_pool, [row]))
    assert list(item) == BASE_KEYS
    assert "case_request" not in item


def test_a_row_without_the_two_columns_is_the_item_of_base(client, mock_db_pool):
    item = _only_item(_get(client, mock_db_pool, [_row()]))
    assert list(item) == BASE_KEYS
    assert "case_request" not in item


def test_a_name_without_a_time_does_not_make_a_case_request(client, mock_db_pool):
    row = _row(case_requested_at=None, case_added_by_name="Zz Maya")
    item = _only_item(_get(client, mock_db_pool, [row]))
    assert "case_request" not in item


def test_two_rows_keep_the_order_the_statement_returned(client, mock_db_pool):
    rows = [_row(5), _asked("Zz Maya", 3), _row(9)]
    response = _get(client, mock_db_pool, rows)
    assert response.status_code == 200
    data = response.json()["data"]
    assert [d["id"] for d in data] == [5, 3, 9]
    assert [("case_request" in d) for d in data] == [False, True, False]


def test_the_statement_names_both_tables_keeps_the_filter_and_has_one_parameter(
    client, mock_db_pool
):
    _get(client, mock_db_pool, [])
    fetch = mock_db_pool._mock_conn.fetch
    fetch.assert_called_once()
    sql, *args = fetch.call_args.args
    assert args == [42]
    assert "practice_document_requests" in sql
    assert "team_members" in sql
    assert "p.client_id = $1 AND p.status != 'cancelled'" in sql
    assert set(re.findall(r"\$\d+", sql)) == {"$1"}
    assert "tm.role IS DISTINCT FROM 'client'" in sql
    assert "LIMIT 1" in sql


def test_the_order_by_puts_the_waiting_asked_slots_first_then_base_terms(client, mock_db_pool):
    _get(client, mock_db_pool, [])
    sql = mock_db_pool._mock_conn.fetch.call_args.args[0]
    order = re.sub(r"\s+", " ", sql.split("ORDER BY")[-1]).strip()
    assert order.startswith("(")
    leading, _, rest = order.partition(") DESC,")
    assert "IS NOT NULL" in leading
    assert "'pending'" in leading and "'rejected'" in leading
    assert "'uploaded'" not in leading and "'verified'" not in leading
    assert rest.strip() == "prd.is_required DESC, prd.created_at DESC"


def test_the_result_of_the_statement_selects_no_other_column_of_the_two_tables(
    client, mock_db_pool
):
    _get(client, mock_db_pool, [])
    sql = mock_db_pool._mock_conn.fetch.call_args.args[0]
    select_list = sql.split("FROM practice_required_documents")[0]
    for forbidden in (
        "requested_by",
        "reason",
        "client_text",
        "decision",
        "decided_by",
        "tm.email",
        "full_name",
        "team_member_notes",
    ):
        # A comment line may name a column it keeps out; code lines may not select it.
        code = "\n".join(
            line for line in select_list.splitlines() if not line.strip().startswith("--")
        )
        assert forbidden not in code, forbidden


def test_every_case_answer_has_no_at_sign_even_with_staff_columns_in_the_row(client, mock_db_pool):
    rows = [_asked("Zz Maya", 1), _asked(None, 2), _asked("a@b.test", 3), _row(4)]
    response = _get(client, mock_db_pool, rows)
    assert response.status_code == 200
    assert "@" not in response.text
    for item in response.json()["data"]:
        assert "requested_by" not in item and "email" not in item


def test_the_query_failing_answers_500_as_on_base(client, mock_db_pool):
    mock_db_pool._mock_conn.fetch.side_effect = RuntimeError("zz database down")
    response = client.get("/api/portal/process/required-documents")
    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to load process documents"


def test_team_member_notes_still_does_not_come_out(client, mock_db_pool):
    response = _get(client, mock_db_pool, [_asked("Zz Maya"), _row(2)])
    assert response.status_code == 200
    assert "INTERNAL:" not in response.text
    for item in response.json()["data"]:
        assert "team_member_notes" not in item
