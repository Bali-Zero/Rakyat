"""Real-PostgreSQL tests of owner rule (e): a link also writes the cash-out row and the paid date.

A transfer linked to a payment recorded by hand enters the cash-out on its own date and dates
the invoice paid; reopening the link reverses that row, append-only, and recomputes the date.
Built on the fixtures of test_accounting_lights_actions_integration. All names and amounts are
invented.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import asyncpg
import pytest

from backend.services.accounting.cashout_service import cashbook_summary, list_cashout
from backend.services.accounting.reconcile_service import (
    REOPEN_REVERSED_NOTE,
    confirm_payment,
    link_recorded,
    reopen_transaction,
)
from backend.tests.services.test_accounting_lights_actions_integration import (
    _bank,
    _Conns,
    _crossing,
    _park_trigger,
    _practice,
    db,  # noqa: F401  (pytest fixture)
)

WHO = "accountant@example.test"


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


async def _bank_on(conn, day: date, amount: int = 4_000_000) -> int:
    txid = await _bank(conn, amount=amount)
    await conn.execute("UPDATE bank_transactions SET txn_date = $1 WHERE id = $2", day, txid)
    return txid


async def _paid_date(conn, iid: int):
    return await conn.fetchval("SELECT paid_date FROM invoices WHERE id = $1", iid)


def _link(conn, tx, p, i):
    return link_recorded(conn, bank_txn_id=tx, practice_id=p, invoice_id=i, linked_by=WHO)


@pytest.mark.parametrize(
    ("already", "expected"),
    [
        (None, date(2026, 6, 25)),  # filled
        (date(2026, 6, 1), date(2026, 6, 25)),  # an earlier date is replaced
        (date(2026, 7, 9), date(2026, 7, 9)),  # a later date is kept
    ],
)
async def test_the_link_dates_the_invoice_with_the_latest_of_the_two_dates(
    db,
    already,
    expected,  # noqa: F811
) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    await conn.execute(
        "UPDATE invoices SET paid_date = $1 WHERE id = $2", already and _utc(already), iid
    )
    txid = await _bank_on(conn, date(2026, 6, 25))
    await _link(conn, txid, pid, iid)
    assert await _paid_date(conn, iid) == _utc(expected)


@pytest.mark.parametrize(
    ("day", "label"),
    [
        (date(2026, 12, 31), "2026-W53"),
        (date(2027, 1, 1), "2026-W53"),
        (date(2027, 1, 4), "2027-W01"),
        (date(2025, 12, 29), "2026-W01"),
    ],
)
async def test_the_cashout_row_sits_in_the_iso_week_of_the_transfer(db, day, label) -> None:  # noqa: F811
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank_on(conn, day)
    await _link(conn, txid, pid, iid)
    row = await conn.fetchrow("SELECT movement_date, week_label FROM weekly_cashout")
    assert (row["movement_date"], row["week_label"]) == (day, label)


async def test_reopening_one_of_two_links_leaves_the_other_links_date(db) -> None:  # noqa: F811
    conn, _ = db
    pid, iid = await _practice(conn, "paid", amount=8_000_000)
    first = await _bank_on(conn, date(2026, 6, 10))
    second = await _bank_on(conn, date(2026, 6, 25))
    await _link(conn, first, pid, iid)
    await _link(conn, second, pid, iid)
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 25))

    await reopen_transaction(conn, bank_txn_id=second, reopened_by=WHO)
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 10))
    standing = await conn.fetch(
        "SELECT linked_bank_txn_id FROM weekly_cashout WHERE reversed_by_id IS NULL"
    )
    assert [r["linked_bank_txn_id"] for r in standing] == [first]


async def test_reopening_a_link_keeps_the_date_a_confirmation_wrote(db) -> None:  # noqa: F811
    conn, _ = db
    pid, iid = await _practice(conn, "partial", amount=8_000_000)
    confirmed = await _bank_on(conn, date(2026, 6, 10))
    await confirm_payment(
        conn,
        bank_txn_id=confirmed,
        practice_id=pid,
        invoice_id=iid,
        amount_applied_idr=4_000_000,
        new_status="paid",
        confirmed_by=WHO,
        movement_date=date(2026, 6, 10),
    )
    linked = await _bank_on(conn, date(2026, 6, 25))
    await _link(conn, linked, pid, iid)
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 25))

    await reopen_transaction(conn, bank_txn_id=linked, reopened_by=WHO)
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 10))
    assert (
        await conn.fetchval(
            "SELECT reconciled_status FROM bank_transactions WHERE id = $1", confirmed
        )
        == "matched"
    )


async def _confirm_on(conn, tx, pid, iid, day: date):
    return await confirm_payment(
        conn,
        bank_txn_id=tx,
        practice_id=pid,
        invoice_id=iid,
        amount_applied_idr=4_000_000,
        new_status="paid",
        confirmed_by=WHO,
        movement_date=day,
    )


async def test_an_older_transfer_confirmed_after_a_newer_link_keeps_the_newer_date(
    db,  # noqa: F811
) -> None:
    """An invoice paid in parts is paid on its latest part, whichever action recorded it."""
    conn, _ = db
    pid, iid = await _practice(conn, "paid", amount=8_000_000)
    later = await _bank_on(conn, date(2026, 6, 25))
    earlier = await _bank_on(conn, date(2026, 6, 10))
    await _link(conn, later, pid, iid)
    await _confirm_on(conn, earlier, pid, iid, date(2026, 6, 10))
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 25))
    standing = await conn.fetch(
        "SELECT linked_bank_txn_id FROM weekly_cashout WHERE reversed_by_id IS NULL ORDER BY id"
    )
    assert [r["linked_bank_txn_id"] for r in standing] == [later, earlier]


async def test_an_older_confirmation_after_a_newer_one_keeps_the_newer_date(db) -> None:  # noqa: F811
    conn, _ = db
    pid, iid = await _practice(conn, "partial", amount=8_000_000)
    later = await _bank_on(conn, date(2026, 6, 25))
    earlier = await _bank_on(conn, date(2026, 6, 10))
    await _confirm_on(conn, later, pid, iid, date(2026, 6, 25))
    await _confirm_on(conn, earlier, pid, iid, date(2026, 6, 10))
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 25))


@pytest.mark.parametrize("first", ["link", "confirm"])
async def test_a_link_and_a_confirmation_crossing_on_one_invoice_keep_the_latest_date(
    db,  # noqa: F811
    first,
) -> None:
    """Either action parked while holding the practice, the other queued on it: the invoice
    ends on the later transfer's date whichever commits last."""
    conn, dbname = db
    pid, iid = await _practice(conn, "paid", amount=8_000_000)
    later = await _bank_on(conn, date(2026, 6, 25))
    earlier = await _bank_on(conn, date(2026, 6, 10))

    def do_link(x):
        return _link(x, later, pid, iid)

    def do_confirm(x):
        return _confirm_on(x, earlier, pid, iid, date(2026, 6, 10))

    if first == "link":
        await _park_trigger(
            conn, "bank_transactions", 11, "WHEN (NEW.reconciled_status = 'manual')"
        )
        pair = (do_link, do_confirm)
    else:
        await _park_trigger(conn, "practices", 11)
        pair = (do_confirm, do_link)
    async with _Conns(conn, dbname) as c:
        a, b = await _crossing(c, 11, *pair)
    assert not isinstance(a, BaseException), a
    assert not isinstance(b, BaseException), b
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 25))
    assert (
        await conn.fetchval("SELECT count(*) FROM weekly_cashout WHERE reversed_by_id IS NULL") == 2
    )


