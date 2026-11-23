"""Real-PostgreSQL tests of the three actions that move no money (spec A1 to A5).

link_recorded, ignore_transaction and reopen_transaction, each run against a database built
the way test_accounting_reconcile_integration builds it (same helpers). Every refusal is
proved with a full before/after snapshot of every table; every success pins the exact rows
written. All names and amounts are invented.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, timezone

import asyncpg
import pytest

from backend.services.accounting.reconcile_service import (
    LINK_NOTE,
    REASON_REFUSAL,
    REOPEN_NOTE,
    REOPEN_REVERSED_NOTE,
    ReconcileError,
    confirm_payment,
    ignore_transaction,
    link_recorded,
    reopen_transaction,
)
from backend.tests.services.test_accounting_reconcile_integration import (
    DEFAULT_COMPANY_STATEMENT_SQL,
    _connect,
    _drop_db,
    _fresh_db,
)

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
MONEY_TABLES = ("practices", "invoices", "weekly_cashout")
_LOG_PARK = (
    "LOCK TABLE reconciliation_log IN SHARE MODE"  # A holds its locks, waits to write its log row
)


async def _full(conn) -> dict[str, list[dict]]:
    """Every row of every table the actions could touch, whole."""
    return {
        t: [dict(r) for r in await conn.fetch(f"SELECT * FROM {t} ORDER BY id")] for t in TABLES
    }


async def _practice(conn, status: str | None, amount: int = 4_000_000) -> tuple[int, int]:
    """One practice with the given payment status and one 4,000,000 invoice."""
    cid = await conn.fetchval(
        "INSERT INTO clients (full_name) VALUES ('Contoh Klien') RETURNING id"
    )
    pid = await conn.fetchval(
        "INSERT INTO practices (client_id, payment_status) VALUES ($1, $2) RETURNING id",
        cid,
        status,
    )
    iid = await conn.fetchval(
        "INSERT INTO invoices (practice_id, invoice_number, amount_idr) VALUES ($1, 'INV-L', $2) RETURNING id",
        pid,
        amount,
    )
    return pid, iid


async def _bank(
    conn, *, direction="credit", status="unmatched", practice=None, invoice=None, amount=4_000_000
) -> int:
    sid = await conn.fetchval("SELECT id FROM bank_statements ORDER BY id LIMIT 1")
    if sid is None:
        sid = await conn.fetchval(DEFAULT_COMPANY_STATEMENT_SQL)
    return await conn.fetchval(
        "INSERT INTO bank_transactions (statement_id, txn_date, description, amount_idr, direction,"
        " reconciled_status, matched_practice_id, matched_invoice_id)"
        " VALUES ($1, $2, 'TRF Contoh Klien', $7, $3, $4, $5, $6) RETURNING id",
        sid,
        date(2026, 6, 25),
        direction,
        status,
        practice,
        invoice,
        amount,
    )


async def _forbid_money_writes(conn, tables=MONEY_TABLES) -> None:
    """From now on ANY statement that writes to one of `tables` (by default practices, invoices
    and weekly_cashout) fails."""
    await conn.execute(
        "CREATE FUNCTION forbid_write() RETURNS trigger LANGUAGE plpgsql AS"
        " $$ BEGIN RAISE EXCEPTION 'money table written'; END $$"
    )
    for t in tables:
        await conn.execute(
            f"CREATE TRIGGER no_write_{t} BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {t}"
            " FOR EACH STATEMENT EXECUTE FUNCTION forbid_write()"
        )


@pytest.fixture
async def db():
    conn, dbname, user = await _fresh_db()
    try:
        yield conn, dbname
    finally:
        await _drop_db(conn, dbname, user)


def _ids(raw) -> list | None:
    """The invoice list exactly as the production codec decodes it: a list, never a string.

    It used to be `json.loads(raw)`, which turned the JSON string "[5]" that production
    stored into [5] and so hid the double encoding.
    """
    assert raw is None or isinstance(raw, list), f"invoice_ids decoded as {type(raw).__name__}"
    return raw


# ── A1 link ──────────────────────────────────────────────────────────────────────────────


async def test_a1_link_writes_the_bank_row_the_paid_date_one_cashout_row_and_one_log_row(
    db,
) -> None:
    """Owner rule (e): the case is not touched; the invoice is dated paid on the transfer's
    day, the transfer enters the cash-out once, and the log row points at that cash-out row."""
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank(conn)
    await _forbid_money_writes(conn, ("practices",))  # the case is never written by a link
    before = await _full(conn)

    res = await link_recorded(
        conn, bank_txn_id=txid, practice_id=pid, invoice_id=iid, linked_by=WHO
    )

    after = await _full(conn)
    for t in ("practices", "clients", "bank_statements"):
        assert after[t] == before[t], t
    expected = dict(before["bank_transactions"][0])
    expected.update(reconciled_status="manual", matched_practice_id=pid, matched_invoice_id=iid)
    assert after["bank_transactions"] == [expected]
    invoice = dict(before["invoices"][0])
    invoice.update(paid_date=datetime(2026, 6, 25, tzinfo=timezone.utc))
    assert after["invoices"] == [invoice]  # the paid date and nothing else
    assert len(after["weekly_cashout"]) == 1
    cash = after["weekly_cashout"][0]
    assert cash["recorded_at"] is not None
    assert {k: v for k, v in cash.items() if k not in ("id", "recorded_at")} == {
        "movement_date": date(2026, 6, 25),
        "week_label": "2026-W26",
        "direction": "in",
        "type": "invoice_payment",
        "amount_idr": 4_000_000,
        "pnbp_idr": 0,
        "urgent_idr": 0,
        "rptka_imta_idr": 0,
        "margin_idr": 0,
        "final_price_idr": 0,
        "original_currency": "IDR",
        "original_amount": None,
        "fx_rate": None,
        "fx_date": None,
        "pph_withheld_idr": 0,
        "bank_fee_idr": 0,
        "linked_practice_id": pid,
        "linked_invoice_id": iid,
        "linked_bank_txn_id": txid,
        "category": None,
        "description": None,
        "counterparty": None,
        "payment_method": "bank_transfer",
        "receipt_drive_file_id": None,
        "recorded_by": WHO,
        "reversed_by_id": None,
    }
    assert len(after["reconciliation_log"]) == 1
    log = after["reconciliation_log"][0]
    assert res.reconciliation_log_id == log["id"] and res.reconciled_status == "manual"
    assert _ids(log["invoice_ids"]) == [iid]
    assert {k: v for k, v in log.items() if k not in ("id", "invoice_ids", "confirmed_at")} == {
        "bank_txn_id": txid,
        "practice_id": pid,
        "cashout_id": cash["id"],
        "confirmed_by": WHO,
        "status_before": "paid",
        "status_after": "paid",
        "amount_applied_idr": 0,
        "pph_delta_idr": 0,
        "bank_fee_delta_idr": 0,
        "reversal_of_id": None,
        "note": LINK_NOTE,
        "before_state": None,  # only a confirmation records one (373)
    }
    assert LINK_NOTE == "linked to a payment already recorded; added to the cash-out"
    assert log["confirmed_at"] is not None


async def _expect_refusal(conn, call, message: str) -> None:
    before = await _full(conn)
    with pytest.raises(ReconcileError) as err:
        await call
    assert str(err.value) == message
    assert await _full(conn) == before  # A5: every table, every column, as it was


def _link(conn, tx, p, i):
    return link_recorded(conn, bank_txn_id=tx, practice_id=p, invoice_id=i, linked_by=WHO)


async def test_a1_every_refusal_names_only_the_ids_and_changes_nothing(db) -> None:
    conn, _ = db
    ppaid, ipaid = await _practice(conn, "paid")
    punpaid, iunpaid = await _practice(conn, "unpaid")
    ppartial, ipartial = await _practice(conn, "partial")
    pnull, inull = await _practice(conn, None)
    credit = await _bank(conn)
    debit = await _bank(conn, direction="debit")
    matched = await _bank(conn, status="matched", practice=ppaid, invoice=ipaid, amount=1)
    manual = await _bank(conn, status="manual", practice=ppaid, invoice=ipaid, amount=1)
    ignored = await _bank(conn, status="ignored")

    await _expect_refusal(
        conn, _link(conn, 999_001, ppaid, ipaid), "bank transaction 999001 not found"
    )
    await _expect_refusal(
        conn, _link(conn, debit, ppaid, ipaid), f"bank transaction {debit} is not a credit"
    )
    for tx in (matched, manual, ignored):
        await _expect_refusal(
            conn, _link(conn, tx, ppaid, ipaid), f"bank transaction {tx} is not unmatched"
        )
    await _expect_refusal(conn, _link(conn, credit, ppaid, 999_003), "invoice 999003 not found")
    await _expect_refusal(
        conn,
        _link(conn, credit, ppaid, iunpaid),
        f"invoice {iunpaid} does not belong to practice {ppaid}",
    )
    await _expect_refusal(conn, _link(conn, credit, 999_002, ipaid), "practice 999002 not found")
    for p, i in ((punpaid, iunpaid), (ppartial, ipartial), (pnull, inull)):
        await _expect_refusal(
            conn, _link(conn, credit, p, i), f"practice {p} has no recorded payment"
        )
    # the bank transaction is read first: with every id unknown it is the one named
    await _expect_refusal(
        conn, _link(conn, 999_004, 999_005, 999_006), "bank transaction 999004 not found"
    )


async def test_a1_a_practice_that_exists_no_more_is_refused_by_name(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    await conn.execute("DELETE FROM practices WHERE id = $1", pid)
    txid = await _bank(conn)
    await _expect_refusal(conn, _link(conn, txid, pid, iid), f"practice {pid} not found")


# the ceiling: this transfer plus what is attached may exceed the invoice by at most 1,000


@pytest.mark.parametrize(
    ("amount", "ok"),
    [(4_001_000, True), (4_001_001, False), (4_000_000, True), (1, True)],
)
async def test_a1_the_ceiling_at_its_boundary(db, amount, ok) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank(conn, amount=amount)
    if ok:
        assert (await _link(conn, txid, pid, iid)).reconciled_status == "manual"
    else:
        await _expect_refusal(
            conn,
            _link(conn, txid, pid, iid),
            f"bank transaction {txid} would exceed the amount of invoice {iid}",
        )


async def test_a1_the_ceiling_counts_what_is_attached_sum_exactly_plus_1000_or_plus_1001(
    db,
) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    await _bank(conn, status="manual", practice=pid, invoice=iid, amount=1_500_000)
    exact = await _bank(conn, amount=2_501_000)  # 1,500,000 + 2,501,000 = 4,001,000: at the limit
    over = await _bank(conn, amount=2_501_001)
    await _expect_refusal(
        conn,
        _link(conn, over, pid, iid),
        f"bank transaction {over} would exceed the amount of invoice {iid}",
    )
    assert (await _link(conn, exact, pid, iid)).reconciled_status == "manual"


async def test_a1_a_second_transfer_for_a_fully_attached_invoice_is_refused(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    await _link(conn, await _bank(conn), pid, iid)
    second = await _bank(conn)
    await _expect_refusal(
        conn,
        _link(conn, second, pid, iid),
        f"bank transaction {second} would exceed the amount of invoice {iid}",
    )


async def test_a1_two_part_transfers_are_accepted(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    await _link(conn, await _bank(conn, amount=1_500_000), pid, iid)
    await _link(conn, await _bank(conn, amount=2_500_000), pid, iid)
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM bank_transactions WHERE reconciled_status = 'manual'"
        )
        == 2
    )


async def test_a1_a_reopened_attachment_no_longer_counts(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    first = await _bank(conn)
    await _link(conn, first, pid, iid)
    second = await _bank(conn)
    await _expect_refusal(
        conn,
        _link(conn, second, pid, iid),
        f"bank transaction {second} would exceed the amount of invoice {iid}",
    )
    await reopen_transaction(conn, bank_txn_id=first, reopened_by=WHO)
    assert (await _link(conn, second, pid, iid)).reconciled_status == "manual"


async def test_a1_a_matched_row_from_confirm_payment_counts(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "unpaid")
    paid_tx = await _bank(conn)
    await confirm_payment(
        conn,
        bank_txn_id=paid_tx,
        practice_id=pid,
        invoice_id=iid,
        amount_applied_idr=4_000_000,
        new_status="paid",
        confirmed_by=WHO,
        margin_idr=4_000_000,
        movement_date=date(2026, 6, 25),
    )
    other = await _bank(conn)
    await _expect_refusal(
        conn,
        _link(conn, other, pid, iid),
        f"bank transaction {other} would exceed the amount of invoice {iid}",
    )


async def test_a1_ignored_and_unmatched_rows_do_not_count_toward_the_ceiling(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    # stale ids on rows that are not attached: they must not count
    await _bank(conn, status="ignored", practice=pid, invoice=iid)
    await _bank(conn, status="unmatched", practice=pid, invoice=iid)
    assert (await _link(conn, await _bank(conn), pid, iid)).reconciled_status == "manual"


@pytest.mark.parametrize("literal", ["NaN", None])
async def test_a1_an_invoice_without_a_usable_amount_is_refused(db, literal) -> None:
    conn, _ = db
    await conn.execute("ALTER TABLE invoices ALTER COLUMN amount_idr TYPE NUMERIC(15,2)")
    pid, iid = await _practice(conn, "paid")
    await conn.execute("UPDATE invoices SET amount_idr = $2::numeric WHERE id = $1", iid, literal)
    txid = await _bank(conn)
    before = await _full(conn)
    with pytest.raises(ReconcileError) as err:
        await _link(conn, txid, pid, iid)
    assert str(err.value) == f"invoice {iid} has no usable amount"
    after = await _full(conn)
    assert (after["bank_transactions"], after["reconciliation_log"]) == (
        before["bank_transactions"],
        before["reconciliation_log"],
    )


async def test_a1_the_invoice_amount_is_compared_exactly_in_numeric_cents(db) -> None:
    conn, _ = db
    await conn.execute("ALTER TABLE invoices ALTER COLUMN amount_idr TYPE NUMERIC(15,2)")
    pid, iid = await _practice(conn, "paid", amount=3_999_999)
    await conn.execute("UPDATE invoices SET amount_idr = 3000000.50 WHERE id = $1", iid)
    ok = await _bank(conn, amount=3_001_000)  # 3,000,000.50 + 1,000 = 3,001,000.50 >= 3,001,000
    over = await _bank(conn, amount=3_001_001)  # above by 0.50
    await _expect_refusal(
        conn,
        _link(conn, over, pid, iid),
        f"bank transaction {over} would exceed the amount of invoice {iid}",
    )
    assert (await _link(conn, ok, pid, iid)).reconciled_status == "manual"


# ── A2 ignore ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("direction", ["credit", "debit"])
async def test_a2_ignore_marks_the_row_and_writes_one_log_row_with_the_reason(
    db, direction
) -> None:
    conn, _ = db
    await _practice(conn, "paid")
    txid = await _bank(conn, direction=direction)
    await _forbid_money_writes(conn)
    before = await _full(conn)

    res = await ignore_transaction(
        conn, bank_txn_id=txid, reason="bunga bank, bukan klien", ignored_by=WHO
    )

    after = await _full(conn)
    for t in MONEY_TABLES + ("clients", "bank_statements"):
        assert after[t] == before[t], t
    expected = dict(before["bank_transactions"][0])
    expected.update(reconciled_status="ignored", matched_practice_id=None, matched_invoice_id=None)
    assert after["bank_transactions"] == [expected]
    assert len(after["reconciliation_log"]) == 1
    log = after["reconciliation_log"][0]
    assert res.reconciliation_log_id == log["id"] and res.reconciled_status == "ignored"
    assert {k: v for k, v in log.items() if k not in ("id", "confirmed_at")} == {
        "bank_txn_id": txid,
        "invoice_ids": None,
        "practice_id": None,
        "cashout_id": None,
        "confirmed_by": WHO,
        "status_before": None,
        "status_after": None,
        "amount_applied_idr": 0,
        "pph_delta_idr": 0,
        "bank_fee_delta_idr": 0,
        "reversal_of_id": None,
        "note": "bunga bank, bukan klien",
        "before_state": None,  # only a confirmation records one (373)
    }


async def test_a2_every_refusal_changes_nothing(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    unmatched = await _bank(conn)
    matched = await _bank(conn, status="matched", practice=pid, invoice=iid)
    manual = await _bank(conn, status="manual", practice=pid, invoice=iid)
    ignored = await _bank(conn, status="ignored")

    def ign(tx, reason="bukan klien"):
        return ignore_transaction(conn, bank_txn_id=tx, reason=reason, ignored_by=WHO)

    await _expect_refusal(conn, ign(999_001), "bank transaction 999001 not found")
    for tx in (matched, manual, ignored):
        await _expect_refusal(conn, ign(tx), f"bank transaction {tx} is not unmatched")
    await _expect_refusal(conn, ign(unmatched, "ab"), REASON_REFUSAL)
    await _expect_refusal(conn, ign(unmatched, "x" * 201), REASON_REFUSAL)
    # the bounds themselves are accepted
    await ign(unmatched, "abc")
    other = await _bank(conn)
    await ign(other, "x" * 200)


# ── A3 reopen ────────────────────────────────────────────────────────────────────────────


async def test_a3_reopen_a_linked_row_reverses_the_link_log_row_and_its_cashout_row(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank(conn)
    link = await link_recorded(
        conn, bank_txn_id=txid, practice_id=pid, invoice_id=iid, linked_by=WHO
    )
    await _forbid_money_writes(conn, ("practices",))
    before = await _full(conn)

    res = await reopen_transaction(conn, bank_txn_id=txid, reopened_by="other@example.test")

    after = await _full(conn)
    for t in ("practices", "clients", "bank_statements"):
        assert after[t] == before[t], t
    expected = dict(before["bank_transactions"][0])
    expected.update(
        reconciled_status="unmatched", matched_practice_id=None, matched_invoice_id=None
    )
    assert after["bank_transactions"] == [expected]
    invoice = dict(before["invoices"][0])
    invoice.update(paid_date=None)  # no cash-out row of this invoice is left standing
    assert after["invoices"] == [invoice]
    original, reversal = after["weekly_cashout"]
    assert original == {**before["weekly_cashout"][0], "reversed_by_id": reversal["id"]}
    assert {k: v for k, v in reversal.items() if k not in ("id", "recorded_at")} == {
        **{k: v for k, v in original.items() if k not in ("id", "recorded_at")},
        "description": f"reversal of cash-out row {original['id']}: link reopened",
        "recorded_by": "other@example.test",
        "reversed_by_id": original["id"],
    }
    assert (
        after["reconciliation_log"][:1] == before["reconciliation_log"]
    )  # the old row is immutable
    assert len(after["reconciliation_log"]) == 2
    log = after["reconciliation_log"][1]
    assert res.reconciliation_log_id == log["id"] and res.reconciled_status == "unmatched"
    assert {k: v for k, v in log.items() if k not in ("id", "confirmed_at")} == {
        "bank_txn_id": txid,
        "invoice_ids": None,
        "practice_id": None,
        "cashout_id": reversal["id"],
        "confirmed_by": "other@example.test",
        "status_before": None,
        "status_after": None,
        "amount_applied_idr": 0,
        "pph_delta_idr": 0,
        "bank_fee_delta_idr": 0,
        "reversal_of_id": link.reconciliation_log_id,
        "note": REOPEN_REVERSED_NOTE,
        "before_state": None,  # only a confirmation records one (373)
    }
    assert REOPEN_REVERSED_NOTE == "reopened; the link's cash-out row reversed"


async def test_a3_reopen_a_link_made_before_rule_e_writes_no_money_table(db) -> None:
    """A link recorded before owner rule (e) has no cash-out row: reopening it is what it was."""
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank(conn, status="manual", practice=pid, invoice=iid)
    old = await conn.fetchval(
        "INSERT INTO reconciliation_log (bank_txn_id, invoice_ids, practice_id, confirmed_by,"
        " status_before, status_after, amount_applied_idr, note)"
        " VALUES ($1, $2, $3, $4, 'paid', 'paid', 0,"
        " 'linked to a payment already recorded; no money moved') RETURNING id",
        txid,
        [iid],
        pid,
        WHO,
    )
    await _forbid_money_writes(conn)
    before = await _full(conn)

    res = await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)

    after = await _full(conn)
    for t in MONEY_TABLES + ("clients", "bank_statements"):
        assert after[t] == before[t], t
    log = await conn.fetchrow(
        "SELECT * FROM reconciliation_log WHERE id = $1", res.reconciliation_log_id
    )
    assert (log["reversal_of_id"], log["cashout_id"], log["note"]) == (old, None, REOPEN_NOTE)


async def test_a3_the_reversal_points_at_the_latest_log_row_of_that_bank_transaction(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    other_tx = await _bank(conn)
    txid = await _bank(conn)
    await ignore_transaction(
        conn, bank_txn_id=other_tx, reason="bukan klien", ignored_by=WHO
    )  # another tx
    first = await ignore_transaction(conn, bank_txn_id=txid, reason="bukan klien", ignored_by=WHO)
    r1 = await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)
    second = await link_recorded(
        conn, bank_txn_id=txid, practice_id=pid, invoice_id=iid, linked_by=WHO
    )
    r2 = await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)
    rev = {
        r["id"]: r["reversal_of_id"]
        for r in await conn.fetch("SELECT id, reversal_of_id FROM reconciliation_log")
    }
    assert rev[first.reconciliation_log_id] is None and rev[second.reconciliation_log_id] is None
    assert rev[r1.reconciliation_log_id] == first.reconciliation_log_id
    assert rev[r2.reconciliation_log_id] == second.reconciliation_log_id  # latest, not first


async def test_a3_reopen_an_ignored_row_and_a_row_with_no_log_at_all(db) -> None:
    conn, _ = db
    ignored = await _bank(conn, status="ignored")  # no log row exists for it
    res = await reopen_transaction(conn, bank_txn_id=ignored, reopened_by=WHO)
    log = await conn.fetchrow(
        "SELECT * FROM reconciliation_log WHERE id = $1", res.reconciliation_log_id
    )
    assert log["reversal_of_id"] is None and log["bank_txn_id"] == ignored
    assert (
        await conn.fetchval(
            "SELECT reconciled_status FROM bank_transactions WHERE id = $1", ignored
        )
        == "unmatched"
    )


async def test_a3_every_refusal_changes_nothing_and_a_matched_row_is_never_reopened(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "unpaid")
    unmatched = await _bank(conn)
    matched = await _bank(conn, status="matched", practice=pid, invoice=iid)
    await _expect_refusal(
        conn,
        reopen_transaction(conn, bank_txn_id=999_001, reopened_by=WHO),
        "bank transaction 999001 not found",
    )
    for tx in (unmatched, matched):
        await _expect_refusal(
            conn,
            reopen_transaction(conn, bank_txn_id=tx, reopened_by=WHO),
            f"bank transaction {tx} is not manual or ignored",
        )


async def test_a3_a_confirmed_payment_is_not_reopened(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "unpaid")
    txid = await _bank(conn)
    await confirm_payment(
        conn,
        bank_txn_id=txid,
        practice_id=pid,
        invoice_id=iid,
        amount_applied_idr=4_000_000,
        new_status="paid",
        confirmed_by=WHO,
        margin_idr=4_000_000,
        movement_date=date(2026, 6, 25),
    )
    await _expect_refusal(
        conn,
        reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO),
        f"bank transaction {txid} is not manual or ignored",
    )


# ── A5 all or nothing ────────────────────────────────────────────────────────────────────


async def test_a5_a_failure_after_the_bank_update_rolls_the_whole_action_back(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    linked = await _bank(conn)
    ignorable = await _bank(conn)
    reopenable = await _bank(conn, status="ignored")
    await conn.execute(
        "CREATE FUNCTION fail_log() RETURNS trigger LANGUAGE plpgsql AS"
        " $$ BEGIN RAISE EXCEPTION 'log refused'; END $$"
    )
    await conn.execute(
        "CREATE TRIGGER fail_log BEFORE INSERT ON reconciliation_log FOR EACH ROW EXECUTE FUNCTION fail_log()"
    )
    before = await _full(conn)
    for call in (
        link_recorded(conn, bank_txn_id=linked, practice_id=pid, invoice_id=iid, linked_by=WHO),
        ignore_transaction(conn, bank_txn_id=ignorable, reason="bukan klien", ignored_by=WHO),
        reopen_transaction(conn, bank_txn_id=reopenable, reopened_by=WHO),
    ):
        with pytest.raises(asyncpg.RaiseError):
            await call
        assert await _full(conn) == before


# ── A5 concurrency: two connections, a deterministic interleaving ────────────────────────


async def _waiting(watcher) -> int:
    return await watcher.fetchval(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
    )


async def _until_waiting(watcher, n: int, *, patience: float = 3.0) -> bool:
    for _ in range(int(patience / 0.02)):
        if await _waiting(watcher) >= n:
            return True
        await asyncio.sleep(0.02)
    return False


class _Conns:
    """The test connection, a lock-holding connection and two actors, on one database."""

    def __init__(self, conn, dbname):
        self.conn, self.dbname = conn, dbname

    async def __aenter__(self):
        self.holder = await _connect(self.dbname)
        self.a = await _connect(self.dbname)
        self.b = await _connect(self.dbname)
        return self

    async def __aexit__(self, *exc):
        await asyncio.gather(self.holder.close(), self.a.close(), self.b.close())


async def _race(c: _Conns, park_sql: str, make_a, make_b, *, b_waits_on_bank: bool = True):
    """A takes the bank row lock, then parks on a lock held by `holder`; B then arrives.

    With the lock taken first B must wait on the bank row; B is released only after A commits.
    Returns (result of A, result of B) as values or exceptions.
    """
    tx = c.holder.transaction()
    await tx.start()
    await c.holder.execute(park_sql)
    task_a = asyncio.create_task(make_a(c.a))
    assert await _until_waiting(c.holder, 1), "A never reached its parking lock"
    task_b = asyncio.create_task(make_b(c.b))
    if b_waits_on_bank:
        await _until_waiting(
            c.holder, 2, patience=1.5
        )  # B is on the bank row (without the lock it just runs)
    else:
        await asyncio.sleep(0.5)
    await tx.rollback()
    return await asyncio.gather(task_a, task_b, return_exceptions=True)


async def test_a5_link_vs_link_on_one_bank_transaction_succeeds_exactly_once(db) -> None:
    conn, dbname = db
    p1, i1 = await _practice(conn, "paid")
    p2, i2 = await _practice(conn, "paid")
    txid = await _bank(conn)
    async with _Conns(conn, dbname) as c:
        a, b = await _race(
            c,
            _LOG_PARK,
            lambda x: link_recorded(
                x, bank_txn_id=txid, practice_id=p1, invoice_id=i1, linked_by=WHO
            ),
            lambda x: link_recorded(
                x, bank_txn_id=txid, practice_id=p2, invoice_id=i2, linked_by=WHO
            ),
        )
    assert not isinstance(a, BaseException), a
    assert isinstance(b, ReconcileError) and str(b) == f"bank transaction {txid} is not unmatched"
    tx = await conn.fetchrow("SELECT * FROM bank_transactions WHERE id = $1", txid)
    assert (tx["reconciled_status"], tx["matched_practice_id"], tx["matched_invoice_id"]) == (
        "manual",
        p1,
        i1,
    )
    assert await conn.fetchval("SELECT count(*) FROM reconciliation_log") == 1


async def test_a5_link_then_confirm_the_second_sees_the_first_committed_state(db) -> None:
    conn, dbname = db
    p1, i1 = await _practice(conn, "paid")
    p2, i2 = await _practice(conn, "unpaid")
    txid = await _bank(conn)
    async with _Conns(conn, dbname) as c:
        a, b = await _race(
            c,
            _LOG_PARK,
            lambda x: link_recorded(
                x, bank_txn_id=txid, practice_id=p1, invoice_id=i1, linked_by=WHO
            ),
            lambda x: confirm_payment(
                x,
                bank_txn_id=txid,
                practice_id=p2,
                invoice_id=i2,
                amount_applied_idr=4_000_000,
                new_status="paid",
                confirmed_by=WHO,
                margin_idr=4_000_000,
                movement_date=date(2026, 6, 25),
            ),
        )
    assert not isinstance(a, BaseException), a
    assert isinstance(b, ReconcileError) and str(b) == f"bank transaction {txid} is not unmatched"
    # the confirm wrote nothing: the one cash-out row is the link's (owner rule e)
    assert [
        tuple(r)
        for r in await conn.fetch(
            "SELECT linked_practice_id, linked_bank_txn_id FROM weekly_cashout"
        )
    ] == [(p1, txid)]
    assert await conn.fetchval("SELECT payment_status FROM practices WHERE id = $1", p2) == "unpaid"
    assert (
        await conn.fetchval("SELECT reconciled_status FROM bank_transactions WHERE id = $1", txid)
        == "manual"
    )


async def test_a5_confirm_then_link_the_second_sees_the_first_committed_state(db) -> None:
    conn, dbname = db
    p1, i1 = await _practice(conn, "unpaid")
    p2, i2 = await _practice(conn, "paid")
    txid = await _bank(conn)
    async with _Conns(conn, dbname) as c:
        a, b = await _race(
            c,
            f"SELECT 1 FROM practices WHERE id = {p1} FOR UPDATE",
            lambda x: confirm_payment(
                x,
                bank_txn_id=txid,
                practice_id=p1,
                invoice_id=i1,
                amount_applied_idr=4_000_000,
                new_status="paid",
                confirmed_by=WHO,
                margin_idr=4_000_000,
                movement_date=date(2026, 6, 25),
            ),
            lambda x: link_recorded(
                x, bank_txn_id=txid, practice_id=p2, invoice_id=i2, linked_by=WHO
            ),
        )
    assert not isinstance(a, BaseException), a
    assert isinstance(b, ReconcileError) and str(b) == f"bank transaction {txid} is not unmatched"
    assert await conn.fetchval("SELECT count(*) FROM weekly_cashout") == 1
    tx = await conn.fetchrow("SELECT * FROM bank_transactions WHERE id = $1", txid)
    assert (tx["reconciled_status"], tx["matched_practice_id"]) == ("matched", p1)
    assert await conn.fetchval("SELECT count(*) FROM reconciliation_log") == 1


async def test_a5_reopen_then_ignore_the_second_sees_the_first_committed_state(db) -> None:
    conn, dbname = db
    txid = await _bank(conn, status="ignored")
    async with _Conns(conn, dbname) as c:
        a, b = await _race(
            c,
            _LOG_PARK,
            lambda x: reopen_transaction(x, bank_txn_id=txid, reopened_by=WHO),
            lambda x: ignore_transaction(x, bank_txn_id=txid, reason="bukan klien", ignored_by=WHO),
        )
    assert not isinstance(a, BaseException), a
    assert not isinstance(b, BaseException), b  # B saw 'unmatched', committed by A
    assert (
        await conn.fetchval("SELECT reconciled_status FROM bank_transactions WHERE id = $1", txid)
        == "ignored"
    )
    assert await conn.fetchval("SELECT count(*) FROM reconciliation_log") == 2


async def test_a5_ignore_then_reopen_the_second_sees_the_first_committed_state(db) -> None:
    conn, dbname = db
    txid = await _bank(conn)
    async with _Conns(conn, dbname) as c:
        a, b = await _race(
            c,
            _LOG_PARK,
            lambda x: ignore_transaction(x, bank_txn_id=txid, reason="bukan klien", ignored_by=WHO),
            lambda x: reopen_transaction(x, bank_txn_id=txid, reopened_by=WHO),
        )
    assert not isinstance(a, BaseException), a
    assert not isinstance(b, BaseException), b  # B saw 'ignored', committed by A
    assert (
        await conn.fetchval("SELECT reconciled_status FROM bank_transactions WHERE id = $1", txid)
        == "unmatched"
    )
    rows = await conn.fetch("SELECT id, reversal_of_id FROM reconciliation_log ORDER BY id")
    assert len(rows) == 2 and rows[1]["reversal_of_id"] == rows[0]["id"]


async def test_a5_ignore_vs_ignore_and_reopen_vs_reopen_succeed_exactly_once(db) -> None:
    conn, dbname = db
    t1 = await _bank(conn)
    t2 = await _bank(conn, status="ignored")
    async with _Conns(conn, dbname) as c:
        a, b = await _race(
            c,
            _LOG_PARK,
            lambda x: ignore_transaction(x, bank_txn_id=t1, reason="bukan klien", ignored_by=WHO),
            lambda x: ignore_transaction(x, bank_txn_id=t1, reason="bukan klien", ignored_by=WHO),
        )
        assert not isinstance(a, BaseException) and isinstance(b, ReconcileError), (a, b)
        a, b = await _race(
            c,
            _LOG_PARK,
            lambda x: reopen_transaction(x, bank_txn_id=t2, reopened_by=WHO),
            lambda x: reopen_transaction(x, bank_txn_id=t2, reopened_by=WHO),
        )
        assert not isinstance(a, BaseException) and isinstance(b, ReconcileError), (a, b)
    assert await conn.fetchval("SELECT count(*) FROM reconciliation_log") == 2


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("  ok then  ", "ok then"),
        (" " * 4 + "x" * 200 + " " * 4, "x" * 200),
        ("\tabc\n", "abc"),
        ("bunga   bank", "bunga bank"),
        ("a\nb c", "a b c"),
        ("a\u00a0\u00a0b\t\tc\r\nd", "a b c d"),
        ("x" * 100 + "   " + "y" * 99, "x" * 100 + " " + "y" * 99),  # 200 once collapsed
    ],
)
async def test_a2_the_reason_is_trimmed_and_the_trimmed_text_is_stored(db, given, stored) -> None:
    conn, _ = db
    txid = await _bank(conn)
    res = await ignore_transaction(conn, bank_txn_id=txid, reason=given, ignored_by=WHO)
    note = await conn.fetchval(
        "SELECT note FROM reconciliation_log WHERE id = $1", res.reconciliation_log_id
    )
    assert note == stored


@pytest.mark.parametrize("given", ["   ", "  ab  ", " " + "x" * 201 + " ", ""])
async def test_a2_a_reason_too_short_or_too_long_once_trimmed_is_refused_and_changes_nothing(
    db, given
) -> None:
    conn, _ = db
    txid = await _bank(conn)
    await _expect_refusal(
        conn,
        ignore_transaction(conn, bank_txn_id=txid, reason=given, ignored_by=WHO),
        REASON_REFUSAL,
    )


async def test_a5_two_links_of_different_transfers_to_one_invoice_exactly_one_succeeds(db) -> None:
    """Both transfers fit the invoice alone, together they exceed the ceiling. A reads the
    attached sum, then parks before its log row; B arrives. The invoice lock makes B wait for
    A's commit and see the attachment; without it B reads an empty sum and both would link."""
    conn, dbname = db
    pid, iid = await _practice(conn, "paid")
    t1 = await _bank(conn)
    t2 = await _bank(conn)
    async with _Conns(conn, dbname) as c:
        a, b = await _race(
            c,
            _LOG_PARK,
            lambda x: _link(x, t1, pid, iid),
            lambda x: _link(x, t2, pid, iid),
        )
    assert not isinstance(a, BaseException), a
    assert isinstance(b, ReconcileError)
    assert str(b) == f"bank transaction {t2} would exceed the amount of invoice {iid}"
    rows = await conn.fetch("SELECT id, reconciled_status FROM bank_transactions ORDER BY id")
    assert [(r["id"], r["reconciled_status"]) for r in rows] == [(t1, "manual"), (t2, "unmatched")]
    assert await conn.fetchval("SELECT count(*) FROM reconciliation_log") == 1


