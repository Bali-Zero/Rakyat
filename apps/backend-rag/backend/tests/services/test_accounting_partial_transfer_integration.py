"""Real-PostgreSQL tests of a transfer applied in parts (owner rule E, M2): an overpayment applies
at most the balance of an invoice and the excess stays in the inbox to be matched.

What a transfer has applied is derived from reconciliation_log (TRANSFER_APPLIED_SQL), never
stored: a partly applied transfer is `unmatched` with no ids, a closed one is `matched`. All
names and amounts are invented.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

import backend.app.routers.accounting as accounting_module
from backend.services.accounting.done_today_service import load_done_today
from backend.services.accounting.reconcile_service import (
    TRANSFER_APPLIED_SQL,
    ReconcileError,
    confirm_payment,
    ignore_transaction,
    link_recorded,
    undo_confirmation,
)
from backend.tests.services.test_accounting_reconcile_integration import (
    DEFAULT_COMPANY_STATEMENT_SQL,
    _drop_db,
    _fresh_db,
)

WHO = "accountant@example.test"
DAY = date(2026, 6, 25)
M = 1_000_000
ADMIN = {"id": "acct-admin", "email": "admin@example.test", "role": "user"}


@pytest.fixture
async def db():
    conn, dbname, user = await _fresh_db()
    try:
        yield conn
    finally:
        await _drop_db(conn, dbname, user)


class _Pool:
    """Just enough pool for a router function: acquire() yields the one test connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.fixture(autouse=True)
def _open_access(monkeypatch):
    monkeypatch.setattr(accounting_module, "_require_accounting_access", lambda user: None)


async def _world(conn, invoices=(10 * M, 10 * M, 5 * M), transfer=25 * M):
    """One credit of `transfer` and one case per invoice (its own client, invoice INV-<n>)."""
    pids, iids = [], []
    for n, amount in enumerate(invoices):
        cid = await conn.fetchval(
            "INSERT INTO clients (full_name) VALUES ($1) RETURNING id", f"Contoh Klien {n}"
        )
        pid = await conn.fetchval(
            "INSERT INTO practices (client_id, payment_status) VALUES ($1, 'unpaid') RETURNING id", cid
        )
        iids.append(
            await conn.fetchval(
                "INSERT INTO invoices (practice_id, invoice_number, amount_idr)"
                " VALUES ($1, $2, $3) RETURNING id",
                pid, f"INV-{n}", amount,
            )
        )
        pids.append(pid)
    sid = await conn.fetchval(DEFAULT_COMPANY_STATEMENT_SQL)
    txid = await conn.fetchval(
        "INSERT INTO bank_transactions (statement_id, txn_date, description, amount_idr, direction)"
        " VALUES ($1, $2, 'TRF Contoh Klien', $3, 'credit') RETURNING id",
        sid, DAY, transfer,
    )
    return pids, iids, txid


def _apply(conn, txid, pid, iid, amount, *, status="paid", **kw):
    return confirm_payment(
        conn, bank_txn_id=txid, practice_id=pid, invoice_id=iid, amount_applied_idr=amount,
        new_status=status, confirmed_by=WHO, movement_date=DAY, **kw,
    )


async def _bank(conn, txid):
    return await conn.fetchrow(
        "SELECT reconciled_status, matched_practice_id, matched_invoice_id,"
        f" {TRANSFER_APPLIED_SQL} AS applied FROM bank_transactions bt WHERE bt.id = $1",
        txid,
    )


async def _state(conn):
    return (
        [tuple(r) for r in await conn.fetch("SELECT id, payment_status, paid_amount FROM practices ORDER BY id")],
        [tuple(r) for r in await conn.fetch("SELECT id, paid_amount_idr FROM invoices ORDER BY id")],
        await conn.fetchval("SELECT count(*) FROM weekly_cashout"),
        await conn.fetchval("SELECT count(*) FROM reconciliation_log"),
        [tuple(r) for r in await conn.fetch(
            "SELECT id, reconciled_status, matched_practice_id, matched_invoice_id FROM bank_transactions ORDER BY id")],
    )


async def _inbox_item(conn, txid):
    items = await accounting_module.reconciliation_inbox(
        limit=200, db_pool=_Pool(conn), current_user=ADMIN, month=None
    )
    return next((i for i in items if i["txn_id"] == txid), None)


