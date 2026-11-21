"""Router tests: a transfer is offered, and classified against, only invoices of the company that
owns the account it reached (owner rule d). Fake connection; every value invented.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.services.accounting.companies import company_key_for_issuer
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES
from backend.tests.routers.test_accounting import _build_app

BASE = "/api/crm/accounting"
ADMIN = {"id": "acct-admin", "email": "Admin@Example.Test", "role": "user"}
DEFAULT = DEFAULT_ENTITY_KEY
OTHER = next(k for k in ENTITIES if k != DEFAULT_ENTITY_KEY)
NOBODY_ACCOUNT = "0000000001"


def _client(mock_db_pool) -> TestClient:
    return TestClient(_build_app(mock_db_pool, ADMIN), raise_server_exceptions=False)


def _account(key: str) -> str:
    return ENTITIES[key].bank_account_number


def _inv(invoice_id: int, issuer: str | None) -> dict:
    return {
        "invoice_id": invoice_id,
        "practice_id": invoice_id * 10,
        "amount_idr": 4_000_000,
        "generated_at": datetime(2026, 6, 20),
        "pph_expected_idr": 0,
        "client_name": "Contoh Klien",
        "issuer_entity_key": issuer,
    }


# One pool for every case: no issuer (the default company's), the default company, another one.
OPEN_POOL = [_inv(11, None), _inv(12, DEFAULT), _inv(13, OTHER)]
RECORDED_POOL = [_inv(21, None), _inv(22, DEFAULT), _inv(23, OTHER)]


def _txn(account: str | None) -> dict:
    return {
        "id": 3,
        "txn_date": date(2026, 6, 25),
        "amount_idr": 4_000_000,
        "direction": "credit",
        "account_number": account,
        "description": "TRF Contoh Klien",
    }


def test_an_invoice_with_no_issuer_belongs_to_the_default_company() -> None:
    assert company_key_for_issuer(None) == DEFAULT
    assert company_key_for_issuer("") == DEFAULT
    assert company_key_for_issuer(OTHER) == OTHER


@pytest.mark.parametrize(
    ("account", "open_ids", "recorded_ids"),
    [
        (_account(DEFAULT), {11, 12}, {21, 22}),
        (f"{_account(DEFAULT)}-IDR", {11, 12}, {21, 22}),
        (_account(OTHER), {13}, {23}),
        (NOBODY_ACCOUNT, set(), set()),
        (None, set(), set()),
    ],
)
def test_match_candidates_offers_only_the_receiving_companys_invoices(
    mock_db_pool, account, open_ids, recorded_ids
) -> None:
    _pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(return_value=_txn(account))
    conn.fetch = AsyncMock(side_effect=[OPEN_POOL, RECORDED_POOL])
    resp = _client(mock_db_pool).get(f"{BASE}/match-candidates/3")
    assert resp.status_code == 200
    body = resp.json()
    assert {c["invoice_id"] for c in body["candidates"]} == open_ids
    assert {c["invoice_id"] for c in body["recorded_candidates"]} == recorded_ids
    sql = " ".join(conn.fetchrow.await_args.args[0].split())
    assert "LEFT JOIN bank_statements bs ON bs.id = bt.statement_id" in sql


def test_the_inbox_classifies_each_transfer_against_its_own_companys_invoices(mock_db_pool) -> None:
    rows = [
        {**_txn(_account(OTHER)), "id": 3},
        {**_txn(_account(DEFAULT)), "id": 2, "amount_idr": 50_000_000},
        {**_txn(NOBODY_ACCOUNT), "id": 1},
    ]
    only_other = [_inv(13, OTHER)]
    # 50,000,000: far above the 4,000,000 transfers, so never a partial candidate of theirs.
    recorded = [
        {**_inv(22, DEFAULT), "amount_idr": 50_000_000},
        {**_inv(23, OTHER), "amount_idr": 50_000_000},
    ]
    _pool, conn = mock_db_pool
    conn.fetch = AsyncMock(side_effect=[rows, only_other, recorded])
    resp = _client(mock_db_pool).get(f"{BASE}/reconciliation-inbox")
    assert resp.status_code == 200
    by_id = {i["txn_id"]: i for i in resp.json()}
    # Another company's transfer meets that company's open invoice: one strong candidate, green.
    assert (by_id[3]["light"], by_id[3]["open_strong"], by_id[3]["suggestion"]["invoice_id"]) == (
        "green",
        1,
        13,
    )
    assert by_id[3]["recorded_candidates"] == 0
    # The default company's transfer sees its own recorded payment only.
    assert (by_id[2]["light"], by_id[2]["recorded_candidates"], by_id[2]["open_candidates"]) == (
        "recorded",
        1,
        0,
    )
    assert by_id[2]["suggestion"]["invoice_id"] == 22
    # An account no company owns meets no invoice at all.
    assert (by_id[1]["light"], by_id[1]["open_candidates"], by_id[1]["recorded_candidates"]) == (
        "red",
        0,
        0,
    )