async def test_a5_two_links_to_one_invoice_that_fit_together_both_succeed(db) -> None:
    conn, dbname = db
    pid, iid = await _practice(conn, "paid")
    t1 = await _bank(conn, amount=1_500_000)
    t2 = await _bank(conn, amount=2_500_000)
    async with _Conns(conn, dbname) as c:
        a, b = await _race(
            c, _LOG_PARK, lambda x: _link(x, t1, pid, iid), lambda x: _link(x, t2, pid, iid)
        )
    assert not isinstance(a, BaseException) and not isinstance(b, BaseException), (a, b)
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM bank_transactions WHERE reconciled_status = 'manual'"
        )
        == 2
    )


async def test_a2_ignore_clears_stale_matched_ids_on_an_unmatched_row(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank(conn, status="unmatched", practice=pid, invoice=iid)  # doctored
    await ignore_transaction(conn, bank_txn_id=txid, reason="bukan klien", ignored_by=WHO)
    tx = await conn.fetchrow("SELECT * FROM bank_transactions WHERE id = $1", txid)
    assert (tx["reconciled_status"], tx["matched_practice_id"], tx["matched_invoice_id"]) == (
        "ignored",
        None,
        None,
    )


async def test_a3_reopen_a_manual_row_with_no_log_row_has_a_null_reversal(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank(conn, status="manual", practice=pid, invoice=iid)
    res = await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)
    log = await conn.fetchrow(
        "SELECT * FROM reconciliation_log WHERE id = $1", res.reconciliation_log_id
    )
    assert log["reversal_of_id"] is None
    tx = await conn.fetchrow("SELECT * FROM bank_transactions WHERE id = $1", txid)
    assert (tx["reconciled_status"], tx["matched_practice_id"], tx["matched_invoice_id"]) == (
        "unmatched",
        None,
        None,
    )


SENTINEL = "alasan-rahasia-contoh"


@pytest.mark.parametrize(
    "given",
    [
        "   ",
        "ab",
        "x" * 201,
        "abc\u0000def",
        "ab\ud800cd",
        "\u0007bell",
        "x" * 100 + "   " + "y" * 101,
        SENTINEL + "\u0000",
    ],
)
async def test_a2_an_unfit_reason_is_refused_with_one_fixed_text_and_never_logged(
    db, caplog, given
) -> None:
    conn, _ = db
    txid = await _bank(conn)
    before = await _full(conn)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ReconcileError) as err:
            await ignore_transaction(conn, bank_txn_id=txid, reason=given, ignored_by=WHO)
    assert str(err.value) == REASON_REFUSAL == "reason must be 3 to 200 printable characters"
    assert await _full(conn) == before
    assert SENTINEL not in caplog.text