async def test_a_lump_sum_applies_one_invoice_at_a_time_and_closes_on_the_last(db) -> None:
    (p0, p1, p2), (i0, i1, i2), txid = await _world(db)

    await _apply(db, txid, p0, i0, 10 * M)
    bank = await _bank(db, txid)
    assert (bank["reconciled_status"], bank["matched_practice_id"], bank["matched_invoice_id"]) == (
        "unmatched", None, None,
    )
    assert bank["applied"] == 10 * M
    assert (await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", p0)) == "paid"
    # the candidates of the remainder rank against 15M, not 25M
    body = await accounting_module.match_candidates(txid, db_pool=_Pool(db), current_user=ADMIN)
    assert body["transfer_amount_idr"] == 15 * M
    assert {c["invoice_id"]: c["amount_delta_idr"] for c in body["candidates"]} == {
        i1: 5 * M, i2: 10 * M,
    }
    item = await _inbox_item(db, txid)
    assert (item["amount_idr"], item["applied_idr"]) == (25 * M, 10 * M)

    await _apply(db, txid, p1, i1, 10 * M)
    bank = await _bank(db, txid)
    assert (bank["reconciled_status"], bank["matched_practice_id"], bank["applied"]) == ("unmatched", None, 20 * M)
    # 5M are left and the 5M invoice is an exact candidate of the remainder (the whole 25M is not)
    item = await _inbox_item(db, txid)
    assert (item["applied_idr"], item["open_candidates"], item["light"] != "red") == (20 * M, 1, True)

    await _apply(db, txid, p2, i2, 5 * M)
    bank = await _bank(db, txid)
    assert (bank["reconciled_status"], bank["matched_practice_id"], bank["matched_invoice_id"]) == (
        "matched", p2, i2,
    )
    assert bank["applied"] == 25 * M
    assert await _inbox_item(db, txid) is None


@pytest.mark.parametrize(("short", "status"), [(0, "matched"), (500, "matched"), (1_000, "matched"), (1_001, "unmatched")])
async def test_the_transfer_closes_when_what_is_left_is_within_the_matcher_tolerance(db, short, status) -> None:
    (pid,), (iid,), txid = await _world(db, invoices=(4 * M,), transfer=4 * M)
    await _apply(db, txid, pid, iid, 4 * M - short)
    bank = await _bank(db, txid)
    assert bank["reconciled_status"] == status
    assert (bank["matched_practice_id"], bank["matched_invoice_id"]) == (
        (pid, iid) if status == "matched" else (None, None)
    )


async def test_an_application_beyond_what_is_left_is_refused_and_nothing_is_written(db) -> None:
    (p0, p1, _), (i0, i1, _), txid = await _world(db)
    with pytest.raises(ReconcileError, match=f"bank transaction {txid} has only"):
        await _apply(db, txid, p0, i0, 25 * M + 1)
    await _apply(db, txid, p0, i0, 10 * M)
    before = await _state(db)
    with pytest.raises(ReconcileError, match=f"bank transaction {txid} has only"):
        await _apply(db, txid, p1, i1, 15 * M + 1)  # 15M are left
    assert await _state(db) == before
    await _apply(db, txid, p1, i1, 10 * M)  # the exact balance of the invoice still goes through


async def test_a_partly_applied_transfer_is_neither_ignored_nor_linked(db) -> None:
    (p0, p1, _), (i0, i1, _), txid = await _world(db)
    await _apply(db, txid, p0, i0, 10 * M)
    before = await _state(db)
    with pytest.raises(ReconcileError, match="undo its confirmations first"):
        await ignore_transaction(db, bank_txn_id=txid, reason="bukan klien", ignored_by=WHO)
    with pytest.raises(ReconcileError, match="undo its confirmations first"):
        await link_recorded(db, bank_txn_id=txid, practice_id=p0, invoice_id=i0, linked_by=WHO)
    assert await _state(db) == before
    await undo_confirmation(db, bank_txn_id=txid, undone_by=WHO)
    result = await ignore_transaction(db, bank_txn_id=txid, reason="bukan klien", ignored_by=WHO)
    assert result.reconciled_status == "ignored"  # an undone confirmation does not stand


async def test_undo_takes_back_the_newest_application_and_unwinds_last_first(db) -> None:
    (p0, p1, _), (i0, i1, _), txid = await _world(db)
    await _apply(db, txid, p0, i0, 10 * M)
    await _apply(db, txid, p1, i1, 10 * M)

    await undo_confirmation(db, bank_txn_id=txid, undone_by=WHO)
    bank = await _bank(db, txid)
    assert (bank["reconciled_status"], bank["matched_practice_id"], bank["applied"]) == ("unmatched", None, 10 * M)
    assert (await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", p1)) == "unpaid"
    assert (await db.fetchval("SELECT paid_amount_idr FROM invoices WHERE id = $1", i1)) == 0
    assert (await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", p0)) == "paid"
    # the remainder is back to 15M in the inbox and in the candidates
    assert (await _inbox_item(db, txid))["applied_idr"] == 10 * M
    body = await accounting_module.match_candidates(txid, db_pool=_Pool(db), current_user=ADMIN)
    assert body["transfer_amount_idr"] == 15 * M

    await undo_confirmation(db, bank_txn_id=txid, undone_by=WHO)
    assert (await _bank(db, txid))["applied"] == 0
    assert (await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", p0)) == "unpaid"
    with pytest.raises(ReconcileError, match="is not confirmed"):
        await undo_confirmation(db, bank_txn_id=txid, undone_by=WHO)


async def test_the_closing_application_is_undone_first_and_reopens_the_transfer(db) -> None:
    (p0, p1, p2), (i0, i1, i2), txid = await _world(db)
    for pid, iid, amount in ((p0, i0, 10 * M), (p1, i1, 10 * M), (p2, i2, 5 * M)):
        await _apply(db, txid, pid, iid, amount)
    assert (await _bank(db, txid))["reconciled_status"] == "matched"

    await undo_confirmation(db, bank_txn_id=txid, undone_by=WHO)
    bank = await _bank(db, txid)
    assert (bank["reconciled_status"], bank["matched_practice_id"], bank["matched_invoice_id"]) == (
        "unmatched", None, None,
    )
    assert bank["applied"] == 20 * M
    assert (await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", p2)) == "unpaid"
    assert (await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", p1)) == "paid"


IN_WINDOW = datetime(2026, 11, 18, 3, tzinfo=UTC)
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=ZoneInfo("Asia/Makassar"))


async def test_done_today_lists_a_partly_applied_transfer_with_the_invoice_of_its_newest_confirmation(db) -> None:
    (p0, p1, _), (i0, i1, _), txid = await _world(db)
    await _apply(db, txid, p0, i0, 10 * M)
    await _apply(db, txid, p1, i1, 10 * M)
    await db.execute("UPDATE reconciliation_log SET confirmed_at = $1", IN_WINDOW)

    done = await load_done_today(db, NOW)

    assert done["counts"] == {"confirmed": 1, "linked": 0, "ignored": 0, "total": 1}
    (item,) = done["items"]
    assert (item["txn_id"], item["action"], item["amount_idr"]) == (txid, "confirmed", 25 * M)
    assert (item["invoice_number"], item["client_name"]) == ("INV-1", "Contoh Klien 1")


async def test_a_transfer_whose_only_confirmation_was_undone_is_not_done_today(db) -> None:
    (p0, _, _), (i0, _, _), txid = await _world(db)
    await _apply(db, txid, p0, i0, 10 * M)
    await undo_confirmation(db, bank_txn_id=txid, undone_by=WHO)
    await db.execute("UPDATE reconciliation_log SET confirmed_at = $1", IN_WINDOW)
    assert (await load_done_today(db, NOW))["counts"]["total"] == 0


async def test_a_whole_transfer_confirmed_at_once_behaves_as_before(db) -> None:
    (pid,), (iid,), txid = await _world(db, invoices=(4 * M,), transfer=4 * M)
    await _apply(db, txid, pid, iid, 4 * M)
    bank = await _bank(db, txid)
    assert (bank["reconciled_status"], bank["matched_practice_id"], bank["matched_invoice_id"]) == (
        "matched", pid, iid,
    )
    with pytest.raises(ReconcileError, match=f"bank transaction {txid} is not unmatched"):
        await _apply(db, txid, pid, iid, 4 * M)
    await undo_confirmation(db, bank_txn_id=txid, undone_by=WHO)
    assert (await _bank(db, txid))["reconciled_status"] == "unmatched"