async def test_link_reopen_link_counts_the_transfer_once_in_the_cashout_and_its_summary(
    db,  # noqa: F811
) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank_on(conn, date(2026, 6, 25))
    await _link(conn, txid, pid, iid)
    reopened = await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)
    assert await list_cashout(conn) == []
    assert (await cashbook_summary(conn))["income_idr"] == 0

    again = await _link(conn, txid, pid, iid)
    rows = await list_cashout(conn)
    assert [(r["linked_invoice_id"], r["amount_idr"], r["reversed_by_id"]) for r in rows] == [
        (iid, 4_000_000, None)
    ]
    summary = await cashbook_summary(conn)
    assert (summary["income_idr"], summary["row_count"]) == (4_000_000, 1)
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 25))
    log = await conn.fetch("SELECT id, cashout_id, note FROM reconciliation_log ORDER BY id")
    assert log[1]["id"] == reopened.reconciliation_log_id
    assert log[1]["note"] == REOPEN_REVERSED_NOTE
    assert log[2]["id"] == again.reconciliation_log_id
    assert log[2]["cashout_id"] == rows[0]["id"]


async def test_a_second_reopen_finds_nothing_left_to_reverse(db) -> None:  # noqa: F811
    """The reversal row carries the transfer's id too, but it is itself marked reversed."""
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank_on(conn, date(2026, 6, 25))
    await _link(conn, txid, pid, iid)
    await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)
    await conn.execute(
        "UPDATE bank_transactions SET reconciled_status = 'manual', matched_practice_id = $1,"
        " matched_invoice_id = $2 WHERE id = $3",
        pid,
        iid,
        txid,
    )  # a manual row again, with no link of rule (e) behind it
    res = await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)
    assert await conn.fetchval("SELECT count(*) FROM weekly_cashout") == 2
    log = await conn.fetchrow(
        "SELECT cashout_id, note FROM reconciliation_log WHERE id = $1", res.reconciliation_log_id
    )
    assert (log["cashout_id"], log["note"]) == (None, "reopened; no money moved")