async def test_a2_a_successful_ignore_never_logs_the_reason(db, caplog) -> None:
    conn, _ = db
    txid = await _bank(conn)
    with caplog.at_level(logging.DEBUG):
        await ignore_transaction(conn, bank_txn_id=txid, reason=SENTINEL, ignored_by=WHO)
    assert caplog.records, "the action is expected to log its ids"
    assert SENTINEL not in caplog.text and all(
        SENTINEL not in r.getMessage() for r in caplog.records
    )
    assert await conn.fetchval("SELECT note FROM reconciliation_log") == SENTINEL


async def test_the_open_pool_includes_a_practice_with_a_null_payment_status(db) -> None:
    """The real SQL of the two pools: NULL status is open, `paid` is recorded, nothing is in both."""
    from backend.app.routers.accounting import _fetch_invoice_pool

    conn, _ = db
    _pn, i_null = await _practice(conn, None)
    _pu, i_unpaid = await _practice(conn, "unpaid")
    _pp, i_partial = await _practice(conn, "partial")
    _pd, i_paid = await _practice(conn, "paid")
    open_ids = {i.invoice_id for i in await _fetch_invoice_pool(conn, recorded=False)}
    recorded_ids = {i.invoice_id for i in await _fetch_invoice_pool(conn, recorded=True)}
    assert open_ids == {i_null, i_unpaid, i_partial}
    assert recorded_ids == {i_paid}


