"""A transfer settles only invoices of the company whose account it reached (owner rule d).

Real PostgreSQL (the reconcile stubs plus migrations 238, 239, 355 and 363); skipped when none is
reachable. Every value is invented. A refusal leaves every row of every table as it was.
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.services.accounting.reconcile_service import (
    ReconcileError,
    confirm_payment,
    link_recorded,
)
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES
from backend.tests.services.test_accounting_reconcile_integration import _drop_db, _fresh_db

WHO = "accountant@example.test"
TABLES = (
    "clients",
    "practices",
    "invoices",
    "bank_statements",
    "bank_transactions",
    "weekly_cashout",
    "reconciliation_log",
)
OTHER_KEY = next(k for k in ENTITIES if k != DEFAULT_ENTITY_KEY)
ANOTHER_COMPANY = "invoice {inv} was issued by another company than the owner of the account"
NO_COMPANY = "reached an account no issuing company owns"


@pytest.fixture
async def db():
    conn, dbname, user = await _fresh_db()
    try:
        yield conn
    finally:
        await _drop_db(conn, dbname, user)


async def _full(conn) -> dict[str, list[dict]]:
    return {
        t: [dict(r) for r in await conn.fetch(f"SELECT * FROM {t} ORDER BY id")] for t in TABLES
    }


async def _practice(conn, *, status="unpaid", issuer: str | None = None, invoice=True):
    cid = await conn.fetchval(
        "INSERT INTO clients (full_name) VALUES ('Contoh Klien') RETURNING id"
    )
    pid = await conn.fetchval(
        "INSERT INTO practices (client_id, payment_status) VALUES ($1, $2) RETURNING id",
        cid,
        status,
    )
    iid = None
    if invoice:
        iid = await conn.fetchval(
            "INSERT INTO invoices (practice_id, invoice_number, amount_idr, issuer_entity_key)"
            " VALUES ($1, 'INV-C', 4000000, $2) RETURNING id",
            pid,
            issuer,
        )
    return pid, iid


async def _transfer(conn, account: str | None) -> int:
    sid = await conn.fetchval(
        "INSERT INTO bank_statements (source_format, account_number) VALUES ('csv', $1) RETURNING id",
        account,
    )
    return await conn.fetchval(
        "INSERT INTO bank_transactions (statement_id, txn_date, description, amount_idr, direction)"
        " VALUES ($1, $2, 'TRF Contoh Klien', 4000000, 'credit') RETURNING id",
        sid,
        date(2026, 11, 17),
    )


def _account(key: str) -> str:
    return ENTITIES[key].bank_account_number


async def _confirm(conn, txid, pid, iid):
    return await confirm_payment(
        conn,
        bank_txn_id=txid,
        practice_id=pid,
        invoice_id=iid,
        amount_applied_idr=4_000_000,
        new_status="paid",
        confirmed_by=WHO,
        movement_date=date(2026, 11, 17),
    )


async def _refused(conn, action, match: str) -> None:
    before = await _full(conn)
    with pytest.raises(ReconcileError, match=match):
        await action()
    assert await _full(conn) == before


# ── confirm ─────────────────────────────────────────────────────────────────────────────────


async def test_a_transfer_never_confirms_another_companys_invoice(db) -> None:
    pid, iid = await _practice(db, issuer=OTHER_KEY)
    txid = await _transfer(db, _account(DEFAULT_ENTITY_KEY))
    await _refused(db, lambda: _confirm(db, txid, pid, iid), ANOTHER_COMPANY.format(inv=iid))


async def test_an_invoice_with_no_issuer_is_the_default_companys(db) -> None:
    pid, iid = await _practice(db, issuer=None)
    txid = await _transfer(db, _account(OTHER_KEY))
    await _refused(db, lambda: _confirm(db, txid, pid, iid), ANOTHER_COMPANY.format(inv=iid))

    pid2, iid2 = await _practice(db, issuer=None)
    txid2 = await _transfer(db, f"{_account(DEFAULT_ENTITY_KEY)}-IDR")
    await _confirm(db, txid2, pid2, iid2)
    assert (
        await db.fetchval("SELECT matched_invoice_id FROM bank_transactions WHERE id = $1", txid2)
        == iid2
    )


@pytest.mark.parametrize("key", sorted(ENTITIES))
async def test_each_company_confirms_its_own_invoice(db, key) -> None:
    pid, iid = await _practice(db, issuer=key)
    txid = await _transfer(db, _account(key))
    await _confirm(db, txid, pid, iid)
    row = await db.fetchrow(
        "SELECT reconciled_status, matched_invoice_id FROM bank_transactions WHERE id = $1", txid
    )
    assert (row["reconciled_status"], row["matched_invoice_id"]) == ("matched", iid)
    assert await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", pid) == "paid"


@pytest.mark.parametrize("account", [None, "", "0000000001", "9" + "0" * 9])
async def test_an_account_no_company_owns_confirms_nothing(db, account) -> None:
    pid, iid = await _practice(db, issuer=None)
    txid = await _transfer(db, account)
    await _refused(db, lambda: _confirm(db, txid, pid, iid), NO_COMPANY)


async def test_with_no_invoice_named_every_invoice_of_the_practice_must_be_the_companys(db) -> None:
    pid, _ = await _practice(db, issuer=OTHER_KEY)
    txid = await _transfer(db, _account(DEFAULT_ENTITY_KEY))
    await _refused(db, lambda: _confirm(db, txid, pid, None), "was issued by another company")

    bare, _ = await _practice(db, invoice=False)
    await _refused(db, lambda: _confirm(db, txid, bare, None), f"practice {bare} has no invoice")

    own, _ = await _practice(db, issuer=DEFAULT_ENTITY_KEY)
    await _confirm(db, txid, own, None)
    assert await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", own) == "paid"


async def test_a_confirmation_without_a_transfer_is_not_compared(db) -> None:
    pid, iid = await _practice(db, issuer=OTHER_KEY)
    await _confirm(db, None, pid, iid)
    assert await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", pid) == "paid"


# ── link to a payment already recorded ──────────────────────────────────────────────────────


async def test_a_transfer_never_links_to_another_companys_recorded_payment(db) -> None:
    pid, iid = await _practice(db, status="paid", issuer=OTHER_KEY)
    txid = await _transfer(db, _account(DEFAULT_ENTITY_KEY))
    action = lambda: link_recorded(
        db, bank_txn_id=txid, practice_id=pid, invoice_id=iid, linked_by=WHO
    )  # noqa: E731
    await _refused(db, action, ANOTHER_COMPANY.format(inv=iid))

    nobody = await _transfer(db, "0000000001")
    action = lambda: link_recorded(
        db, bank_txn_id=nobody, practice_id=pid, invoice_id=iid, linked_by=WHO
    )  # noqa: E731
    await _refused(db, action, NO_COMPANY)


async def test_a_transfer_links_to_its_own_companys_recorded_payment(db) -> None:
    pid, iid = await _practice(db, status="paid", issuer=OTHER_KEY)
    txid = await _transfer(db, _account(OTHER_KEY))
    await link_recorded(db, bank_txn_id=txid, practice_id=pid, invoice_id=iid, linked_by=WHO)
    row = await db.fetchrow(
        "SELECT reconciled_status, matched_invoice_id FROM bank_transactions WHERE id = $1", txid
    )
    assert (row["reconciled_status"], row["matched_invoice_id"]) == ("manual", iid)