async def test_a_failure_after_the_reversal_rolls_the_whole_reopen_back(db) -> None:  # noqa: F811
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    txid = await _bank_on(conn, date(2026, 6, 25))
    await _link(conn, txid, pid, iid)
    await conn.execute(
        "CREATE FUNCTION fail_log() RETURNS trigger LANGUAGE plpgsql AS"
        " $$ BEGIN RAISE EXCEPTION 'log refused'; END $$"
    )
    await conn.execute(
        "CREATE TRIGGER fail_log BEFORE INSERT ON reconciliation_log FOR EACH ROW"
        " EXECUTE FUNCTION fail_log()"
    )
    snapshot = "SELECT * FROM {} ORDER BY id"
    before = {
        t: [dict(r) for r in await conn.fetch(snapshot.format(t))]
        for t in ("invoices", "weekly_cashout", "bank_transactions")
    }
    with pytest.raises(asyncpg.RaiseError):
        await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)
    for t, rows in before.items():
        assert [dict(r) for r in await conn.fetch(snapshot.format(t))] == rows, t


async def test_a_reopen_holding_the_practice_and_a_link_on_the_same_invoice_do_not_deadlock(
    db,  # noqa: F811
) -> None:
    """The reopen arrives first and is parked after it reversed the cash-out row (its UPDATE of
    that row fires the trigger), holding the practice; the link of another transfer to the same
    invoice then queues on the practice. Both complete, in that order."""
    conn, dbname = db
    pid, iid = await _practice(conn, "paid")
    t_old = await _bank_on(conn, date(2026, 6, 10))
    t_new = await _bank_on(conn, date(2026, 6, 25))
    await _link(conn, t_old, pid, iid)
    await _park_trigger(conn, "weekly_cashout", 9)
    async with _Conns(conn, dbname) as c:
        a, b = await _crossing(
            c,
            9,
            lambda x: reopen_transaction(x, bank_txn_id=t_old, reopened_by=WHO),
            lambda x: _link(x, t_new, pid, iid),
        )
    assert not isinstance(a, BaseException), a
    assert not isinstance(b, BaseException), b
    rows = await conn.fetch("SELECT id, reconciled_status FROM bank_transactions ORDER BY id")
    assert [(r["id"], r["reconciled_status"]) for r in rows] == [
        (t_old, "unmatched"),
        (t_new, "manual"),
    ]
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 25))
    standing = await conn.fetch(
        "SELECT linked_bank_txn_id FROM weekly_cashout WHERE reversed_by_id IS NULL"
    )
    assert [r["linked_bank_txn_id"] for r in standing] == [t_new]