async def test_a_non_finite_invoice_amount_becomes_zero_in_the_pool(db) -> None:
    from backend.app.routers.accounting import _fetch_invoice_pool

    conn, _ = db
    await conn.execute("ALTER TABLE invoices ALTER COLUMN amount_idr TYPE NUMERIC(15,2)")
    await conn.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS pph_expected_idr BIGINT")
    ids = []
    for literal in ("NaN", "1234.00"):
        _p, i = await _practice(conn, "unpaid")
        await conn.execute("UPDATE invoices SET amount_idr = $2::numeric WHERE id = $1", i, literal)
        ids.append(i)
    got = {i.invoice_id: i.amount_idr for i in await _fetch_invoice_pool(conn, recorded=False)}
    assert got == {ids[0]: 0, ids[1]: 1234}


# ── A1 ATTACHED: a payment confirmed without naming its invoice counts too ────────────────


@pytest.mark.parametrize("status", ["matched", "manual"])
async def test_a1_attached_by_practice_when_the_matched_invoice_is_null(db, status) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    await _bank(conn, status=status, practice=pid, invoice=None, amount=1_500_000)
    exact = await _bank(conn, amount=2_501_000)  # 1,500,000 + 2,501,000 = 4,001,000
    over = await _bank(conn, amount=2_501_001)
    await _expect_refusal(
        conn,
        _link(conn, over, pid, iid),
        f"bank transaction {over} would exceed the amount of invoice {iid}",
    )
    assert (await _link(conn, exact, pid, iid)).reconciled_status == "manual"


