"""Router tests for the reconciliation lights: the inbox and the three actions (spec L7, A6, A7).

Fake connection (conftest `mock_db_pool`); the SQL itself is proved against a real database in
services/test_accounting_lights_actions_integration.py. All values invented.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import backend.app.routers.accounting as accounting_module
from backend.services.accounting.reconcile_service import ActionResult, ReconcileError
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES
from backend.tests.routers.test_accounting import _build_app  # noqa: F401  (shared app builder)

BASE = "/api/crm/accounting"
ADMIN = {"id": "acct-admin", "email": "Admin@Example.Test", "role": "user"}
OUTSIDER = {"id": "x", "email": "random@example.test", "role": "user"}

ACTIONS = [
    ("/link-recorded", {"bank_txn_id": 1, "practice_id": 2, "invoice_id": 3}),
    ("/ignore-transaction", {"bank_txn_id": 1, "reason": "bukan klien"}),
    ("/reopen-transaction", {"bank_txn_id": 1}),
]


# The account of the default company: its transfers are matched against invoices with no issuer.
DEFAULT_ACCOUNT = ENTITIES[DEFAULT_ENTITY_KEY].bank_account_number


def _client(mock_db_pool, user=ADMIN) -> TestClient:
    return TestClient(_build_app(mock_db_pool, user), raise_server_exceptions=False)


def _inv(invoice_id: int, name: str, amount: int = 4_000_000, gen=datetime(2026, 6, 20)) -> dict:
    return {
        "invoice_id": invoice_id,
        "practice_id": invoice_id * 10,
        "amount_idr": amount,
        "generated_at": gen,
        "pph_expected_idr": 0,
        "client_name": name,
    }


def _tx(
    txn_id: int,
    amount: int,
    memo: str | None,
    day=date(2026, 6, 25),
    account: str | None = DEFAULT_ACCOUNT,
) -> dict:
    return {
        "id": txn_id,
        "txn_date": day,
        "amount_idr": amount,
        "description": memo,
        "account_number": account,
    }


# ── the admin guard ──────────────────────────────────────────────────────────────────────


def test_inbox_is_admin_only(mock_db_pool) -> None:
    resp = _client(mock_db_pool, OUTSIDER).get(f"{BASE}/reconciliation-inbox")
    assert resp.status_code == 403
    _pool, conn = mock_db_pool
    conn.fetch.assert_not_called()


def test_companies_is_admin_only(mock_db_pool) -> None:
    resp = _client(mock_db_pool, OUTSIDER).get(f"{BASE}/companies")
    assert resp.status_code == 403
    _pool, conn = mock_db_pool
    conn.fetch.assert_not_called()


def test_companies_lists_every_issuing_company_with_only_selects(mock_db_pool) -> None:
    from backend.services.invoicing.entities import ENTITIES

    _pool, conn = mock_db_pool
    conn.fetch = AsyncMock(side_effect=[[], []])
    resp = _client(mock_db_pool).get(f"{BASE}/companies")
    assert resp.status_code == 200
    assert [c["key"] for c in resp.json()] == list(ENTITIES)
    sqls = [c.args[0] for c in conn.fetch.await_args_list]
    assert len(sqls) == 2 and all(s.lstrip().upper().startswith("SELECT") for s in sqls)
    for name in ("execute", "fetchrow", "fetchval"):
        assert not getattr(conn, name).called, name


@pytest.mark.parametrize(("path", "body"), ACTIONS)
def test_the_three_actions_are_admin_only(mock_db_pool, path, body) -> None:
    with (
        patch.object(accounting_module.reconcile_service, "link_recorded", AsyncMock()) as a,
        patch.object(accounting_module.reconcile_service, "ignore_transaction", AsyncMock()) as b,
        patch.object(accounting_module.reconcile_service, "reopen_transaction", AsyncMock()) as c,
    ):
        resp = _client(mock_db_pool, OUTSIDER).post(f"{BASE}{path}", json=body)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Accounting access denied"}
    assert not (a.called or b.called or c.called)


# ── success path and identity ────────────────────────────────────────────────────────────


def test_link_passes_the_ids_and_the_lowercased_jwt_identity(mock_db_pool) -> None:
    link = AsyncMock(return_value=ActionResult(1, "manual", 77))
    with patch.object(accounting_module.reconcile_service, "link_recorded", link):
        resp = _client(mock_db_pool).post(f"{BASE}/link-recorded", json=ACTIONS[0][1])
    assert resp.status_code == 200
    assert resp.json() == {
        "bank_txn_id": 1,
        "reconciled_status": "manual",
        "reconciliation_log_id": 77,
    }
    kw = link.await_args.kwargs
    assert (kw["bank_txn_id"], kw["practice_id"], kw["invoice_id"], kw["linked_by"]) == (
        1,
        2,
        3,
        "admin@example.test",
    )


def test_ignore_passes_the_reason_and_identity(mock_db_pool) -> None:
    ign = AsyncMock(return_value=ActionResult(1, "ignored", 8))
    with patch.object(accounting_module.reconcile_service, "ignore_transaction", ign):
        resp = _client(mock_db_pool).post(f"{BASE}/ignore-transaction", json=ACTIONS[1][1])
    assert resp.status_code == 200
    assert resp.json() == {
        "bank_txn_id": 1,
        "reconciled_status": "ignored",
        "reconciliation_log_id": 8,
    }
    kw = ign.await_args.kwargs
    assert (kw["bank_txn_id"], kw["reason"], kw["ignored_by"]) == (
        1,
        "bukan klien",
        "admin@example.test",
    )


def test_reopen_passes_identity(mock_db_pool) -> None:
    reo = AsyncMock(return_value=ActionResult(1, "unmatched", 9))
    with patch.object(accounting_module.reconcile_service, "reopen_transaction", reo):
        resp = _client(mock_db_pool).post(f"{BASE}/reopen-transaction", json={"bank_txn_id": 1})
    assert resp.status_code == 200
    assert resp.json() == {
        "bank_txn_id": 1,
        "reconciled_status": "unmatched",
        "reconciliation_log_id": 9,
    }
    assert reo.await_args.kwargs["reopened_by"] == "admin@example.test"


# ── 422 mapping, fixed text, no echo (A6) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "body", "target"),
    [
        (ACTIONS[0][0], ACTIONS[0][1], "link_recorded"),
        (ACTIONS[1][0], ACTIONS[1][1], "ignore_transaction"),
        (ACTIONS[2][0], ACTIONS[2][1], "reopen_transaction"),
    ],
)
def test_a_refusal_is_422_with_the_services_fixed_text(mock_db_pool, path, body, target) -> None:
    boom = AsyncMock(side_effect=ReconcileError("bank transaction 1 is not unmatched"))
    with patch.object(accounting_module.reconcile_service, target, boom):
        resp = _client(mock_db_pool).post(f"{BASE}{path}", json=body)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "bank transaction 1 is not unmatched"}


def test_the_real_service_refusal_through_the_router_is_422_and_never_echoes_the_reason(
    mock_db_pool, caplog
) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(return_value=None)  # no such bank transaction
    sentinel = "alasan-rahasia-contoh"
    with caplog.at_level(logging.DEBUG):
        resp = _client(mock_db_pool).post(
            f"{BASE}/ignore-transaction", json={"bank_txn_id": 41, "reason": sentinel}
        )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "bank transaction 41 not found"}
    assert sentinel not in resp.text
    assert sentinel not in caplog.text
    conn.execute.assert_not_called()  # a refusal wrote nothing


def test_the_real_link_refusal_through_the_router_names_only_ids(mock_db_pool) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"direction": "credit", "reconciled_status": "unmatched", "amount_idr": 1},
            None,
        ]
    )
    resp = _client(mock_db_pool).post(
        f"{BASE}/link-recorded", json={"bank_txn_id": 5, "practice_id": 6, "invoice_id": 7}
    )
    assert resp.status_code == 422 and resp.json() == {"detail": "practice 6 not found"}
    conn.execute.assert_not_called()


# ── request bounds ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/link-recorded", {"practice_id": 2, "invoice_id": 3}),
        ("/link-recorded", {"bank_txn_id": 1}),
        ("/link-recorded", {"bank_txn_id": 1, "practice_id": 2}),
        ("/link-recorded", {"bank_txn_id": 0, "practice_id": 2, "invoice_id": 3}),
        ("/link-recorded", {"bank_txn_id": 1, "practice_id": 2**31, "invoice_id": 3}),
        ("/link-recorded", {"bank_txn_id": 2**63, "practice_id": 2, "invoice_id": 3}),
        ("/link-recorded", {"bank_txn_id": 1, "practice_id": 2, "invoice_id": 0}),
        ("/link-recorded", {"bank_txn_id": "one", "practice_id": 2, "invoice_id": 3}),
        ("/ignore-transaction", {"bank_txn_id": 1}),
        ("/ignore-transaction", {"reason": "bukan klien"}),
        ("/reopen-transaction", {}),
        ("/reopen-transaction", {"bank_txn_id": -4}),
    ],
)
def test_malformed_bodies_are_422_before_the_handler_runs(mock_db_pool, path, body) -> None:
    _pool, conn = mock_db_pool
    resp = _client(mock_db_pool).post(f"{BASE}{path}", json=body)
    assert resp.status_code == 422
    conn.fetchrow.assert_not_called()


@pytest.mark.parametrize(
    ("limit", "status"),
    [(0, 422), (1, 200), (200, 200), (500, 200), (1000, 200), (1001, 422), (-1, 422)],
)
def test_inbox_limit_bounds(mock_db_pool, limit, status) -> None:
    resp = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox?limit={limit}")
    assert resp.status_code == status


def test_inbox_default_limit_is_200_and_is_passed_to_the_query(mock_db_pool) -> None:
    _pool, conn = mock_db_pool
    _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox")
    assert conn.fetch.await_args_list[0].args[1] == 200
    _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox?limit=17")
    assert conn.fetch.await_args_list[3].args[1] == 17


# ── the inbox ────────────────────────────────────────────────────────────────────────────


def _inbox_conn(mock_db_pool, txns, open_pool, recorded_pool) -> None:
    _pool, conn = mock_db_pool
    conn.fetch = AsyncMock(side_effect=[txns, open_pool, recorded_pool])


def test_inbox_shape_and_each_light(mock_db_pool) -> None:
    txns = [
        _tx(4, 4_000_000, "TRF CONTOH KLIEN"),  # one strong open, none recorded -> green
        _tx(3, 7_000_000, "TRF Pembayar Lain"),  # strong recorded only -> recorded
        _tx(2, 4_000_000, "TRF Zzzz"),  # candidates without name evidence -> yellow
        _tx(1, 123_457, None),  # nothing fits -> red, null memo tolerated
    ]
    open_pool = [_inv(11, "Contoh Klien"), _inv(12, "Orang Lain")]
    recorded_pool = [_inv(21, "Pembayar Lain", 7_000_000)]
    _inbox_conn(mock_db_pool, txns, open_pool, recorded_pool)
    resp = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox")
    assert resp.status_code == 200
    items = resp.json()
    assert [i["txn_id"] for i in items] == [4, 3, 2, 1]  # the query's order is kept (newest first)
    assert [i["light"] for i in items] == ["green", "recorded", "yellow", "red"]
    for i in items:
        assert set(i) == {
            "txn_id",
            "txn_date",
            "amount_idr",
            "description",
            "company_key",
            "light",
            "open_candidates",
            "open_strong",
            "recorded_candidates",
            "recorded_strong",
            "suggestion",
        }
    green, recorded, yellow, red = items
    assert green["txn_date"] == "2026-06-25" and green["amount_idr"] == 4_000_000
    assert (
        green["open_candidates"],
        green["open_strong"],
        green["recorded_candidates"],
        green["recorded_strong"],
    ) == (2, 1, 0, 0)
    assert green["suggestion"]["invoice_id"] == 11
    assert set(green["suggestion"]) == {
        "invoice_id",
        "practice_id",
        "client_name",
        "invoice_amount_idr",
        "confidence",
        "reasons",
        "amount_delta_idr",
        "likely_withheld",
        "invoice_number",
        "invoice_issued_at",
        "service_name",
    }
    assert (
        recorded["recorded_candidates"],
        recorded["recorded_strong"],
        recorded["open_candidates"],
    ) == (1, 1, 0)
    assert recorded["suggestion"]["invoice_id"] == 21
    assert (
        yellow["suggestion"] is None
        and yellow["open_candidates"] == 2
        and yellow["open_strong"] == 0
    )
    assert red["suggestion"] is None and red["description"] == "" and red["open_candidates"] == 0


def test_inbox_handler_issues_only_selects_and_asks_for_credits_unmatched_newest_first(
    mock_db_pool,
) -> None:
    _inbox_conn(mock_db_pool, [], [], [])
    _pool, conn = mock_db_pool
    assert _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox?limit=9").json() == []
    for name in ("execute", "fetchrow", "fetchval"):
        assert not getattr(conn, name).called, name
    sqls = [c.args[0] for c in conn.fetch.await_args_list]
    assert len(sqls) == 3 and all(s.lstrip().upper().startswith("SELECT") for s in sqls)
    first = " ".join(sqls[0].split())
    assert "bt.direction = 'credit' AND bt.reconciled_status = 'unmatched'" in first
    assert "ORDER BY bt.txn_date DESC, bt.id DESC LIMIT $1" in first
    assert "LEFT JOIN bank_statements bs ON bs.id = bt.statement_id" in first
    assert "<> 'paid'" in sqls[1] and "= 'paid'" in sqls[2] and "<> 'paid'" not in sqls[2]
    conn.transaction.assert_not_called()  # no transaction is opened by the read route


def test_each_inbox_transfer_names_the_company_whose_account_received_it(mock_db_pool) -> None:
    from backend.services.invoicing.entities import ENTITIES

    keys = list(ENTITIES)
    first, second = ENTITIES[keys[0]], ENTITIES[keys[1]]
    txns = [
        # the statement's number carries a currency suffix, the company's has none
        _tx(3, 1_000_000, "x", account=f"{first.bank_account_number}-IDR"),
        # the company's number has dashes or spaces the statement's has not, or the reverse
        _tx(
            2, 1_000_000, "x", account="".join(c for c in second.bank_account_number if c.isdigit())
        ),
        _tx(1, 1_000_000, "x", account="999000111"),  # an account no company owns
        _tx(0, 1_000_000, "x", account=None),  # a statement without an account number
        # a company's digits inside a longer number are another account, never that company
        _tx(5, 1_000_000, "x", account=f"9{first.bank_account_number}"),
        _tx(6, 1_000_000, "x", account=f"{first.bank_account_number}9"),
    ]
    _inbox_conn(mock_db_pool, txns, [], [])
    items = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox").json()
    assert [i["company_key"] for i in items] == [
        first.key,
        second.key,
        "other",
        "other",
        "other",
        "other",
    ]


def test_inbox_classification_runs_off_the_event_loop(mock_db_pool, monkeypatch) -> None:
    import asyncio

    calls: list[object] = []
    original = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        calls.append(func)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(accounting_module.asyncio, "to_thread", spy)
    _inbox_conn(mock_db_pool, [_tx(1, 4_000_000, "x")], [_inv(1, "Contoh Klien")], [])
    resp = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox")
    assert resp.status_code == 200
    assert calls == [accounting_module._classify_inbox]


# ── A7: what exists does not change ──────────────────────────────────────────────────────


def test_match_candidates_keeps_its_fields_and_gains_recorded_candidates(mock_db_pool) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 3,
            "txn_date": date(2026, 6, 25),
            "amount_idr": 4_000_000,
            "direction": "credit",
            "account_number": DEFAULT_ACCOUNT,
            "description": "TRF Contoh Klien",
        }
    )
    conn.fetch = AsyncMock(side_effect=[[_inv(11, "Contoh Klien")], [_inv(21, "Contoh Klien")]])
    resp = _client(mock_db_pool).get(f"{BASE}/match-candidates/3")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"txn_id", "transfer_amount_idr", "candidates", "recorded_candidates"}
    assert (body["txn_id"], body["transfer_amount_idr"]) == (3, 4_000_000)
    assert body["candidates"] == [
        {
            "invoice_id": 11,
            "practice_id": 110,
            "client_name": "Contoh Klien",
            "invoice_amount_idr": 4_000_000,
            "confidence": 0.95,
            "reasons": ["exact amount match", "payer name ~ client (100%)"],
            "amount_delta_idr": 0,
            "likely_withheld": False,
            "invoice_number": None,
            "invoice_issued_at": "2026-06-20T00:00:00",
            "service_name": None,
        }
    ]
    assert body["recorded_candidates"] == [
        {**body["candidates"][0], "invoice_id": 21, "practice_id": 210}
    ]
    sqls = [c.args[0] for c in conn.fetch.await_args_list]
    assert "<> 'paid'" in sqls[0] and "= 'paid'" in sqls[1] and "<> 'paid'" not in sqls[1]


def test_match_candidates_recorded_list_is_empty_when_nothing_is_recorded(mock_db_pool) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 3,
            "txn_date": date(2026, 6, 25),
            "amount_idr": 5,
            "direction": "credit",
            "account_number": DEFAULT_ACCOUNT,
            "description": "",
        }
    )
    conn.fetch = AsyncMock(return_value=[])
    body = _client(mock_db_pool).get(f"{BASE}/match-candidates/3").json()
    assert body["candidates"] == [] and body["recorded_candidates"] == []


# ── A8: ids are integers ─────────────────────────────────────────────────────────────────

_ID_BODIES = {
    "/link-recorded": {"bank_txn_id": 1, "practice_id": 2, "invoice_id": 3},
    "/ignore-transaction": {"bank_txn_id": 1, "reason": "bukan klien"},
    "/reopen-transaction": {"bank_txn_id": 1},
}
_BAD_IDS = [True, "5", 5.0, 5.5, None, [5], False]


@pytest.mark.parametrize(
    ("path", "field"),
    [(p, f) for p, body in _ID_BODIES.items() for f in body if f.endswith("_id")],
)
@pytest.mark.parametrize("bad", _BAD_IDS, ids=repr)
def test_an_id_that_is_not_a_json_integer_is_422_and_reaches_no_action(
    mock_db_pool, path, field, bad
) -> None:
    _pool, conn = mock_db_pool
    body = {**_ID_BODIES[path], field: bad}
    with (
        patch.object(accounting_module.reconcile_service, "link_recorded", AsyncMock()) as a,
        patch.object(accounting_module.reconcile_service, "ignore_transaction", AsyncMock()) as b,
        patch.object(accounting_module.reconcile_service, "reopen_transaction", AsyncMock()) as c,
    ):
        resp = _client(mock_db_pool).post(f"{BASE}{path}", json=body)
    assert resp.status_code == 422
    assert not (a.called or b.called or c.called)
    conn.fetchrow.assert_not_called()


def test_a_raw_float_with_a_zero_fraction_is_not_an_integer(mock_db_pool) -> None:
    resp = _client(mock_db_pool).post(
        f"{BASE}/reopen-transaction",
        content=b'{"bank_txn_id": 5.0}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


# ── A2/A6: the reason is validated by the service, with fixed text, and never leaked ─────

_SENTINEL = "alasan-rahasia-contoh"
_REFUSAL = "reason must be 3 to 200 printable characters"


@pytest.mark.parametrize(
    "reason",
    [
        "   ",
        "ab",
        "x" * 201,
        "abc\u0000def",
        "\u0007bell",
        _SENTINEL + "\u0000",
        _SENTINEL * 20,
        "x" * 100 + "   " + "y" * 101,
    ],
)
def test_an_unfit_reason_is_422_with_the_fixed_text_and_is_never_echoed_or_logged(
    mock_db_pool, caplog, reason
) -> None:
    _pool, conn = mock_db_pool
    with caplog.at_level(logging.DEBUG):
        resp = _client(mock_db_pool).post(
            f"{BASE}/ignore-transaction", json={"bank_txn_id": 1, "reason": reason}
        )
    assert resp.status_code == 422
    assert resp.json() == {"detail": _REFUSAL}
    assert _SENTINEL not in resp.text and _SENTINEL not in caplog.text
    conn.execute.assert_not_called()


def test_a_lone_surrogate_reason_is_refused_with_the_fixed_text(mock_db_pool, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        resp = _client(mock_db_pool).post(
            f"{BASE}/ignore-transaction",
            content=b'{"bank_txn_id": 1, "reason": "ab\\ud800cd"}',
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 422
    assert resp.json() == {"detail": _REFUSAL}


def test_the_reason_field_has_no_declared_length_or_pattern() -> None:
    field = accounting_module.IgnoreTransactionRequest.model_fields["reason"]
    assert field.metadata == [] and field.annotation is str


def test_a_trimmed_reason_of_200_is_accepted_and_the_trimmed_text_is_stored(mock_db_pool) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(
        return_value={"direction": "credit", "reconciled_status": "unmatched", "amount_idr": 1}
    )
    conn.fetchval = AsyncMock(return_value=33)
    reason = "  " + "x" * 200 + "  "
    resp = _client(mock_db_pool).post(
        f"{BASE}/ignore-transaction", json={"bank_txn_id": 1, "reason": reason}
    )
    assert resp.status_code == 200 and resp.json()["reconciliation_log_id"] == 33
    assert conn.fetchval.await_args.args[-1] == "x" * 200  # the note is the trimmed text


# ── A6: an unexpected failure is a 500 with fixed text, the type name only in the log ────


class _DriverError(Exception):
    pass


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/link-recorded", "link_recorded"),
        ("/ignore-transaction", "ignore_transaction"),
        ("/reopen-transaction", "reopen_transaction"),
    ],
)
def test_an_unexpected_failure_is_500_fixed_text_and_logs_only_the_type_name(
    mock_db_pool, caplog, path, target
) -> None:
    boom = AsyncMock(side_effect=_DriverError(f"duplicate key value {_SENTINEL} (id)=(5)"))
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(accounting_module.reconcile_service, target, boom),
    ):
        resp = _client(mock_db_pool).post(f"{BASE}{path}", json=_ID_BODIES[path])
    assert resp.status_code == 500
    assert resp.json() == {"detail": "reconciliation action could not be completed"}
    assert _SENTINEL not in resp.text and _SENTINEL not in caplog.text
    assert "_DriverError" in caplog.text
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_refusal_is_still_422_not_500(mock_db_pool) -> None:
    boom = AsyncMock(side_effect=ReconcileError("bank transaction 1 not found"))
    with patch.object(accounting_module.reconcile_service, "reopen_transaction", boom):
        resp = _client(mock_db_pool).post(f"{BASE}/reopen-transaction", json={"bank_txn_id": 1})
    assert resp.status_code == 422


# ── L7/A7: a non-finite stored amount never makes a route fail ───────────────────────────


@pytest.mark.parametrize(
    "bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), float("nan"), float("inf")]
)
def test_a_non_finite_invoice_amount_is_zero_in_the_inbox_and_in_match_candidates(
    mock_db_pool, bad
) -> None:
    _pool, conn = mock_db_pool
    rows = [_inv(11, "Contoh Klien", amount=bad), _inv(12, "Contoh Klien")]
    _inbox_conn(mock_db_pool, [_tx(1, 4_000_000, "TRF Contoh Klien")], rows, [])
    resp = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox")
    assert resp.status_code == 200
    item = resp.json()[0]
    assert (item["open_candidates"], item["suggestion"]["invoice_id"]) == (1, 12)

    conn.fetchrow = AsyncMock(
        return_value={
            "id": 3,
            "txn_date": date(2026, 6, 25),
            "amount_idr": 4_000_000,
            "direction": "credit",
            "account_number": DEFAULT_ACCOUNT,
            "description": "TRF Contoh Klien",
        }
    )
    conn.fetch = AsyncMock(side_effect=[rows, [_inv(21, "Contoh Klien", amount=bad)]])
    resp = _client(mock_db_pool).get(f"{BASE}/match-candidates/3")
    assert resp.status_code == 200
    body = resp.json()
    assert [c["invoice_id"] for c in body["candidates"]] == [12]
    assert body["recorded_candidates"] == []


def test_a_null_invoice_amount_is_zero_too(mock_db_pool) -> None:
    _inbox_conn(mock_db_pool, [_tx(1, 4_000_000, "x")], [_inv(11, "Contoh Klien", amount=None)], [])
    resp = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox")
    assert resp.status_code == 200 and resp.json()[0]["light"] == "red"


@pytest.mark.parametrize(
    ("given", "stored"),
    [("a\nb c", "a b c"), ("bunga   bank", "bunga bank"), ("\t x\u00a0\u00a0y ", "x y")],
)
def test_whitespace_runs_collapse_to_one_space_and_that_text_is_stored(
    mock_db_pool, given, stored
) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(
        return_value={"direction": "credit", "reconciled_status": "unmatched", "amount_idr": 1}
    )
    conn.fetchval = AsyncMock(return_value=33)
    resp = _client(mock_db_pool).post(
        f"{BASE}/ignore-transaction", json={"bank_txn_id": 1, "reason": given}
    )
    assert resp.status_code == 200
    assert conn.fetchval.await_args.args[-1] == stored


# ── A6: obtaining the connection is inside the guarded block ─────────────────────────────


@pytest.mark.parametrize("path", list(_ID_BODIES))
def test_a_pool_failure_is_the_fixed_500_and_is_logged_by_type_name_only(
    mock_db_pool, caplog, path
) -> None:
    pool, _conn = mock_db_pool

    def boom():
        raise _DriverError(f"connection to server failed {_SENTINEL}")

    pool.acquire = boom
    with caplog.at_level(logging.DEBUG):
        resp = _client(mock_db_pool).post(f"{BASE}{path}", json=_ID_BODIES[path])
    assert resp.status_code == 500
    assert resp.json() == {"detail": "reconciliation action could not be completed"}
    assert _SENTINEL not in resp.text and _SENTINEL not in caplog.text
    assert "_DriverError" in caplog.text


# ── the lights see an invoice amount truncated to whole rupiah, never rounded ────────────


def test_the_cents_of_an_invoice_amount_are_truncated_in_the_classifier_and_in_match_candidates(
    mock_db_pool,
) -> None:
    _pool, conn = mock_db_pool
    cents = Decimal("4000000.99")
    _inbox_conn(
        mock_db_pool,
        [_tx(1, 4_000_000, "TRF Contoh Klien")],
        [_inv(11, "Contoh Klien", amount=cents)],
        [],
    )
    item = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox").json()[0]
    assert item["light"] == "green"
    assert item["suggestion"]["invoice_amount_idr"] == 4_000_000  # not 4,000,001
    assert item["suggestion"]["amount_delta_idr"] == 0

    conn.fetchrow = AsyncMock(
        return_value={
            "id": 3,
            "txn_date": date(2026, 6, 25),
            "amount_idr": 4_000_000,
            "direction": "credit",
            "account_number": DEFAULT_ACCOUNT,
            "description": "TRF Contoh Klien",
        }
    )
    conn.fetch = AsyncMock(side_effect=[[_inv(11, "Contoh Klien", amount=cents)], []])
    cand = _client(mock_db_pool).get(f"{BASE}/match-candidates/3").json()["candidates"][0]
    assert (cand["invoice_amount_idr"], cand["amount_delta_idr"]) == (4_000_000, 0)


# ── candidate identity: invoice number, issue date, service (spec I1, I2) ────────────────

IDENTITY_KEYS = {"invoice_number", "invoice_issued_at", "service_name"}


def _inv_id(invoice_id: int, name: str, **identity) -> dict:
    return {**_inv(invoice_id, name), **identity}


def test_identity_keys_are_in_every_candidate_of_both_routes_and_in_the_suggestion(
    mock_db_pool,
) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 3,
            "txn_date": date(2026, 6, 25),
            "amount_idr": 4_000_000,
            "direction": "credit",
            "account_number": DEFAULT_ACCOUNT,
            "description": "TRF Contoh Klien",
        }
    )
    conn.fetch = AsyncMock(side_effect=[[_inv(11, "Contoh Klien")], [_inv(21, "Contoh Klien")]])
    body = _client(mock_db_pool).get(f"{BASE}/match-candidates/3").json()
    for c in body["candidates"] + body["recorded_candidates"]:
        assert IDENTITY_KEYS <= set(c)

    _pool, conn = mock_db_pool
    conn.fetch = AsyncMock(
        side_effect=[
            [_tx(1, 4_000_000, "TRF Contoh Klien")],
            [_inv(11, "Contoh Klien")],
            [],
        ]
    )
    item = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox").json()[0]
    assert IDENTITY_KEYS <= set(item["suggestion"])


def test_null_sources_yield_three_nulls_and_values_pass_through(mock_db_pool) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 3,
            "txn_date": date(2026, 6, 25),
            "amount_idr": 4_000_000,
            "direction": "credit",
            "account_number": DEFAULT_ACCOUNT,
            "description": "",
        }
    )
    nulls = _inv_id(11, "Contoh A", invoice_number=None, service_name=None)
    nulls["generated_at"] = None
    full = _inv_id(
        12, "Contoh B", invoice_number="INV-000000-00001", service_name="Example service"
    )
    conn.fetch = AsyncMock(side_effect=[[nulls, full], []])
    cands = {
        c["invoice_id"]: c
        for c in _client(mock_db_pool).get(f"{BASE}/match-candidates/3").json()["candidates"]
    }
    assert [cands[11][k] for k in sorted(IDENTITY_KEYS)] == [None, None, None]
    assert cands[12]["invoice_number"] == "INV-000000-00001"
    assert cands[12]["invoice_issued_at"] == "2026-06-20T00:00:00"
    assert cands[12]["service_name"] == "Example service"


def test_a_row_without_the_new_columns_still_yields_nulls(mock_db_pool) -> None:
    _pool, conn = mock_db_pool
    row = _inv(11, "Contoh A")
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 3,
            "txn_date": date(2026, 6, 25),
            "amount_idr": 4_000_000,
            "direction": "credit",
            "account_number": DEFAULT_ACCOUNT,
            "description": "",
        }
    )
    conn.fetch = AsyncMock(side_effect=[[row], []])
    cand = _client(mock_db_pool).get(f"{BASE}/match-candidates/3").json()["candidates"][0]
    assert (cand["invoice_number"], cand["service_name"]) == (None, None)


def _txn_row() -> dict:
    return {
        "id": 3,
        "txn_date": date(2026, 6, 25),
        "amount_idr": 4_000_000,
        "direction": "credit",
        "account_number": DEFAULT_ACCOUNT,
        "description": "",
    }


def test_candidates_of_equal_confidence_keep_the_order_of_the_pool_rows_given(
    mock_db_pool,
) -> None:
    """Three tied rows whose numbers are 5, 9, 1 in pool order: an ascending or a descending
    tie-break by number would reorder them. This pins the matcher's handling of the rows it is
    given; it does not make the database's row order deterministic."""
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(return_value=_txn_row())
    rows = [
        _inv_id(31, "Contoh A", invoice_number="INV-000000-00005"),
        _inv_id(32, "Contoh A", invoice_number="INV-000000-00009"),
        _inv_id(33, "Contoh A", invoice_number="INV-000000-00001"),
    ]
    conn.fetch = AsyncMock(side_effect=[rows, []])
    body = _client(mock_db_pool).get(f"{BASE}/match-candidates/3").json()
    assert [c["invoice_id"] for c in body["candidates"]] == [31, 32, 33]


