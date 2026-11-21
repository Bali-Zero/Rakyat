"""Real-PostgreSQL tests of migration 374: links made before rule (e) enter the cash-out once.

The migration gives every transfer linked before #553 what link_recorded writes since: one
invoice_payment cash-out row on the transfer's date and the invoice's paid date. Built on the
fixtures of test_accounting_lights_actions_integration. All names and amounts are invented.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql
from backend.services.accounting.cashout_service import cashbook_summary, list_cashout
from backend.services.accounting.reconcile_service import (
    _iso_week_label,
    link_recorded,
    reopen_transaction,
)
from backend.tests.services.test_accounting_lights_actions_integration import (
    _bank,
    _practice,
    db,  # noqa: F401  (pytest fixture)
)

BACKEND = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND / "db" / "migrations_v2" / "374_link_cashout_backfill.sql"
MARK = "older link, added to the cash-out by migration 374"
WHO = "accountant@example.test"
OLD_NOTE = "linked to a payment already recorded; no money moved"
LINKED_AT = datetime(2026, 10, 2, 3, 4, 5, tzinfo=timezone.utc)


def _sql() -> tuple[str, str]:
    return split_migration_sql(MIGRATION.read_text())


async def _forward(conn) -> None:
    async with conn.transaction():
        await conn.execute(_sql()[0])


async def _rollback(conn) -> None:
    async with conn.transaction():
        await conn.execute(_sql()[1])


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


async def _old_link(
    conn, day: date, amount: int = 4_000_000, *, by: str | None = WHO, invoice=None
):
    """A transfer linked before rule (e): manual, its log row, no cash-out row."""
    if invoice is None:
        pid, iid = await _practice(conn, "paid")
    else:
        pid, iid = invoice
    txid = await _bank(conn, status="manual", practice=pid, invoice=iid, amount=amount)
    await conn.execute("UPDATE bank_transactions SET txn_date = $1 WHERE id = $2", day, txid)
    if by is not None:
        await conn.execute(
            "INSERT INTO reconciliation_log (bank_txn_id, invoice_ids, practice_id, confirmed_by,"
            " confirmed_at, status_before, status_after, amount_applied_idr, note)"
            " VALUES ($1, $2, $3, $4, $5, 'paid', 'paid', 0, $6)",
            txid,
            [iid],
            pid,
            by,
            LINKED_AT,
            OLD_NOTE,
        )
    return pid, iid, txid


async def _rows(conn):
    return [dict(r) for r in await conn.fetch("SELECT * FROM weekly_cashout ORDER BY id")]


async def test_an_old_link_gets_the_row_and_the_paid_date_a_link_writes_today(db) -> None:
    conn, _ = db
    pid, iid, txid = await _old_link(conn, date(2026, 10, 7), 5_500_000)

    await _forward(conn)

    rows = await _rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert {
        k: row[k]
        for k in (
            "movement_date",
            "week_label",
            "direction",
            "type",
            "amount_idr",
            "linked_practice_id",
            "linked_invoice_id",
            "linked_bank_txn_id",
            "description",
            "payment_method",
            "recorded_by",
            "recorded_at",
            "reversed_by_id",
        )
    } == {
        "movement_date": date(2026, 10, 7),
        "week_label": "2026-W41",
        "direction": "in",
        "type": "invoice_payment",
        "amount_idr": 5_500_000,
        "linked_practice_id": pid,
        "linked_invoice_id": iid,
        "linked_bank_txn_id": txid,
        "description": MARK,
        "payment_method": "bank_transfer",
        "recorded_by": WHO,
        "recorded_at": LINKED_AT,
        "reversed_by_id": None,
    }
    paid = await conn.fetchval("SELECT paid_date FROM invoices WHERE id = $1", iid)
    assert paid == _utc(date(2026, 10, 7))


async def test_the_row_matches_what_link_recorded_writes_for_the_same_transfer(db) -> None:
    conn, _ = db
    _, _, old_tx = await _old_link(conn, date(2026, 11, 3))
    pid, iid = await _practice(conn, "paid")
    new_tx = await _bank(conn)
    await conn.execute(
        "UPDATE bank_transactions SET txn_date = $1 WHERE id = $2", date(2026, 11, 3), new_tx
    )
    await link_recorded(conn, bank_txn_id=new_tx, practice_id=pid, invoice_id=iid, linked_by=WHO)

    await _forward(conn)

    by_tx = {r["linked_bank_txn_id"]: r for r in await _rows(conn)}
    same = (
        "movement_date",
        "week_label",
        "direction",
        "type",
        "amount_idr",
        "payment_method",
        "recorded_by",
        "reversed_by_id",
    )
    assert {k: by_tx[old_tx][k] for k in same} == {k: by_tx[new_tx][k] for k in same}
    assert by_tx[new_tx]["description"] is None and by_tx[old_tx]["description"] == MARK


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 12, 28),
        date(2026, 12, 31),
        date(2027, 1, 1),
        date(2027, 1, 3),
        date(2027, 1, 4),
        date(2026, 10, 1),
    ],
)
async def test_the_week_label_is_the_one_the_code_writes(db, day) -> None:
    conn, _ = db
    await _old_link(conn, day)
    await _forward(conn)
    assert [r["week_label"] for r in await _rows(conn)] == [_iso_week_label(day)]


async def test_what_the_migration_does_not_touch(db) -> None:
    conn, _ = db
    pid, iid = await _practice(conn, "paid")
    await _bank(conn)  # unmatched
    await _bank(conn, status="matched", practice=pid, invoice=iid)  # a confirmation's
    await _bank(conn, status="manual", practice=pid, invoice=None)  # manual with no invoice
    await _bank(conn, direction="debit", status="ignored")
    linked_pid, linked_iid = await _practice(conn, "paid")
    linked_tx = await _bank(conn)
    await link_recorded(
        conn, bank_txn_id=linked_tx, practice_id=linked_pid, invoice_id=linked_iid, linked_by=WHO
    )
    before = await _rows(conn)
    tables = ("practices", "bank_transactions", "reconciliation_log", "invoices")
    snap = {
        t: [dict(r) for r in await conn.fetch(f"SELECT * FROM {t} ORDER BY id")] for t in tables
    }

    await _forward(conn)

    assert await _rows(conn) == before
    for t in tables:
        assert [dict(r) for r in await conn.fetch(f"SELECT * FROM {t} ORDER BY id")] == snap[t], t


@pytest.mark.parametrize(
    ("already", "expected"),
    [
        (None, date(2026, 10, 7)),
        (date(2026, 9, 1), date(2026, 10, 7)),
        (date(2026, 11, 2), date(2026, 11, 2)),
    ],
)
async def test_the_paid_date_only_moves_forward(db, already, expected) -> None:
    conn, _ = db
    _, iid, _ = await _old_link(conn, date(2026, 10, 7))
    await conn.execute(
        "UPDATE invoices SET paid_date = $1 WHERE id = $2", already and _utc(already), iid
    )
    await _forward(conn)
    assert await conn.fetchval("SELECT paid_date FROM invoices WHERE id = $1", iid) == _utc(
        expected
    )


async def test_two_old_links_to_one_invoice_give_two_rows_and_the_later_date(db) -> None:
    conn, _ = db
    pid, iid, _ = await _old_link(conn, date(2026, 10, 20), 1_000_000)
    await _old_link(conn, date(2026, 10, 6), 3_000_000, invoice=(pid, iid))
    await _forward(conn)
    rows = await _rows(conn)
    assert sorted((r["movement_date"], r["amount_idr"]) for r in rows) == [
        (date(2026, 10, 6), 3_000_000),
        (date(2026, 10, 20), 1_000_000),
    ]
    assert await conn.fetchval("SELECT paid_date FROM invoices WHERE id = $1", iid) == _utc(
        date(2026, 10, 20)
    )


async def test_the_newest_link_row_names_the_author_and_none_names_the_migration(db) -> None:
    conn, _ = db
    _, _, first = await _old_link(conn, date(2026, 10, 7))
    await conn.execute(
        "INSERT INTO reconciliation_log (bank_txn_id, confirmed_by, confirmed_at,"
        " amount_applied_idr, note) VALUES ($1, 'later@example.test', $2, 0, $3)",
        first,
        datetime(2026, 10, 9, tzinfo=timezone.utc),
        OLD_NOTE,
    )
    _, _, second = await _old_link(conn, date(2026, 10, 8), by=None)
    await _forward(conn)
    by_tx = {r["linked_bank_txn_id"]: r for r in await _rows(conn)}
    assert (by_tx[first]["recorded_by"], by_tx[first]["recorded_at"]) == (
        "later@example.test",
        datetime(2026, 10, 9, tzinfo=timezone.utc),
    )
    assert by_tx[second]["recorded_by"] == "migration 374"


async def test_a_second_run_adds_nothing(db) -> None:
    conn, _ = db
    await _old_link(conn, date(2026, 10, 7))
    await _forward(conn)
    once = await _rows(conn)
    await _forward(conn)
    assert await _rows(conn) == once


async def test_the_cash_out_view_and_its_totals_read_the_added_rows(db) -> None:
    conn, _ = db
    await _old_link(conn, date(2026, 10, 7), 5_500_000)
    await _old_link(conn, date(2026, 10, 14), 2_000_000)
    before = await cashbook_summary(
        conn, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31)
    )

    await _forward(conn)

    after = await cashbook_summary(
        conn, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31)
    )
    assert after["income_idr"] - before["income_idr"] == 7_500_000
    listed = await list_cashout(conn)
    assert sorted((i["week_label"], i["amount_idr"]) for i in listed) == [
        ("2026-W41", 5_500_000),
        ("2026-W42", 2_000_000),
    ]


async def test_reopening_an_old_link_after_the_migration_reverses_its_row(db) -> None:
    conn, _ = db
    _, iid, txid = await _old_link(conn, date(2026, 10, 7))
    await _forward(conn)
    added = (await _rows(conn))[0]

    await reopen_transaction(conn, bank_txn_id=txid, reopened_by=WHO)

    rows = {r["id"]: r for r in await _rows(conn)}
    assert len(rows) == 2
    reversal = next(r for r in rows.values() if r["id"] != added["id"])
    assert (rows[added["id"]]["reversed_by_id"], reversal["reversed_by_id"]) == (
        reversal["id"],
        added["id"],
    )
    assert await conn.fetchval("SELECT paid_date FROM invoices WHERE id = $1", iid) is None


async def test_the_rollback_refuses_and_changes_nothing(db) -> None:
    conn, _ = db
    await _old_link(conn, date(2026, 10, 7))
    await _forward(conn)
    before = await _rows(conn)
    paid_before = await conn.fetch("SELECT id, paid_date FROM invoices ORDER BY id")

    with pytest.raises(asyncpg.RaiseError, match="rollback 374 refused"):
        await _rollback(conn)

    assert await _rows(conn) == before
    assert await conn.fetch("SELECT id, paid_date FROM invoices ORDER BY id") == paid_before


async def test_on_a_schema_without_the_accounting_tables_the_forward_skips(db) -> None:
    """CI applies migrations_v2 to an empty schema: 374 must skip there, not fail."""
    conn, _ = db
    await conn.execute("CREATE SCHEMA empty_374")
    try:
        await conn.execute("SET search_path TO empty_374")
        await _forward(conn)
        assert (
            await conn.fetchval("SELECT count(*) FROM pg_tables WHERE schemaname = 'empty_374'")
            == 0
        )
    finally:
        await conn.execute("SET search_path TO DEFAULT")
        await conn.execute("DROP SCHEMA empty_374")