async def test_a1_a_confirm_without_an_invoice_counts_toward_the_ceiling(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "unpaid")
    first = await _bank(conn)
    await confirm_payment(
        conn,
        bank_txn_id=first,
        practice_id=pid,
        invoice_id=None,
        amount_applied_idr=4_000_000,
        new_status="paid",
        confirmed_by=WHO,
        margin_idr=4_000_000,
        movement_date=date(2026, 6, 25),
    )
    other = await _bank(conn)
    await _expect_refusal(
        conn,
        _link(conn, other, pid, iid),
        f"bank transaction {other} would exceed the amount of invoice {iid}",
    )


async def test_a1_rows_that_are_not_attached_to_this_invoice_do_not_count(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    other_p, other_i = await _practice(conn, "paid")
    await _bank(
        conn, status="manual", practice=other_p, invoice=None
    )  # another practice, no invoice
    await _bank(conn, status="matched", practice=pid, invoice=other_i)  # another invoice
    await _bank(conn, status="ignored", practice=pid, invoice=None)  # not attached by status
    await _bank(conn, status="unmatched", practice=pid, invoice=None)
    assert (await _link(conn, await _bank(conn), pid, iid)).reconciled_status == "manual"


async def test_a1_the_ceiling_compares_with_the_exact_amount_cents_included(db) -> None:
    conn, _ = db
    await conn.execute("ALTER TABLE invoices ALTER COLUMN amount_idr TYPE NUMERIC(15,2)")
    pid, iid = await _practice(conn, "paid")
    await conn.execute("UPDATE invoices SET amount_idr = 4000000.99 WHERE id = $1", iid)
    ok = await _bank(conn, amount=4_001_000)
    over = await _bank(conn, amount=4_001_001)
    await _expect_refusal(
        conn,
        _link(conn, over, pid, iid),
        f"bank transaction {over} would exceed the amount of invoice {iid}",
    )
    assert (await _link(conn, ok, pid, iid)).reconciled_status == "manual"


async def test_the_pool_drops_the_cents_of_an_invoice_amount_by_truncation(db) -> None:
    """The lights see 4000000.99 as 4000000 (truncated, not rounded to 4000001)."""
    from backend.app.routers.accounting import _fetch_invoice_pool

    conn, _ = db
    await conn.execute("ALTER TABLE invoices ALTER COLUMN amount_idr TYPE NUMERIC(15,2)")
    await conn.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS pph_expected_idr BIGINT")
    _p, iid = await _practice(conn, "unpaid")
    await conn.execute("UPDATE invoices SET amount_idr = 4000000.99 WHERE id = $1", iid)
    pool = await _fetch_invoice_pool(conn, recorded=False)
    assert [(i.invoice_id, i.amount_idr) for i in pool] == [(iid, 4_000_000)]


# ── the deadlock between link and confirm on one practice and one invoice ─────────────────


async def _confirm(conn, tx, p, i):
    return await confirm_payment(
        conn,
        bank_txn_id=tx,
        practice_id=p,
        invoice_id=i,
        amount_applied_idr=4_000_000,
        new_status="paid",
        confirmed_by=WHO,
        margin_idr=4_000_000,
        movement_date=date(2026, 6, 25),
    )


async def _park_trigger(conn, table: str, key: int, when: str = "") -> None:
    """A row trigger that takes advisory lock `key` for the transaction: whoever fires it waits
    while the test holds that lock, i.e. is parked at a chosen point of its transaction."""
    await conn.execute(
        f"CREATE FUNCTION park_{key}() RETURNS trigger LANGUAGE plpgsql AS"
        f" $$ BEGIN PERFORM pg_advisory_xact_lock({key}); RETURN NEW; END $$"
    )
    await conn.execute(
        f"CREATE TRIGGER park_{key} BEFORE UPDATE ON {table} FOR EACH ROW {when}"
        f" EXECUTE FUNCTION park_{key}()"
    )


async def _crossing(c: _Conns, key: int, first, second):
    """`first` runs until it is parked on advisory lock `key`; `second` is then started; the
    lock is released and both finish. Returns both outcomes (values or exceptions)."""
    await c.holder.execute("SELECT pg_advisory_lock($1)", key)
    task_a = asyncio.create_task(first(c.a))
    assert await _until_waiting(c.holder, 1), "the first action never reached its parking point"
    task_b = asyncio.create_task(second(c.b))
    await _until_waiting(c.holder, 2, patience=1.5)
    await c.holder.execute("SELECT pg_advisory_unlock($1)", key)
    return await asyncio.gather(task_a, task_b, return_exceptions=True)


async def test_a5_a_confirm_holding_the_practice_and_a_link_on_the_same_invoice_do_not_deadlock(
    db,
) -> None:
    """Confirm arrives first and is parked after it took the practice lock (its UPDATE of the
    practice fires the trigger); the link arrives. With the old order (invoice lock first) the
    link took the invoice, then needed the practice: a deadlock. Now the link queues."""
    conn, dbname = db
    pid, iid = await _practice(conn, "paid")
    t_confirm = await _bank(conn)
    t_link = await _bank(conn)
    await _park_trigger(conn, "practices", 7)
    async with _Conns(conn, dbname) as c:
        a, b = await _crossing(
            c, 7, lambda x: _confirm(x, t_confirm, pid, iid), lambda x: _link(x, t_link, pid, iid)
        )
    assert not isinstance(a, asyncpg.DeadlockDetectedError) and not isinstance(
        b, asyncpg.DeadlockDetectedError
    ), (a, b)
    assert not isinstance(a, BaseException), a
    # the link queued behind the confirm and then saw its payment attached: refused by the ceiling
    assert isinstance(b, ReconcileError)
    assert str(b) == f"bank transaction {t_link} would exceed the amount of invoice {iid}"
    rows = await conn.fetch("SELECT id, reconciled_status FROM bank_transactions ORDER BY id")
    assert [(r["id"], r["reconciled_status"]) for r in rows] == [
        (t_confirm, "matched"),
        (t_link, "unmatched"),
    ]
    assert await conn.fetchval("SELECT count(*) FROM weekly_cashout") == 1
    assert await conn.fetchval("SELECT count(*) FROM reconciliation_log") == 1


async def test_a5_a_link_holding_the_practice_and_a_confirm_on_the_same_invoice_do_not_deadlock(
    db,
) -> None:
    """The link arrives first and is parked (its UPDATE of the bank row fires the trigger);
    then the confirm arrives. Both complete, in that order, with consistent rows."""
    conn, dbname = db
    pid, iid = await _practice(conn, "paid")
    t_link = await _bank(conn)
    t_confirm = await _bank(conn)
    await _park_trigger(conn, "bank_transactions", 8, "WHEN (NEW.reconciled_status = 'manual')")
    async with _Conns(conn, dbname) as c:
        a, b = await _crossing(
            c, 8, lambda x: _link(x, t_link, pid, iid), lambda x: _confirm(x, t_confirm, pid, iid)
        )
    assert not isinstance(a, BaseException), a
    assert not isinstance(b, BaseException), b
    rows = await conn.fetch("SELECT id, reconciled_status FROM bank_transactions ORDER BY id")
    assert [(r["id"], r["reconciled_status"]) for r in rows] == [
        (t_link, "manual"),
        (t_confirm, "matched"),
    ]
    assert await conn.fetchval("SELECT count(*) FROM weekly_cashout") == 2  # link + confirm
    assert await conn.fetchval("SELECT count(*) FROM reconciliation_log") == 2
    assert await conn.fetchval("SELECT paid_amount FROM practices WHERE id = $1", pid) == 4_000_000


PAIRS = 200


async def test_a5_a_randomised_stress_of_link_and_confirm_pairs_on_one_practice_never_deadlocks(
    db,
) -> None:
    conn, dbname = db
    pid, iid = await _practice(conn, "paid", amount=10**12)  # the ceiling never refuses here
    rng = random.Random(1118)
    jitters = [
        (rng.random() * 0.004, rng.random() * 0.004, rng.random() < 0.5) for _ in range(PAIRS)
    ]
    txs = [(await _bank(conn), await _bank(conn)) for _ in range(PAIRS)]
    deadlocks = 0
    other_errors = []
    async with _Conns(conn, dbname) as c:

        async def delayed(delay, call):
            await asyncio.sleep(delay)
            return await call

        for (t_link, t_confirm), (d1, d2, link_first) in zip(txs, jitters, strict=True):
            if not link_first:
                d1, d2 = d2, d1
            got = await asyncio.gather(
                delayed(d1, _link(c.a, t_link, pid, iid)),
                delayed(d2, _confirm(c.b, t_confirm, pid, iid)),
                return_exceptions=True,
            )
            for g in got:
                if isinstance(g, asyncpg.DeadlockDetectedError):
                    deadlocks += 1
                elif isinstance(g, BaseException):
                    other_errors.append(repr(g))
    assert deadlocks == 0 and other_errors == []
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM bank_transactions WHERE reconciled_status = 'manual'"
        )
        == PAIRS
    )
    assert await conn.fetchval("SELECT count(*) FROM weekly_cashout") == 2 * PAIRS
    assert await conn.fetchval("SELECT count(*) FROM reconciliation_log") == 2 * PAIRS