async def test_a_link_holding_the_invoice_and_a_reopen_on_the_same_invoice_do_not_deadlock(
    db,  # noqa: F811
) -> None:
    """The link arrives first and is parked after it took the practice and a share lock on the
    invoice (its UPDATE of the bank row fires the trigger); the reopen of an earlier link to the
    same invoice arrives and waits on the practice (its own lock, and the key lock the reversal
    row's reference to the practice takes), so it recomputes the paid date only after the link
    committed: the link's later date stands and nothing deadlocks."""
    conn, dbname = db
    pid, iid = await _practice(conn, "paid", amount=8_000_000)
    t_old = await _bank_on(conn, date(2026, 6, 10))
    t_new = await _bank_on(conn, date(2026, 6, 25))
    await _link(conn, t_old, pid, iid)
    await _park_trigger(conn, "bank_transactions", 10, "WHEN (NEW.reconciled_status = 'manual')")
    async with _Conns(conn, dbname) as c:
        a, b = await _crossing(
            c,
            10,
            lambda x: _link(x, t_new, pid, iid),
            lambda x: reopen_transaction(x, bank_txn_id=t_old, reopened_by=WHO),
        )
    assert not isinstance(a, BaseException), a
    assert not isinstance(b, BaseException), b
    rows = await conn.fetch("SELECT id, reconciled_status FROM bank_transactions ORDER BY id")
    assert [(r["id"], r["reconciled_status"]) for r in rows] == [
        (t_old, "unmatched"),
        (t_new, "manual"),
    ]
    assert await _paid_date(conn, iid) == _utc(date(2026, 6, 25))
    standing = await conn.fetch(
        "SELECT linked_bank_txn_id FROM weekly_cashout WHERE reversed_by_id IS NULL"
    )
    assert [r["linked_bank_txn_id"] for r in standing] == [t_new]


async def test_reopen_and_link_on_one_invoice_in_random_order_never_deadlock(db) -> None:  # noqa: F811
    conn, dbname = db
    pid, iid = await _practice(conn, "paid", amount=10**12)
    rounds = 60
    txs = [await _bank_on(conn, date(2026, 6, 1 + i % 28)) for i in range(2 * rounds)]
    for t in txs[::2]:
        await _link(conn, t, pid, iid)
    deadlocks, other = 0, []
    async with _Conns(conn, dbname) as c:
        for k in range(rounds):
            got = await asyncio.gather(
                reopen_transaction(c.a, bank_txn_id=txs[2 * k], reopened_by=WHO),
                _link(c.b, txs[2 * k + 1], pid, iid),
                return_exceptions=True,
            )
            for g in got:
                if isinstance(g, asyncpg.DeadlockDetectedError):
                    deadlocks += 1
                elif isinstance(g, BaseException):
                    other.append(repr(g))
    assert deadlocks == 0 and other == []
    standing = await conn.fetchval(
        "SELECT count(*) FROM weekly_cashout WHERE reversed_by_id IS NULL"
    )
    assert standing == rounds
    latest = await conn.fetchval(
        "SELECT max(txn_date) FROM bank_transactions WHERE reconciled_status = 'manual'"
    )
    assert await _paid_date(conn, iid) == _utc(latest)


async def test_the_cash_out_list_names_the_service_of_the_linked_case(db) -> None:  # noqa: F811
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    ptid = await conn.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('CONTOH', 'Contoh Layanan') RETURNING id"
    )
    await conn.execute("UPDATE practices SET practice_type_id = $1 WHERE id = $2", ptid, pid)
    await _link(conn, await _bank_on(conn, date(2026, 6, 25)), pid, iid)
    pid2, iid2 = await _practice(conn, "paid")  # a case without a practice type
    await _link(conn, await _bank_on(conn, date(2026, 6, 24)), pid2, iid2)

    rows = await list_cashout(conn)

    assert [(r["linked_practice_id"], r["service_name"]) for r in rows] == [
        (pid, "Contoh Layanan"),
        (pid2, None),
    ]
