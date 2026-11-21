"""Real-PostgreSQL tests of owner rule (b): a confirmation can be undone the same day (WITA).

Everything a confirmation wrote goes back in one transaction (the transfer, its cash-out row,
the invoice, the case, the start of a case it started), a log row marks the undo, and anything
not undoable is refused with nothing written. All names and amounts are invented.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.services.accounting.reconcile_service import (
    START_TIMELINE_EVENT,
    UNDO_NOTE,
    ReconcileError,
    confirm_payment,
    ignore_transaction,
    link_recorded,
    recorded_without_notifications,
    reopen_transaction,
    undo_confirmation,
)
from backend.tests.services.test_accounting_confirm_starts_case_integration import (
    DAY,
    WHO,
    _case,
    _confirm,
)
from backend.tests.services.test_accounting_reconcile_integration import _drop_db, _fresh_db

UNDOER = "second.accountant@example.test"


@pytest.fixture
async def db():
    conn, dbname, user = await _fresh_db()
    try:
        yield conn
    finally:
        await _drop_db(conn, dbname, user)


def _undo(conn, txid):
    return undo_confirmation(conn, bank_txn_id=txid, undone_by=UNDOER)


async def _snapshot(conn) -> dict[str, list[dict]]:
    out = {}
    for table in (
        "bank_transactions",
        "practices",
        "invoices",
        "weekly_cashout",
        "reconciliation_log",
        "timeline_events",
        "activity_log",
    ):
        out[table] = [dict(r) for r in await conn.fetch(f"SELECT * FROM {table} ORDER BY id")]
    return out


async def _standing_cashout(conn) -> int:
    return int(
        await conn.fetchval(
            "SELECT COALESCE(sum(amount_idr), 0) FROM weekly_cashout WHERE reversed_by_id IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM weekly_cashout o WHERE o.reversed_by_id = weekly_cashout.id)"
        )
    )


async def _another_transfer(conn, amount: int, day: date) -> int:
    sid = await conn.fetchval("SELECT id FROM bank_statements ORDER BY id LIMIT 1")
    return await conn.fetchval(
        "INSERT INTO bank_transactions (statement_id, txn_date, description, amount_idr, direction)"
        " VALUES ($1, $2, 'TRF Contoh Klien', $3, 'credit') RETURNING id",
        sid,
        day,
        amount,
    )


async def test_a_partial_confirmation_undone_puts_every_row_back(db) -> None:
    pid, iid, txid = await _case(db, "on_process")
    before = await _snapshot(db)
    confirm = await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)

    result = await _undo(db, txid)

    assert (result.bank_txn_id, result.reconciled_status) == (txid, "unmatched")
    bank = await db.fetchrow("SELECT * FROM bank_transactions WHERE id = $1", txid)
    assert (bank["reconciled_status"], bank["matched_practice_id"], bank["matched_invoice_id"]) == (
        "unmatched",
        None,
        None,
    )
    prac = await db.fetchrow("SELECT * FROM practices WHERE id = $1", pid)
    assert (prac["payment_status"], prac["paid_amount"], prac["status"]) == (
        "unpaid",
        0,
        "on_process",
    )
    inv = await db.fetchrow("SELECT paid_amount_idr, paid_date FROM invoices WHERE id = $1", iid)
    assert (inv["paid_amount_idr"], inv["paid_date"]) == (0, None)
    rows = await db.fetch("SELECT * FROM weekly_cashout ORDER BY id")
    assert len(rows) == 2
    original, reversal = rows
    assert original["id"] == confirm.cashout_id and original["reversed_by_id"] == reversal["id"]
    assert (
        reversal["description"] == f"reversal of cash-out row {original['id']}: confirmation undone"
    )
    assert reversal["recorded_by"] == UNDOER and reversal["amount_idr"] == 1_000_000
    assert await _standing_cashout(db) == 0
    log = await db.fetchrow(
        "SELECT * FROM reconciliation_log WHERE id = $1", result.reconciliation_log_id
    )
    assert (
        log["bank_txn_id"],
        log["practice_id"],
        log["reversal_of_id"],
        log["note"],
        log["status_before"],
        log["status_after"],
        log["amount_applied_idr"],
        log["cashout_id"],
        log["confirmed_by"],
        list(log["invoice_ids"]),
    ) == (txid, pid, confirm.reconciliation_log_id, UNDO_NOTE, "partial", "unpaid", 1_000_000,
          reversal["id"], UNDOER, [iid])  # fmt: skip
    after = await _snapshot(db)
    assert after["timeline_events"] == before["timeline_events"] == []
    assert after["activity_log"] == before["activity_log"] == []


async def test_a_confirmation_that_started_the_case_undone_puts_the_case_back(db) -> None:
    pid, iid, txid = await _case(db, "sending_invoice", summary="Visa contoh")
    confirm = await _confirm(db, pid, iid, txid)
    assert (await db.fetchval("SELECT status FROM practices WHERE id = $1", pid)) == "on_process"

    await _undo(db, txid)

    prac = await db.fetchrow("SELECT * FROM practices WHERE id = $1", pid)
    assert (prac["status"], prac["start_date"], prac["payment_status"], prac["paid_amount"]) == (
        "sending_invoice",
        None,
        "unpaid",
        0,
    )
    timeline = await db.fetch("SELECT * FROM timeline_events ORDER BY id")
    assert [(t["event_type"], t["title"], t["client_visible"]) for t in timeline] == [
        (START_TIMELINE_EVENT[0], START_TIMELINE_EVENT[1], False)
    ]
    activity = await db.fetch("SELECT * FROM activity_log ORDER BY id")
    assert [
        (a["entity_id"], a["description"], a["changes"], a["performed_by"]) for a in activity
    ] == [
        (pid, "Updated: status", {"status": "on_process"}, WHO),
        (pid, "Updated: status", {"status": "sending_invoice"}, UNDOER),
    ]
    assert confirm.status_before == "unpaid"


async def test_a_start_date_set_before_the_confirmation_is_kept(db) -> None:
    earlier = date(2026, 6, 1)
    pid, iid, txid = await _case(db, "sending_invoice", start=earlier)
    await _confirm(db, pid, iid, txid)
    await _undo(db, txid)
    prac = await db.fetchrow("SELECT status, start_date FROM practices WHERE id = $1", pid)
    assert (prac["status"], prac["start_date"]) == ("sending_invoice", earlier)


async def test_a_start_date_set_earlier_the_same_day_is_kept(db) -> None:
    """A case opened today with its start date already set: the confirmation that starts it
    keeps that date (COALESCE), so the undo must keep it too."""
    today = await db.fetchval("SELECT now()::date")
    pid, iid, txid = await _case(db, "sending_invoice", start=today)
    await _confirm(db, pid, iid, txid)
    await _undo(db, txid)
    prac = await db.fetchrow("SELECT status, start_date FROM practices WHERE id = $1", pid)
    assert (prac["status"], prac["start_date"]) == ("sending_invoice", today)


async def test_a_start_date_changed_by_hand_after_the_start_is_kept(db) -> None:
    pid, iid, txid = await _case(db, "sending_invoice")
    await _confirm(db, pid, iid, txid)
    await db.execute("UPDATE practices SET start_date = '2026-06-30' WHERE id = $1", pid)
    await _undo(db, txid)
    prac = await db.fetchrow("SELECT status, start_date FROM practices WHERE id = $1", pid)
    assert (prac["status"], prac["start_date"]) == ("sending_invoice", date(2026, 6, 30))


async def test_the_invoice_gets_back_a_paid_date_no_cash_out_row_explains(db) -> None:
    pid, iid, txid = await _case(db, "on_process")
    may = datetime(2026, 5, 1, tzinfo=timezone.utc)
    await db.execute(
        "UPDATE invoices SET paid_date = $2, paid_amount_idr = 1000000 WHERE id = $1", iid, may
    )
    await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    assert (await db.fetchval("SELECT paid_date FROM invoices WHERE id = $1", iid)) > may
    await _undo(db, txid)
    inv = await db.fetchrow("SELECT paid_date, paid_amount_idr FROM invoices WHERE id = $1", iid)
    assert (inv["paid_date"], inv["paid_amount_idr"]) == (may, 1_000_000)


async def test_the_confirmation_records_what_it_overwrites(db) -> None:
    may = datetime(2026, 5, 1, 7, 30, 15, 123456, tzinfo=timezone.utc)
    pid, iid, txid = await _case(db, "on_process", start=date(2026, 6, 1))
    await db.execute("UPDATE invoices SET paid_date = $2 WHERE id = $1", iid, may)
    confirm = await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    state = await db.fetchval(
        "SELECT before_state FROM reconciliation_log WHERE id = $1", confirm.reconciliation_log_id
    )
    assert state == {"invoice_paid_date": may.isoformat(), "start_date": "2026-06-01"}


async def test_an_undo_that_gives_a_case_back_its_paid_is_recorded_without_notifications(
    db,
) -> None:
    """A case already `paid` (by hand) confirmed as `partial`: the undo moves the payment from
    `partial` back to `paid`, which the listener would read as a payment arriving. The undo's
    log row carries the change's instant and transition, so the listener sends nothing."""
    pid, iid, txid = await _case(db, "on_process")
    await db.execute(
        "UPDATE practices SET payment_status = 'paid', paid_amount = 4000000 WHERE id = $1", pid
    )
    confirm = await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    result = await _undo(db, txid)
    assert (await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", pid)) == "paid"

    def stamp(log_id):
        return db.fetchval(
            "SELECT extract(epoch FROM confirmed_at)::text FROM reconciliation_log WHERE id = $1",
            log_id,
        )

    undo_ts = await stamp(result.reconciliation_log_id)
    assert await recorded_without_notifications(db, pid, undo_ts, "partial", "paid") is True
    # the instant names the undo, the transition must match it too
    assert await recorded_without_notifications(db, pid, undo_ts, "unpaid", "paid") is False
    # the live confirmation itself stays an ordinary change
    confirm_ts = await stamp(confirm.reconciliation_log_id)
    assert await recorded_without_notifications(db, pid, confirm_ts, "paid", "partial") is False


async def test_an_undo_inside_another_transaction_is_refused(db) -> None:
    pid, iid, txid = await _case(db, "on_process")
    await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    before = await _snapshot(db)
    with pytest.raises(ReconcileError, match="transaction of its own"):
        async with db.transaction():
            await _undo(db, txid)
    assert await _snapshot(db) == before


async def test_a_historical_confirmation_can_be_undone(db) -> None:
    pid, iid, txid = await _case(db, "on_process")
    await _confirm(db, pid, iid, txid, historical=True)
    await _undo(db, txid)
    assert (
        await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", pid)
    ) == "unpaid"
    assert (
        await db.fetchval("SELECT reconciled_status FROM bank_transactions WHERE id = $1", txid)
    ) == ("unmatched")


async def test_the_invoice_keeps_the_paid_date_of_its_other_standing_payment(db) -> None:
    pid, iid, first = await _case(db, "on_process")
    await db.execute("UPDATE bank_transactions SET amount_idr = 2000000 WHERE id = $1", first)
    second = await _another_transfer(db, 2_000_000, date(2026, 6, 28))
    await confirm_payment(
        db, bank_txn_id=first, practice_id=pid, invoice_id=iid, amount_applied_idr=2_000_000,
        new_status="partial", confirmed_by=WHO, movement_date=date(2026, 6, 25),
    )  # fmt: skip
    await confirm_payment(
        db, bank_txn_id=second, practice_id=pid, invoice_id=iid, amount_applied_idr=2_000_000,
        new_status="paid", confirmed_by=WHO, movement_date=date(2026, 6, 28),
    )  # fmt: skip

    with pytest.raises(ReconcileError, match="has changed since this confirmation"):
        await _undo(db, first)  # the newer one stands on the same case

    await _undo(db, second)
    inv = await db.fetchrow("SELECT paid_amount_idr, paid_date FROM invoices WHERE id = $1", iid)
    assert inv["paid_amount_idr"] == 2_000_000
    assert inv["paid_date"].date() == date(2026, 6, 25)
    prac = await db.fetchrow("SELECT payment_status, paid_amount FROM practices WHERE id = $1", pid)
    assert (prac["payment_status"], prac["paid_amount"]) == ("partial", 2_000_000)
    assert await _standing_cashout(db) == 2_000_000

    await _undo(db, first)  # now the newest standing one
    inv = await db.fetchrow("SELECT paid_amount_idr, paid_date FROM invoices WHERE id = $1", iid)
    assert (inv["paid_amount_idr"], inv["paid_date"]) == (0, None)
    assert await _standing_cashout(db) == 0


async def test_an_older_part_is_refused_while_a_newer_part_of_the_same_case_stands(db) -> None:
    """Two parts, both leaving the case partial: the payment status alone cannot tell the older
    one is not the newest entry, so restoring its `unpaid` would erase the newer part."""
    pid, iid, first = await _case(db, "on_process")
    await db.execute("UPDATE bank_transactions SET amount_idr = 1000000 WHERE id = $1", first)
    second = await _another_transfer(db, 1_000_000, date(2026, 6, 28))
    for txid in (first, second):
        await confirm_payment(
            db, bank_txn_id=txid, practice_id=pid, invoice_id=iid, amount_applied_idr=1_000_000,
            new_status="partial", confirmed_by=WHO, movement_date=DAY,
        )  # fmt: skip
    before = await _snapshot(db)
    with pytest.raises(ReconcileError, match="has changed since this confirmation"):
        await _undo(db, first)
    assert await _snapshot(db) == before
    await _undo(db, second)
    prac = await db.fetchrow("SELECT payment_status, paid_amount FROM practices WHERE id = $1", pid)
    assert (prac["payment_status"], prac["paid_amount"]) == ("partial", 1_000_000)


async def test_a_confirmation_of_the_last_wita_second_of_yesterday_is_refused(db) -> None:
    pid, iid, txid = await _case(db, "on_process")
    confirm = await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    await db.execute(
        "UPDATE reconciliation_log SET confirmed_at ="
        " ((now() AT TIME ZONE 'Asia/Makassar')::date::timestamp AT TIME ZONE 'Asia/Makassar')"
        " - interval '1 second' WHERE id = $1",
        confirm.reconciliation_log_id,
    )
    before = await _snapshot(db)
    with pytest.raises(ReconcileError, match="confirmed before today"):
        await _undo(db, txid)
    assert await _snapshot(db) == before


async def test_a_confirmation_of_the_first_wita_second_of_today_is_undone(db) -> None:
    pid, iid, txid = await _case(db, "on_process")
    confirm = await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    await db.execute(
        "UPDATE reconciliation_log SET confirmed_at ="
        " ((now() AT TIME ZONE 'Asia/Makassar')::date::timestamp AT TIME ZONE 'Asia/Makassar')"
        " WHERE id = $1",
        confirm.reconciliation_log_id,
    )
    await _undo(db, txid)
    assert (
        await db.fetchval("SELECT payment_status FROM practices WHERE id = $1", pid)
    ) == "unpaid"


async def _linked(db):
    pid, iid, txid = await _case(db, "on_process")
    await db.execute("UPDATE practices SET payment_status = 'paid' WHERE id = $1", pid)
    await link_recorded(db, bank_txn_id=txid, practice_id=pid, invoice_id=iid, linked_by=WHO)
    return txid


async def _ignored(db):
    _pid, _iid, txid = await _case(db, "on_process")
    await ignore_transaction(db, bank_txn_id=txid, reason="bukan klien", ignored_by=WHO)
    return txid


async def _undone(db):
    pid, iid, txid = await _case(db, "on_process")
    await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    await _undo(db, txid)
    return txid


async def _paid_by_hand_since(db):
    pid, iid, txid = await _case(db, "on_process")
    await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    await db.execute("UPDATE practices SET payment_status = 'paid' WHERE id = $1", pid)
    return txid


async def _started_then_moved_on(db):
    pid, iid, txid = await _case(db, "sending_invoice")
    await _confirm(db, pid, iid, txid)
    await db.execute("UPDATE practices SET status = 'submitted' WHERE id = $1", pid)
    return txid


async def _an_earlier_link_reopened_since(db):
    """A link, then a confirmation on the same invoice, then the link reopened: the
    confirmation's before_state still holds the link's date, which no payment stands for."""
    pid, iid, linked = await _case(db, "on_process")
    await db.execute("UPDATE practices SET payment_status = 'paid' WHERE id = $1", pid)
    await db.execute("UPDATE bank_transactions SET amount_idr = 1000000 WHERE id = $1", linked)
    await link_recorded(db, bank_txn_id=linked, practice_id=pid, invoice_id=iid, linked_by=WHO)
    txid = await _another_transfer(db, 1_000_000, date(2026, 6, 28))
    await confirm_payment(
        db, bank_txn_id=txid, practice_id=pid, invoice_id=iid, amount_applied_idr=1_000_000,
        new_status="paid", confirmed_by=WHO, movement_date=date(2026, 6, 28),
    )  # fmt: skip
    await reopen_transaction(db, bank_txn_id=linked, reopened_by=WHO)
    return txid


async def _a_newer_link_reopened_since(db):
    """A confirmation, then a link on the same invoice, then the link reopened: the reopen
    recomputed the invoice's paid date, so the before_state is no longer the answer."""
    pid, iid, txid = await _case(db, "on_process")
    await db.execute("UPDATE bank_transactions SET amount_idr = 1000000 WHERE id = $1", txid)
    await confirm_payment(
        db, bank_txn_id=txid, practice_id=pid, invoice_id=iid, amount_applied_idr=1_000_000,
        new_status="paid", confirmed_by=WHO, movement_date=date(2026, 6, 25),
    )  # fmt: skip
    linked = await _another_transfer(db, 1_000_000, date(2026, 6, 28))
    await link_recorded(db, bank_txn_id=linked, practice_id=pid, invoice_id=iid, linked_by=WHO)
    await reopen_transaction(db, bank_txn_id=linked, reopened_by=WHO)
    return txid


async def _an_earlier_link_reopened_while_another_stands(db):
    """Two links, a confirmation, then the older link reopened: refused by design too, even
    though the standing link still holds the date (any reopen after the confirmation)."""
    pid, iid, first = await _case(db, "on_process")
    await db.execute("UPDATE practices SET payment_status = 'paid' WHERE id = $1", pid)
    await db.execute("UPDATE bank_transactions SET amount_idr = 1000000 WHERE id = $1", first)
    await link_recorded(db, bank_txn_id=first, practice_id=pid, invoice_id=iid, linked_by=WHO)
    second = await _another_transfer(db, 1_000_000, date(2026, 6, 30))
    await link_recorded(db, bank_txn_id=second, practice_id=pid, invoice_id=iid, linked_by=WHO)
    txid = await _another_transfer(db, 1_000_000, date(2026, 7, 1))
    await confirm_payment(
        db, bank_txn_id=txid, practice_id=pid, invoice_id=iid, amount_applied_idr=1_000_000,
        new_status="paid", confirmed_by=WHO, movement_date=date(2026, 7, 1),
    )  # fmt: skip
    await reopen_transaction(db, bank_txn_id=first, reopened_by=WHO)
    return txid


async def _never_confirmed(db):
    _pid, _iid, txid = await _case(db, "on_process")
    return txid


async def _missing(db):
    return 999_999


async def _recorded_before_373(db):
    pid, iid, txid = await _case(db, "on_process")
    confirm = await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    await db.execute(
        "UPDATE reconciliation_log SET before_state = NULL WHERE id = $1",
        confirm.reconciliation_log_id,
    )
    return txid


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (_linked, "is not confirmed"),
        (_ignored, "is not confirmed"),
        (_undone, "is not confirmed"),
        (_never_confirmed, "is not confirmed"),
        (_missing, "not found"),
        (_paid_by_hand_since, "has changed since this confirmation"),
        (_started_then_moved_on, "has moved on since this confirmation started it"),
        (_an_earlier_link_reopened_since, "had a link reopened after this confirmation"),
        (_a_newer_link_reopened_since, "had a link reopened after this confirmation"),
        (
            _an_earlier_link_reopened_while_another_stands,
            "had a link reopened after this confirmation",
        ),
        (_recorded_before_373, "before its undo could be recorded"),
    ],
)
async def test_what_cannot_be_undone_is_refused_and_writes_nothing(db, make, message) -> None:
    txid = await make(db)
    before = await _snapshot(db)
    with pytest.raises(ReconcileError, match=message):
        await _undo(db, txid)
    assert await _snapshot(db) == before


async def test_an_undone_transfer_can_be_confirmed_again(db) -> None:
    pid, iid, txid = await _case(db, "on_process")
    await _confirm(db, pid, iid, txid, status="partial", amount=1_000_000)
    await _undo(db, txid)
    again = await _confirm(db, pid, iid, txid)
    assert again.status_before == "unpaid"
    prac = await db.fetchrow("SELECT payment_status, paid_amount FROM practices WHERE id = $1", pid)
    assert (prac["payment_status"], prac["paid_amount"]) == ("paid", 4_000_000)
    assert await _standing_cashout(db) == 4_000_000
    assert DAY.year == 2026