# ── the invoice is read under a share lock: its amount cannot change under the ceiling ────


@pytest.mark.parametrize("x_commits", [True, False])
async def test_a1_a_link_waits_for_an_uncommitted_change_of_the_invoice_amount(
    db, x_commits
) -> None:
    conn, dbname = db
    pid, iid = await _practice(conn, "paid")  # invoice 4,000,000
    txid = await _bank(conn)  # a 4,000,000 transfer
    async with _Conns(conn, dbname) as c:
        x = c.holder.transaction()
        await x.start()
        await c.holder.execute("UPDATE invoices SET amount_idr = 1000000 WHERE id = $1", iid)
        task = asyncio.create_task(_link(c.a, txid, pid, iid))
        assert await _until_waiting(c.holder, 1), "the link did not wait for the invoice row"
        assert not task.done()
        if x_commits:
            await x.commit()
        else:
            await x.rollback()
        got = await asyncio.gather(task, return_exceptions=True)
    if x_commits:  # judged on the new, lower amount: 4,000,000 > 1,000,000 + 1,000
        assert isinstance(got[0], ReconcileError)
        assert str(got[0]) == f"bank transaction {txid} would exceed the amount of invoice {iid}"
        assert (
            await conn.fetchval(
                "SELECT reconciled_status FROM bank_transactions WHERE id = $1", txid
            )
            == "unmatched"
        )
    else:
        assert not isinstance(got[0], BaseException), got[0]
        assert got[0].reconciled_status == "manual"


