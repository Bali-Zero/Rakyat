"""Route tests of GET /api/crm/accounting/done-today with a mocked connection.

The SQL itself is judged by test_accounting_done_today_integration.py. All data is invented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import backend.app.routers.accounting as accounting_module
from backend.tests.routers.test_accounting import _build_app

URL = "/api/crm/accounting/done-today"
WITA = ZoneInfo("Asia/Makassar")
NOW = datetime(2026, 11, 18, 10, 30, tzinfo=WITA)
TOP_KEYS = {"day", "timezone", "counts", "items", "truncated"}
ITEM_KEYS = {
    "txn_id",
    "action",
    "done_at",
    "txn_date",
    "amount_idr",
    "description",
    "invoice_number",
    "client_name",
}


@pytest.fixture
def admin_user() -> dict[str, str]:
    return {"id": "admin", "email": "admin@example.test", "role": "user"}


@pytest.fixture
def outsider_user() -> dict[str, str]:
    return {"id": "x", "email": "random@balizero.com", "role": "user"}


def _row(txn_id: int = 1, status: str = "matched", **over):
    row = {
        "txn_id": txn_id,
        "txn_date": datetime(2026, 11, 17).date(),
        "amount_idr": 4_000_000,
        "description": "TRF PT CONTOH SATU",
        "reconciled_status": status,
        "confirmed_at": datetime(2026, 11, 18, 3, 15, 42, 654321, tzinfo=UTC),
        "invoice_number": "INV-000000-00001",
        "client_name": "PT CONTOH SATU",
    }
    row.update(over)
    return row


def _get(mock_db_pool, user, rows, now=NOW):
    _pool, conn = mock_db_pool
    conn.fetch = AsyncMock(return_value=rows)
    client = TestClient(_build_app(mock_db_pool, user), raise_server_exceptions=False)
    with patch.object(accounting_module, "_wita_now", return_value=now):
        return client.get(URL), conn


def test_b1_outsider_gets_403_and_no_statement_runs(mock_db_pool, outsider_user) -> None:
    pool, conn = mock_db_pool
    resp, _ = _get(mock_db_pool, outsider_user, [_row()])
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Accounting access denied"}
    pool.acquire.assert_not_called()
    conn.fetch.assert_not_called()


def test_b1_only_the_bank_month_can_be_filtered() -> None:
    route = next(r for r in accounting_module.router.routes if r.path.endswith("/done-today"))
    assert route.methods == {"GET"}
    assert [p.name for p in route.dependant.query_params] == ["month"]


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 11, 18, 0, 0, 0, tzinfo=WITA),
        datetime(2026, 11, 18, 23, 59, 59, 999999, tzinfo=WITA),
        # the same instants written in UTC land on the same WITA day
        datetime(2026, 11, 17, 16, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 18, 15, 59, 59, 999999, tzinfo=UTC),
    ],
)
def test_b8_bounds_are_the_wita_day_for_either_edge_of_it(mock_db_pool, admin_user, now) -> None:
    resp, conn = _get(mock_db_pool, admin_user, [], now=now)
    assert resp.status_code == 200
    start, end = conn.fetch.call_args.args[1:]
    assert start == datetime(2026, 11, 18, 0, 0, tzinfo=WITA)
    assert end == datetime(2026, 11, 19, 0, 0, tzinfo=WITA)
    assert start.utcoffset() == end.utcoffset() == timedelta(hours=8)
    assert resp.json()["day"] == "2026-11-18"
    assert resp.json()["timezone"] == "Asia/Makassar"


def test_the_route_reads_the_clock_only_through_wita_now(mock_db_pool, admin_user) -> None:
    far = datetime(2031, 3, 5, 9, 0, tzinfo=WITA)
    resp, conn = _get(mock_db_pool, admin_user, [], now=far)
    start, end = conn.fetch.call_args.args[1:]
    assert start == datetime(2031, 3, 5, 0, 0, tzinfo=WITA)
    assert end == datetime(2031, 3, 6, 0, 0, tzinfo=WITA)
    assert resp.json()["day"] == "2031-03-05"


def test_b6_exactly_one_select_and_nothing_else_touched(mock_db_pool, admin_user) -> None:
    _, conn = _get(mock_db_pool, admin_user, [_row()])
    assert conn.fetch.await_count == 1
    sql = conn.fetch.call_args.args[0]
    assert sql.lstrip().upper().startswith("SELECT")
    conn.execute.assert_not_called()
    conn.fetchrow.assert_not_called()
    conn.fetchval.assert_not_called()
    conn.transaction.assert_not_called()


def test_b2_shape_and_wita_done_at(mock_db_pool, admin_user) -> None:
    resp, _ = _get(mock_db_pool, admin_user, [_row(1), _row(2, "manual"), _row(3, "ignored")])
    body = resp.json()
    assert set(body) == TOP_KEYS
    assert body["truncated"] is False
    assert body["counts"] == {"confirmed": 1, "linked": 1, "ignored": 1, "total": 3}
    assert all(type(v) is int for v in body["counts"].values())
    assert [i["action"] for i in body["items"]] == ["confirmed", "linked", "ignored"]
    for item in body["items"]:
        assert set(item) == ITEM_KEYS
    first = body["items"][0]
    assert first["done_at"] == "2026-11-18T11:15:42+08:00"
    assert first["txn_date"] == "2026-11-17"
    assert first["amount_idr"] == 4_000_000
    assert first["invoice_number"] == "INV-000000-00001"
    assert first["client_name"] == "PT CONTOH SATU"


def test_b4_ignored_row_returns_no_invoice_or_client(mock_db_pool, admin_user) -> None:
    resp, _ = _get(mock_db_pool, admin_user, [_row(5, "ignored")])
    item = resp.json()["items"][0]
    assert item["invoice_number"] is None
    assert item["client_name"] is None


def test_b4_missing_links_give_nulls_for_a_confirmed_row(mock_db_pool, admin_user) -> None:
    row = _row(6, "matched", invoice_number=None, client_name=None)
    item = _get(mock_db_pool, admin_user, [row])[0].json()["items"][0]
    assert item["invoice_number"] is None
    assert item["client_name"] is None


def test_null_description_is_an_empty_string(mock_db_pool, admin_user) -> None:
    resp, _ = _get(mock_db_pool, admin_user, [_row(description=None)])
    assert resp.json()["items"][0]["description"] == ""


def test_b5_nothing_else_leaves(mock_db_pool, admin_user) -> None:
    extra = {
        "note": "SENTINEL-NOTE",
        "confirmed_by": "SENTINEL-WHO",
        "bank_account_id": 99,
        "raw_row": "SENTINEL-RAW",
    }
    resp, _ = _get(mock_db_pool, admin_user, [_row(**extra)])
    assert "SENTINEL" not in resp.text
    assert set(resp.json()["items"][0]) == ITEM_KEYS


def test_b3_501_rows_are_cut_to_500_and_truncated(mock_db_pool, admin_user) -> None:
    rows = [_row(1000 - n) for n in range(501)]
    body = _get(mock_db_pool, admin_user, rows)[0].json()
    assert len(body["items"]) == 500
    assert body["truncated"] is True
    assert body["counts"]["total"] == 501
    assert [i["txn_id"] for i in body["items"]] == [1000 - n for n in range(500)]


def test_b3_500_rows_are_not_truncated(mock_db_pool, admin_user) -> None:
    body = _get(mock_db_pool, admin_user, [_row(n) for n in range(1, 501)])[0].json()
    assert len(body["items"]) == 500
    assert body["truncated"] is False
    assert body["counts"]["total"] == 500


def test_empty_result_is_all_zeros(mock_db_pool, admin_user) -> None:
    body = _get(mock_db_pool, admin_user, [])[0].json()
    assert body["counts"] == {"confirmed": 0, "linked": 0, "ignored": 0, "total": 0}
    assert body["items"] == []
    assert body["truncated"] is False
    assert body["day"] == "2026-11-18"


def test_b7_b9_module_has_no_write_word_and_no_log_call() -> None:
    import re
    from pathlib import Path

    from backend.services.accounting import done_today_service

    text = Path(done_today_service.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE)\b", text, re.IGNORECASE
    )
    assert "logger" not in text and "logging" not in text
