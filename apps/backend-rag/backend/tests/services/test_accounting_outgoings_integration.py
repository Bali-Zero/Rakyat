"""Real-PostgreSQL tests of the outgoings reading (owner 2026-11-21: justify the debits too).

The rows are written as the CIMB import writes them (kind, posted_at, raw_row). The texts follow
the bank's formats with invented names, codes and amounts.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backend.services.accounting.outgoings import load_outgoings, read_outgoing
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES
from backend.tests.services.test_accounting_reconcile_integration import (
    MIGRATION_375,
    MIGRATION_376,
    _drop_db,
    _forward_sql_of,
    _fresh_db,
)

ACCOUNT = ENTITIES[DEFAULT_ENTITY_KEY].bank_account_number
TRANSFER = "OCTO Biz TO CONTOH PENERIMA IBI261121000000001 CENAIDJA Reimburse PNBP an. Contoh"


@pytest.fixture
async def db():
    conn, dbname, user = await _fresh_db()
    try:
        await conn.execute(_forward_sql_of(MIGRATION_375))
        await conn.execute(_forward_sql_of(MIGRATION_376))
        yield conn
    finally:
        await _drop_db(conn, dbname, user)


async def _statement(conn, account=ACCOUNT) -> int:
    return await conn.fetchval(
        "INSERT INTO bank_statements (source_format, account_number) VALUES ('csv', $1) RETURNING id",
        account,
    )


async def _row(
    conn,
    sid: int,
    kind: str | None,
    amount: int,
    text: str,
    at: datetime,
    *,
    direction="debit",
    status="unmatched",
    payment_type="TR TO REMITT",
    reference: str | None = None,
) -> int:
    return await conn.fetchval(
        "INSERT INTO bank_transactions (statement_id, txn_date, posted_at, amount_idr, direction,"
        " kind, description, raw_row, reconciled_status)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9) RETURNING id",
        sid,
        at.date(),
        at,
        amount,
        direction,
        kind,
        text,
        # A dict: the connection's jsonb codec encodes it, as the import's connection does.
        {"Payment Type": payment_type, "Other Reference": reference},
        status,
    )


@pytest.mark.parametrize(
    ("kind", "text", "expected"),
    [
        (
            "transfer_out",
            TRANSFER,
            {
                "label": "Transfer",
                "payee": "CONTOH PENERIMA",
                "purpose": "Reimburse PNBP an. Contoh",
                "detail": "Reimburse PNBP an. Contoh",
            },
        ),
        (
            "transfer_out",
            "OCTO Biz TO CONTOH PENERIMA IBI261121000000002 BNINIDJA",
            {
                "label": "Transfer",
                "payee": "CONTOH PENERIMA",
                "purpose": "",
                "detail": "no purpose written",
            },
        ),
        (
            "government_fee",
            "EMPN BILLING  820261120000001  ",
            {
                "label": "PNBP",
                "billing_code": "820261120000001",
                "detail": "e-billing 820261120000001",
            },
        ),
        (
            "cash_withdrawal",
            "BDS          CLEARING Bali - Contoh 1910 1910001CONTOH PENARIK",
            {
                "label": "Cash withdrawal",
                "cheque": "1910001",
                "payee": "CONTOH PENARIK",
                "detail": "Bali - Contoh 1910, cheque 1910001",
            },
        ),
        (
            "cash_withdrawal",
            "BDS          CLEARING Bali - Contoh Graha 1501001",
            {
                "label": "Cash withdrawal",
                "cheque": "1501001",
                "payee": None,
                "detail": "Bali - Contoh Graha, cheque 1501001",
            },
        ),
        ("payroll", "BULAN OKTOBER", {"label": "Payroll", "detail": "BULAN OKTOBER"}),
        ("bank_fee", "", {"label": "Bank charges", "detail": "MONTHLY ADMIN FEE"}),
        (
            "other",
            "BizChannel  BILL 400000000000 BPJSTK 2026112000000000",
            {"label": "BPJS", "detail": "BizChannel  BILL 400000000000 BPJSTK 2026112000000000"},
        ),
        (
            "transfer_out",
            "an unexpected text",
            {"label": "Transfer", "detail": "an unexpected text"},
        ),
        ("mystery", "x", {"label": "Other", "detail": "x"}),
    ],
)
def test_the_bank_text_is_read_apart(kind, text, expected) -> None:
    payment_type = "MONTHLY ADMIN FEE" if kind == "bank_fee" else "X"
    assert read_outgoing(kind, text, payment_type) == expected


async def test_only_unjustified_debits_newest_first_with_totals(db) -> None:
    sid = await _statement(db)
    t1 = datetime(2026, 11, 20, 9, 0, 0)
    old = await _row(db, sid, "government_fee", 5_000_000, "EMPN BILLING  820261119000001", t1)
    new = await _row(
        db, sid, "government_fee", 1_000_000, "EMPN BILLING  820261121000001", t1.replace(day=21)
    )
    same_day_later = await _row(db, sid, "payroll", 7_000_000, "BULAN OKTOBER", t1.replace(hour=18))
    await _row(db, sid, "client_credit", 9_000_000, "BFS FR CONTOH", t1, direction="credit")
    await _row(db, sid, "government_fee", 2_000_000, "EMPN BILLING  1", t1, status="ignored")
    await _row(db, sid, "transfer_out", 3_000_000, TRANSFER, t1, status="manual")

    result = await load_outgoings(db)

    assert [i["id"] for i in result["items"]] == [new, same_day_later, old]
    assert result["by_kind"] == [
        {"kind": "payroll", "label": "Payroll", "n": 1, "total_idr": 7_000_000},
        {"kind": "government_fee", "label": "PNBP", "n": 2, "total_idr": 6_000_000},
    ]
    assert (result["n"], result["total_idr"]) == (3, 13_000_000)
    assert (result["n_justified"], result["justified_total_idr"]) == (0, 0)
    first = result["items"][0]
    assert first["posted_at"] is not None and first["txn_date"] == date(2026, 11, 21)
    assert {k: v for k, v in first.items() if k not in ("posted_at", "txn_date")} == {
        "id": new,
        "amount_idr": 1_000_000,
        "kind": "government_fee",
        "description": "EMPN BILLING  820261121000001",
        "company_key": DEFAULT_ENTITY_KEY,
        "reading": {
            "label": "PNBP",
            "billing_code": "820261121000001",
            "detail": "e-billing 820261121000001",
            "code_debits": 1,
        },
        "fee_of_txn_id": None,
        "state": "to_justify",
        "justification": None,
    }


async def test_a_fee_is_paired_with_the_transfer_of_its_bank_reference(db) -> None:
    sid = await _statement(db)
    other_sid = await _statement(db)
    at = datetime(2026, 11, 21, 13, 19, 40)
    commission = "REMITTANCE COMMISSION"
    transfer = await _row(db, sid, "transfer_out", 12_000_000, TRANSFER, at, reference="R1")
    # Another transfer in the same second, under its own reference.
    neighbour = await _row(
        db,
        sid,
        "transfer_out",
        3_000_000,
        TRANSFER.replace("CONTOH PENERIMA", "B"),
        at,
        reference="R2",
    )
    fee = await _row(
        db, sid, "transfer_fee", 2_500, TRANSFER, at, payment_type=commission, reference="R1"
    )
    lone_fee = await _row(
        db, sid, "transfer_fee", 2_500, "x", at, payment_type=commission, reference="R9"
    )
    no_ref_fee = await _row(db, sid, "transfer_fee", 2_500, "x", at, payment_type=commission)
    # Same reference, another statement: never paired across statements.
    other_fee = await _row(db, other_sid, "transfer_fee", 2_500, "x", at, reference="R1")

    by_id = {i["id"]: i for i in (await load_outgoings(db))["items"]}

    assert by_id[fee]["fee_of_txn_id"] == transfer
    assert by_id[fee]["reading"] == {
        "label": "Transfer fee",
        "payee": "CONTOH PENERIMA",
        "detail": "fee of the transfer to CONTOH PENERIMA",
    }
    for unpaired in (lone_fee, no_ref_fee, other_fee):
        assert by_id[unpaired]["fee_of_txn_id"] is None
        assert by_id[unpaired]["reading"] == {"label": "Transfer fee", "detail": "x"}
    assert by_id[transfer]["fee_of_txn_id"] is None and by_id[neighbour]["fee_of_txn_id"] is None


async def test_a_reference_shared_by_two_transfers_pairs_no_fee(db) -> None:
    sid = await _statement(db)
    at = datetime(2026, 11, 21, 10, 0, 0)
    await _row(db, sid, "transfer_out", 1_000_000, TRANSFER, at, reference="R1")
    await _row(db, sid, "transfer_out", 2_000_000, TRANSFER, at, reference="R1")
    fee = await _row(db, sid, "transfer_fee", 2_500, "x", at, reference="R1")
    by_id = {i["id"]: i for i in (await load_outgoings(db))["items"]}
    assert by_id[fee]["fee_of_txn_id"] is None


async def test_the_limit_never_makes_a_shared_reference_look_unique(db) -> None:
    """Gate 555 round 1: two transfers under one reference, the limit hiding one of them."""
    sid = await _statement(db)
    older = datetime(2026, 11, 20, 10, 0, 0)
    await _row(db, sid, "transfer_out", 1_000_000, TRANSFER, older, reference="R1")
    newer = older.replace(day=21)
    transfer = await _row(db, sid, "transfer_out", 2_000_000, TRANSFER, newer, reference="R1")
    fee = await _row(db, sid, "transfer_fee", 2_500, "x", newer, reference="R1")
    listed = (await load_outgoings(db, limit=2))["items"]
    assert [i["id"] for i in listed] == [fee, transfer]
    assert listed[0]["fee_of_txn_id"] is None
    assert listed[0]["reading"] == {"label": "Transfer fee", "detail": "x"}


async def test_a_fee_pairs_with_its_transfer_even_when_that_transfer_is_not_listed(db) -> None:
    sid = await _statement(db)
    at = datetime(2026, 11, 21, 10, 0, 0)
    justified = await _row(
        db, sid, "transfer_out", 1_000_000, TRANSFER, at, reference="R1", status="manual"
    )
    beyond = await _row(
        db, sid, "transfer_out", 1_000_000, TRANSFER, at.replace(day=1), reference="R2"
    )
    fee1 = await _row(db, sid, "transfer_fee", 2_500, "x", at, reference="R1")
    fee2 = await _row(db, sid, "transfer_fee", 2_500, "x", at.replace(day=2), reference="R2")
    by_id = {i["id"]: i for i in (await load_outgoings(db, limit=2))["items"]}
    assert set(by_id) == {fee1, fee2}
    assert (by_id[fee1]["fee_of_txn_id"], by_id[fee2]["fee_of_txn_id"]) == (justified, beyond)
    assert by_id[fee1]["reading"]["payee"] == "CONTOH PENERIMA"


async def test_an_account_no_company_owns_is_other_and_the_limit_bounds_the_list(db) -> None:
    sid = await _statement(db, account="000-NOT-OURS")
    at = datetime(2026, 11, 21, 10, 0, 0)
    for minute in range(3):
        await _row(db, sid, "bank_fee", 10_000, "", at.replace(minute=minute))
    result = await load_outgoings(db, limit=2)
    assert [i["company_key"] for i in result["items"]] == ["other", "other"]
    assert (result["n"], result["total_idr"]) == (3, 30_000)


async def test_nothing_to_justify(db) -> None:
    assert await load_outgoings(db) == {
        "items": [],
        "by_kind": [],
        "n": 0,
        "total_idr": 0,
        "n_justified": 0,
        "justified_total_idr": 0,
    }


async def test_month_bounds_filter_before_limit_and_totals(db) -> None:
    sid = await _statement(db)
    for when, amount in [
        (datetime(2026, 10, 31), 100),
        (datetime(2026, 11, 1), 200),
        (datetime(2026, 11, 30), 300),
        (datetime(2026, 12, 1), 400),
    ]:
        await _row(db, sid, "bank_fee", amount, "MONTH BOUNDARY", when)
    scoped = await load_outgoings(db, month=date(2026, 11, 1), limit=1)
    assert [i["txn_date"] for i in scoped["items"]] == [date(2026, 11, 30)]
    assert (scoped["n"], scoped["total_idr"]) == (2, 500)
    history = await load_outgoings(db)
    assert (history["n"], history["total_idr"]) == (4, 1000)