# ── candidate identity: the pool query's LEFT JOIN to practice_types, on real Postgres ─────


async def _typed_practice(
    conn, status: str | None, *, type_name: str | None, number: str | None, issued=None
) -> tuple[int, int]:
    """One practice (optionally typed) with one invoice carrying the given number and date."""
    type_id = None
    if type_name is not None:
        type_id = await conn.fetchval(
            "INSERT INTO practice_types (code, name) VALUES ($1, $2) RETURNING id",
            f"code-{type_name.lower().replace(' ', '-')}-{random.randrange(10**9)}",
            type_name,
        )
    cid = await conn.fetchval(
        "INSERT INTO clients (full_name) VALUES ('Contoh Klien') RETURNING id"
    )
    pid = await conn.fetchval(
        "INSERT INTO practices (client_id, payment_status, practice_type_id)"
        " VALUES ($1, $2, $3) RETURNING id",
        cid,
        status,
        type_id,
    )
    iid = await conn.fetchval(
        "INSERT INTO invoices (practice_id, invoice_number, amount_idr, generated_at)"
        " VALUES ($1, $2, 4000000, $3) RETURNING id",
        pid,
        number,
        issued,
    )
    return pid, iid


@pytest.mark.parametrize(("status", "recorded"), [("unpaid", False), ("paid", True)])
async def test_identity_comes_back_from_the_database_in_both_pools(db, status, recorded) -> None:
    from datetime import datetime, timezone

    from backend.app.routers.accounting import _candidate_json, _fetch_invoice_pool
    from backend.services.accounting.matcher import rank_candidates

    conn, _ = db
    issued = datetime(2026, 1, 10, 15, 30, tzinfo=timezone.utc)
    _p, iid = await _typed_practice(
        conn, status, type_name="Example service", number="INV-000000-00001", issued=issued
    )
    (inv,) = await _fetch_invoice_pool(conn, recorded=recorded)
    assert inv.invoice_id == iid
    assert (inv.invoice_number, inv.invoice_issued_at, inv.service_name) == (
        "INV-000000-00001",
        "2026-01-10T15:30:00+00:00",
        "Example service",
    )
    (cand,) = rank_candidates(
        transfer_amount_idr=4_000_000,
        transfer_date=date(2026, 1, 12),
        payer_memo="",
        open_invoices=[inv],
    )
    out = _candidate_json(cand)
    assert (out["invoice_number"], out["invoice_issued_at"], out["service_name"]) == (
        "INV-000000-00001",
        "2026-01-10T15:30:00+00:00",
        "Example service",
    )