_POOL_SQL_HEAD = (
    "SELECT i.id AS invoice_id, i.practice_id, i.amount_idr, i.generated_at, "
    "COALESCE(i.pph_expected_idr, 0) AS pph_expected_idr, c.full_name AS client_name, "
    "i.invoice_number, pt.name AS service_name, i.issuer_entity_key "
    "FROM invoices i JOIN practices p ON i.practice_id = p.id "
    "LEFT JOIN practice_types pt ON pt.id = p.practice_type_id "
    "LEFT JOIN clients c ON p.client_id = c.id "
)


def test_each_pool_query_is_pinned_in_full(mock_db_pool) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(return_value=_txn_row())
    conn.fetch = AsyncMock(side_effect=[[], []])
    _client(mock_db_pool).get(f"{BASE}/match-candidates/3")
    open_sql, recorded_sql = [" ".join(c.args[0].split()) for c in conn.fetch.await_args_list]
    assert open_sql == _POOL_SQL_HEAD + "WHERE COALESCE(p.payment_status, 'unpaid') <> 'paid'"
    assert recorded_sql == _POOL_SQL_HEAD + "WHERE COALESCE(p.payment_status, 'unpaid') = 'paid'"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invoice_number", "INV-000000-00001"),
        ("invoice_issued_at", "2026-01-10"),
        ("service_name", "Example service"),
    ],
)
def test_the_matcher_carries_each_identity_value_from_the_pool_row_to_the_candidate(
    field, value
) -> None:
    from backend.services.accounting.matcher import OpenInvoice, rank_candidates

    inv = OpenInvoice(
        invoice_id=1,
        practice_id=2,
        client_name="Contoh A",
        amount_idr=4_000_000,
        generated_at=date(2026, 6, 20),
        **{field: value},
    )
    (cand,) = rank_candidates(
        transfer_amount_idr=4_000_000,
        transfer_date=date(2026, 6, 25),
        payer_memo="",
        open_invoices=[inv],
    )
    assert getattr(cand, field) == value