@pytest.mark.parametrize(("status", "recorded"), [("unpaid", False), ("paid", True)])
async def test_a_practice_without_a_type_stays_in_the_pool_with_a_null_service(
    db, status, recorded
) -> None:
    from backend.app.routers.accounting import _fetch_invoice_pool

    conn, _ = db
    _p, iid = await _typed_practice(conn, status, type_name=None, number="INV-000000-00002")
    (inv,) = await _fetch_invoice_pool(conn, recorded=recorded)
    assert (inv.invoice_id, inv.service_name) == (iid, None)


async def test_an_invoice_without_a_number_gives_none_and_without_a_date_gives_none(db) -> None:
    from backend.app.routers.accounting import _fetch_invoice_pool

    conn, _ = db
    await _typed_practice(conn, "unpaid", type_name="Example service", number=None)
    await conn.execute("UPDATE invoices SET generated_at = NULL")
    (inv,) = await _fetch_invoice_pool(conn, recorded=False)
    assert (inv.invoice_number, inv.invoice_issued_at) == (None, None)
    assert inv.service_name == "Example service"


async def test_the_join_neither_adds_nor_drops_a_pool_row(db) -> None:
    from backend.app.routers.accounting import _fetch_invoice_pool

    conn, _ = db
    # Typed (two practices share one type name), untyped and NULL-status, all open; two paid.
    for status, name in [
        ("unpaid", "Example service"),
        ("partial", "Example service"),
        (None, None),
        ("unpaid", None),
        ("paid", "Example service"),
        ("paid", None),
    ]:
        await _typed_practice(conn, status, type_name=name, number=None)
    open_rows = await _fetch_invoice_pool(conn, recorded=False)
    recorded_rows = await _fetch_invoice_pool(conn, recorded=True)
    want_open = await conn.fetchval(
        "SELECT count(*) FROM invoices i JOIN practices p ON i.practice_id = p.id"
        " WHERE COALESCE(p.payment_status, 'unpaid') <> 'paid'"
    )
    want_recorded = await conn.fetchval(
        "SELECT count(*) FROM invoices i JOIN practices p ON i.practice_id = p.id"
        " WHERE COALESCE(p.payment_status, 'unpaid') = 'paid'"
    )
    assert (len(open_rows), len(recorded_rows)) == (want_open, want_recorded) == (4, 2)
    assert len({r.invoice_id for r in open_rows + recorded_rows}) == 6


async def test_recorded_payment_uses_only_the_company_selected_for_that_case(db) -> None:
    from backend.app.routers.accounting import _fetch_invoice_pool

    conn, _ = db
    pid, iid = await _typed_practice(conn, "paid", type_name=None, number=None)
    await conn.execute("INSERT INTO companies (company_name) VALUES ('Unbilled Example')")
    company = await conn.fetchval(
        "INSERT INTO companies (company_name) VALUES ('PT Example Workshop') RETURNING id"
    )
    await conn.execute("UPDATE practices SET invoice_company_id=$2 WHERE id=$1", pid, company)
    (invoice,) = await _fetch_invoice_pool(conn, recorded=True)
    assert (invoice.invoice_id, invoice.bill_to_company) == (iid, "PT Example Workshop")
