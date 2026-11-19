"""Real-PostgreSQL proof of the bank statement import (guarantees I1 to I7, I10).

A mock cannot show the unique index, the ON CONFLICT counts, the rollback or the exact
NUMERIC / timestamp round trip, so these tests run against a real server: each one
builds a scratch database, applies migrations 238, 239 and 363 forward-only, and drops
it. Skipped when no PostgreSQL is reachable (the hosted CI provides one through
TEST_DATABASE_URL). Every value is invented.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from backend.services.accounting import bank_import_service
from backend.services.accounting.bank_import_service import BankImportError, import_statement
from backend.tests.fixtures.bank_statement_builder import ACCOUNT, at, build_statement

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
M238 = MIGRATIONS / "238_accounting_asya.sql"
M239 = MIGRATIONS / "239_cashout_worksheet_type.sql"
M363 = MIGRATIONS / "363_bank_statement_import.sql"

STUBS = """
CREATE TABLE IF NOT EXISTS team_members (id VARCHAR PRIMARY KEY);
CREATE TABLE IF NOT EXISTS clients (id SERIAL PRIMARY KEY, full_name VARCHAR);
CREATE TABLE IF NOT EXISTS practices (
    id SERIAL PRIMARY KEY, client_id INT, payment_status VARCHAR DEFAULT 'unpaid',
    paid_amount NUMERIC DEFAULT 0, updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS invoices (
    id SERIAL PRIMARY KEY, practice_id INT UNIQUE, invoice_number VARCHAR,
    amount_idr BIGINT, generated_at TIMESTAMPTZ DEFAULT now());
"""


def _sql(path: Path, *, rollback: bool = False) -> str:
    parts = re.split(r"^-- === ROLLBACK ===\s*$", path.read_text("utf-8"), maxsplit=1, flags=re.M)
    return parts[1] if rollback else parts[0]


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/postgres",
    )


async def _create_db(*, with_363: bool = True) -> tuple[asyncpg.Connection, str]:
    try:
        admin = await asyncpg.connect(_dsn(), database="postgres", timeout=3)
    except Exception:
        pytest.skip("no Postgres reachable for the bank import integration tests")
    name = f"acc_import_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    conn = await asyncpg.connect(_dsn(), database=name)
    await conn.execute(STUBS)
    await conn.execute(_sql(M238))
    await conn.execute(_sql(M239))
    if with_363:
        await conn.execute(_sql(M363))
    return conn, name


async def _drop_db(conn: asyncpg.Connection, name: str) -> None:
    await conn.close()
    admin = await asyncpg.connect(_dsn(), database="postgres")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture
async def db():
    conn, name = await _create_db()
    try:
        yield conn, name
    finally:
        await _drop_db(conn, name)


async def _counts(conn: asyncpg.Connection) -> tuple[int, int]:
    return (
        await conn.fetchval("SELECT count(*) FROM bank_statements"),
        await conn.fetchval("SELECT count(*) FROM bank_transactions"),
    )


def _first_rows():
    # day 2..4, opening 1,000,000.00: debit, credit (with cents), debit
    return [
        (at(2, 9), "DIRECT DEBIT", "100000.00", "0.00"),
        (at(3, 10), "REMITTANCE CR - BIFAST", "0.00", "250000.50"),
        (at(4, 11), "TR TO REMITT", "300000.00", "0.00"),
    ]


OPENING = "10000000.00"


def _full_ledger() -> list:
    """Seven days, three postings a day, invented amounts."""
    rows = []
    for d in range(1, 8):
        rows += [
            (at(d, 9), "DIRECT DEBIT", "100000.00", "0.00"),
            (at(d, 12), "REMITTANCE CR - BIFAST", "0.00", "250000.50"),
            (at(d, 16), "DIRECT DEBIT", "30000.00", "0.00"),
        ]
    return rows


def _ledger(d1: int, d2: int, *, upto=None) -> list:
    return [r for r in _full_ledger() if d1 <= r[0].day <= d2 and (upto is None or r[0] <= upto)]


def _stmt(rows: list, *, opening_of: list | None = None, shift: str = "0.00"):
    """A statement of `rows`, whose opening balance is what the full ledger had before them
    (plus `shift`, to open a hole)."""
    from decimal import Decimal as D

    base = D(OPENING)
    for r in _full_ledger():
        if r[0] < rows[0][0]:
            base += D(r[3]) - D(r[2])
    return build_statement(rows, opening=f"{base + D(shift):.2f}")


async def _import(conn, statement, name="a.csv"):
    return await import_statement(conn, statement, uploaded_by="asya@example.test", source_filename=name)


# ── I3 + I4: identity, re-import, overlap, nothing new ──────────────────────


async def test_i3_unique_index_is_in_the_database(db) -> None:
    conn, _ = db
    sid = await conn.fetchval("INSERT INTO bank_statements (source_format) VALUES ('csv') RETURNING id")
    ins = (
        "INSERT INTO bank_transactions (statement_id, txn_date, amount_idr, direction, bank_account_id,"
        " posted_at, debit, credit, balance, dup_ordinal)"
        " VALUES ($1, '2026-11-02', 5, 'debit', 'ACC', '2026-11-02 09:00:00', 5, 0, 10, 0)"
    )
    await conn.execute(ins, sid)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(ins, sid)
    # rows without posted_at (legacy) are outside the index
    legacy = "INSERT INTO bank_transactions (statement_id, txn_date, amount_idr, direction) VALUES ($1, '2026-11-02', 1, 'debit')"
    await conn.execute(legacy, sid)
    await conn.execute(legacy, sid)


async def test_i3_first_import_stores_every_row_and_the_counts_sum(db) -> None:
    conn, _ = db
    stmt = build_statement(_first_rows(), opening="1000000.00")
    result = await _import(conn, stmt)
    assert (result.rows_inserted, result.rows_duplicate, result.nothing_new) == (3, 0, False)
    assert result.statement_id is not None
    assert await _counts(conn) == (1, 3)
    stored = await conn.fetchrow(
        "SELECT rows_inserted, rows_duplicate, txn_count FROM bank_statements WHERE id=$1",
        result.statement_id,
    )
    assert tuple(stored) == (3, 0, 3)


async def test_i3_i4_reimport_inserts_nothing_and_adds_no_statement(db) -> None:
    conn, _ = db
    stmt = build_statement(_first_rows(), opening="1000000.00")
    await _import(conn, stmt)
    again = await _import(conn, stmt)
    assert again.nothing_new is True
    assert again.statement_id is None
    assert (again.rows_inserted, again.rows_duplicate) == (0, 3)
    assert await _counts(conn) == (1, 3)  # zero new statement rows, zero new transactions


async def test_i3_overlap_inserts_only_the_rows_not_yet_stored(db) -> None:
    conn, _ = db
    await _import(conn, build_statement(_first_rows(), opening="1000000.00"))
    longer = _first_rows() + [(at(5, 12), "DIRECT DEBIT", "50000.00", "0.00")]
    result = await _import(conn, build_statement(longer, opening="1000000.00"))
    assert (result.rows_inserted, result.rows_duplicate) == (1, 3)
    assert result.rows_inserted + result.rows_duplicate == 4
    assert await _counts(conn) == (2, 4)
    # the overlap statement row records its own counts
    row = await conn.fetchrow(
        "SELECT rows_inserted, rows_duplicate, txn_count FROM bank_statements WHERE id=$1", result.statement_id
    )
    assert tuple(row) == (1, 3, 4)


async def test_ordinals_keep_two_identical_zero_amount_rows_and_rematch_them(db) -> None:
    conn, _ = db
    rows = [
        (at(2, 9), "DIRECT DEBIT", "100.00", "0.00"),
        (at(3, 9), "MISCELLANEOUS CHARGES", "0.00", "0.00"),
        (at(3, 9), "MISCELLANEOUS CHARGES", "0.00", "0.00"),
        (at(4, 9), "DIRECT DEBIT", "100.00", "0.00"),
    ]
    stmt = build_statement(rows, opening="1000.00")
    first = await _import(conn, stmt)
    assert first.rows_inserted == 4
    ordinals = await conn.fetch(
        "SELECT dup_ordinal FROM bank_transactions WHERE debit = 0 AND credit = 0 ORDER BY dup_ordinal"
    )
    assert [r["dup_ordinal"] for r in ordinals] == [0, 1]
    again = await _import(conn, stmt)
    assert again.nothing_new and again.rows_duplicate == 4
    assert await _counts(conn) == (1, 4)


# ── I5 + I1 + I2: disagreement and refusals leave nothing behind ───────────


async def test_i5_i1_disagreement_is_refused_and_leaves_nothing_behind(db) -> None:
    conn, _ = db
    await _import(conn, build_statement(_first_rows(), opening="1000000.00"))
    before = await _counts(conn)
    # same period, but the middle row differs: a stored row is not among the new rows
    rows = _first_rows()
    rows[1] = (at(3, 10), "REMITTANCE CR - BIFAST", "0.00", "999999.00")
    with pytest.raises(BankImportError) as info:
        await _import(conn, build_statement(rows, opening="1000000.00"))
    assert "disagrees" in str(info.value)
    assert await _counts(conn) == before  # the rows inserted before the check were rolled back
    assert await conn.fetchval("SELECT count(*) FROM bank_transactions WHERE credit = 999999") == 0


async def test_i2_refused_statements_store_nothing(db) -> None:
    conn, _ = db
    tampered = build_statement(_first_rows(), opening="1000000.00", tamper_balance_of=1)
    with pytest.raises(BankImportError, match="running-balance check"):
        await _import(conn, tampered)
    no_account = build_statement(_first_rows(), opening="1000000.00", account=None)
    with pytest.raises(BankImportError, match="account number"):
        await _import(conn, no_account)
    assert await _counts(conn) == (0, 0)


async def test_i1_a_failure_after_the_inserts_rolls_everything_back(db, monkeypatch) -> None:
    conn, _ = db

    async def boom(*_a, **_k):
        raise RuntimeError("fault injected after the transaction rows were inserted")

    monkeypatch.setattr(bank_import_service, "_finalize_statement", boom)
    with pytest.raises(RuntimeError):
        await _import(conn, build_statement(_first_rows(), opening="1000000.00"))
    assert await _counts(conn) == (0, 0)


async def test_i1_an_error_raised_by_the_database_mid_insert_leaves_nothing_behind(db) -> None:
    from datetime import date

    from backend.services.accounting.bank_parser import ParsedStatement, ParsedTxn

    def txn(at_, debit, balance):
        return ParsedTxn(
            txn_date=at_.date(), description="d", amount_idr=int(Decimal(debit)), direction="debit",
            running_balance_idr=int(Decimal(balance)), raw_row={}, posted_at=at_, debit=Decimal(debit),
            credit=Decimal(0), balance=Decimal(balance), kind="other",
        )

    # the second debit has 17 integer digits: it overflows NUMERIC(18, 2) inside the INSERT
    huge = "99999999999999999.00"
    rows = [txn(at(2), "10.00", "90.00"), txn(at(3), huge, str(Decimal("90.00") - Decimal(huge)))]
    stmt = ParsedStatement(
        account_number="700000000001", account_name="X", metadata={}, transactions=rows
    )
    assert date(2026, 11, 2) == rows[0].txn_date
    with pytest.raises(asyncpg.exceptions.NumericValueOutOfRangeError):
        await _import(conn := db[0], stmt)
    assert await _counts(conn) == (0, 0)  # neither the statement row nor any transaction
    # the connection is still usable and the next, valid import works
    assert (await _import(conn, build_statement(_first_rows(), opening="1000000.00"))).rows_inserted == 3


# ── I6: exact values ───────────────────────────────────────────────────────


async def test_i6_values_round_trip_exactly(db) -> None:
    conn, _ = db
    rows = [
        (at(2, 9, 0, 5), "DIRECT DEBIT", "10.50", "0.00"),             # balance 2.50 -> opening 13.00
        (at(3, 10, 30, 1), "TR TO REMITT", "12345678.25", "0.00"),     # drives the balance negative
        (at(4, 11, 59, 59), "REMITTANCE CR - SKN", "0.00", "0.50"),
    ]
    stmt = build_statement(rows, opening="13.00", account="700000000009")
    filename = "x" * 300 + ".csv"
    result = await import_statement(conn, stmt, uploaded_by="asya@example.test", source_filename=filename)
    got = await conn.fetch(
        "SELECT * FROM bank_transactions WHERE statement_id = $1 ORDER BY posted_at", result.statement_id
    )
    assert [r["posted_at"].isoformat() for r in got] == [
        "2026-11-02T09:00:05", "2026-11-03T10:30:01", "2026-11-04T11:59:59",
    ]
    assert [r["debit"] for r in got] == [Decimal("10.50"), Decimal("12345678.25"), Decimal("0.00")]
    assert [r["credit"] for r in got] == [Decimal("0.00"), Decimal("0.00"), Decimal("0.50")]
    assert [r["balance"] for r in got] == [Decimal("2.50"), Decimal("-12345675.75"), Decimal("-12345675.25")]
    assert [r["kind"] for r in got] == ["payroll", "transfer_out", "client_credit"]
    # the columns the existing readers use, from the same row
    assert [r["txn_date"].isoformat() for r in got] == ["2026-11-02", "2026-11-03", "2026-11-04"]
    assert [r["amount_idr"] for r in got] == [11, 12345678, 1]  # 10.50 -> 11, 0.50 -> 1 (half up)
    assert [r["running_balance_idr"] for r in got] == [3, -12345676, -12345675]
    assert [r["direction"] for r in got] == ["debit", "debit", "credit"]
    assert {r["bank_account_id"] for r in got} == {"700000000009"}
    assert got[0]["description"] == "desc0"
    assert got[0]["reconciled_status"] == "unmatched"
    assert got[0]["raw_row"] is not None
    st = await conn.fetchrow("SELECT * FROM bank_statements WHERE id = $1", result.statement_id)
    assert st["bank_code"] == "cimb_niaga" and st["source_format"] == "csv"
    assert st["parse_status"] == "parsed" and st["balance_check_ok"] is True
    assert st["uploaded_by"] == "asya@example.test"
    assert len(st["source_filename"]) == 255
    assert st["account_number"] == "700000000009" and st["account_name"] == "PT CONTOH UJI"
    assert st["opening_balance"] == Decimal("13.00") and st["closing_balance"] == Decimal("-12345675.25")
    assert st["period_first_at"].isoformat() == "2026-11-02T09:00:05"
    assert st["period_last_at"].isoformat() == "2026-11-04T11:59:59"
    assert (st["period_start"].isoformat(), st["period_end"].isoformat()) == ("2026-11-02", "2026-11-04")
    assert (st["opening_balance_idr"], st["closing_balance_idr"]) == (13, -12345675)
    assert st["txn_count"] == 3 and st["order_decided_by"] == "timestamps"


async def test_i6_whole_rupiah_columns_round_half_away_from_zero(db) -> None:
    conn, _ = db
    rows = [
        (at(2, 9), "DIRECT DEBIT", "0.50", "0.00"),             # amount 0.50 -> 1, balance 2.00
        (at(3, 9), "REMITTANCE CR - BIFAST", "0.00", "0.50"),   # amount 0.50 -> 1, balance 2.50 -> 3 (half-even: 2)
        (at(4, 9), "TR TO REMITT", "5.00", "0.00"),             # balance -2.50 -> -3 (half-even: -2)
        (at(5, 9), "REMITTANCE CR - BIFAST", "0.00", "10.00"),  # balance 7.50 -> 8
        (at(6, 9), "DIRECT DEBIT", "10.00", "0.00"),            # closing -2.50 -> -3 (half-even: -2)
    ]
    stmt = build_statement(rows, opening="2.50", account="700000000009")  # opening 2.50 -> 3 (half-even: 2)
    result = await _import(conn, stmt)
    got = await conn.fetch(
        "SELECT amount_idr, running_balance_idr FROM bank_transactions WHERE statement_id=$1 ORDER BY posted_at",
        result.statement_id,
    )
    assert [r["amount_idr"] for r in got] == [1, 1, 5, 10, 10]
    assert [r["running_balance_idr"] for r in got] == [2, 3, -3, 8, -3]
    st = await conn.fetchrow("SELECT opening_balance_idr, closing_balance_idr FROM bank_statements WHERE id=$1", result.statement_id)
    assert (st["opening_balance_idr"], st["closing_balance_idr"]) == (3, -3)
    neg = await _import(
        conn,
        build_statement([(at(9, 9), "DIRECT DEBIT", "1.00", "0.00")], opening="-0.50", account="700000000008"),
    )
    s2 = await conn.fetchrow(
        "SELECT opening_balance_idr, closing_balance_idr FROM bank_statements WHERE id=$1", neg.statement_id
    )
    assert (s2["opening_balance_idr"], s2["closing_balance_idr"]) == (-1, -2)  # -0.50 -> -1, -1.50 -> -2


# ── I5: two exports must agree where both are settled ────────────────────────


async def _stored_everything(conn) -> object:
    await _import(conn, _stmt(_full_ledger()))
    return await _counts(conn)


def _with(rows: list, extra: list) -> list:
    return sorted(rows + extra, key=lambda r: r[0])


_MID = at(4, 10)
_DISHONEST = {
    "row_removed": lambda rows: [r for r in rows if r[0] != at(4, 12)],
    "row_added": lambda rows: _with(rows, [(_MID, "REMITTANCE CR - BIFAST", "0.00", "5000000.00")]),
    "amount_changed": lambda rows: [
        (r[0], r[1], r[2], "250001.50") if r[0] == at(4, 12) else r for r in rows
    ],
    "zero_amount_added": lambda rows: _with(rows, [(_MID, "MISCELLANEOUS CHARGES", "0.00", "0.00")]),
    "cancelling_pair_added": lambda rows: _with(
        rows,
        [(_MID, "REMITTANCE CR - BIFAST", "0.00", "5000000.00"), (at(4, 10, 0, 30), "TR TO REMITT", "5000000.00", "0.00")],
    ),
}


@pytest.mark.parametrize("variant", list(_DISHONEST))
async def test_i5_i2_i1_a_changed_export_of_a_settled_period_is_refused_and_leaves_nothing(db, variant) -> None:
    conn, _ = db
    before = await _stored_everything(conn)
    changed = _DISHONEST[variant](_full_ledger())
    with pytest.raises(BankImportError, match="disagrees with rows already stored"):
        await _import(conn, _stmt(changed))
    assert await _counts(conn) == before


async def test_i5a_alone_a_stored_zero_amount_row_the_new_export_omits_is_refused(db) -> None:
    conn, _ = db
    with_zero = _with(_full_ledger(), [(_MID, "MISCELLANEOUS CHARGES", "0.00", "0.00")])
    await _import(conn, _stmt(with_zero))
    before = await _counts(conn)
    # same rows, later balances unchanged, only the zero-amount row missing: nothing new is
    # inserted, so only rule (a) can see it
    with pytest.raises(BankImportError, match=r"\(1 stored row\(s\) in its settled period are not in it\)"):
        await _import(conn, _stmt(_full_ledger()))
    assert await _counts(conn) == before


async def test_i5b_alone_a_new_zero_amount_row_inside_a_stored_settled_period_is_refused(db) -> None:
    conn, _ = db
    before = await _stored_everything(conn)
    added = _with(_full_ledger(), [(_MID, "MISCELLANEOUS CHARGES", "0.00", "0.00")])
    with pytest.raises(BankImportError, match=r"\(1 new row\(s\) fall inside the settled period of a stored statement\)"):
        await _import(conn, _stmt(added))
    assert await _counts(conn) == before


@pytest.mark.parametrize(
    ("seconds_before_end", "accepted"),
    [(1, True), (2, True), (3, False)],
    ids=["last_second", "exactly_two_seconds_before_the_end", "three_seconds_before_the_end"],
)
async def test_i5_stated_limit_the_last_two_seconds_of_a_stored_period_are_not_judged(
    db, seconds_before_end, accepted
) -> None:
    conn, _ = db
    before = await _stored_everything(conn)
    from datetime import timedelta

    end = _full_ledger()[-1][0]  # the stored period's last instant
    extra = [(end - timedelta(seconds=seconds_before_end), "MISCELLANEOUS CHARGES", "0.00", "0.00")]
    attempt = _stmt(_with(_full_ledger(), extra))
    if accepted:
        result = await _import(conn, attempt)
        assert result.rows_inserted == 1 and result.statement_id is not None
    else:
        with pytest.raises(BankImportError):
            await _import(conn, attempt)
        assert await _counts(conn) == before


def _batch_tail():
    """The bank's real shape: two postings stamped one second out of order at the very end."""
    base = _full_ledger()
    p1 = (at(7, 16, 0, 1), "DIRECT DEBIT", "10.00", "0.00")  # posted first, stamped later
    p2 = (at(7, 16, 0, 0), "DIRECT DEBIT", "40.00", "0.00")  # posted second, stamped earlier
    return base, p1, p2


async def test_i5a_a_stored_row_exactly_two_seconds_before_the_end_is_not_judged(db) -> None:
    from datetime import timedelta

    conn, _ = db
    end = _full_ledger()[-1][0]
    boundary = (end - timedelta(seconds=2), "MISCELLANEOUS CHARGES", "0.00", "0.00")
    await _import(conn, _stmt(_with(_full_ledger(), [boundary])))
    result = await _import(conn, _stmt(_full_ledger()))  # omits the boundary row: still agrees
    assert result.nothing_new is True


async def test_i5a_a_boundary_row_present_in_both_must_not_mask_a_missing_stored_row(db) -> None:
    from datetime import timedelta

    conn, _ = db
    end = _full_ledger()[-1][0]
    boundary = (end - timedelta(seconds=2), "MISCELLANEOUS CHARGES", "0.00", "0.00")
    mid_zero = (_MID, "MISCELLANEOUS CHARGES", "0.00", "0.00")
    await _import(conn, _stmt(_with(_full_ledger(), [boundary, mid_zero])))
    before = await _counts(conn)
    with pytest.raises(BankImportError, match=r"\(1 stored row\(s\) in its settled period"):
        await _import(conn, _stmt(_with(_full_ledger(), [boundary])))  # has the boundary row, lacks mid_zero
    assert await _counts(conn) == before


async def test_i5b_a_new_row_exactly_at_a_stored_periods_first_instant_is_inside_it(db) -> None:
    conn, _ = db
    before = await _stored_everything(conn)
    first = _full_ledger()[0][0]
    with pytest.raises(BankImportError, match="new row"):
        await _import(conn, _stmt(_with([(first, "MISCELLANEOUS CHARGES", "0.00", "0.00")], []) + _full_ledger()))
    assert await _counts(conn) == before


async def test_i5_a_download_cut_in_the_middle_of_a_batch_is_not_a_disagreement(db) -> None:
    conn, _ = db
    base, p1, p2 = _batch_tail()
    await _import(conn, _stmt(base + [p1, p2]))
    before = await _counts(conn)
    cut = await _import(conn, _stmt(base + [p1]))  # p2 is stored, inside the cut's last two seconds
    assert cut.nothing_new is True and cut.rows_duplicate == len(base) + 1
    assert await _counts(conn) == before


# ── honest sequences: the daily use ──────────────────────────────────────────


async def test_honest_whole_days_in_sequence_then_a_superset_and_subsets(db) -> None:
    conn, _ = db
    a = await _import(conn, _stmt(_ledger(1, 3)))
    assert (a.history_segments, a.rows_inserted) == (1, 9)
    b = await _import(conn, _stmt(_ledger(4, 6)))
    assert (b.history_segments, b.rows_inserted) == (1, 9)
    for rows in (_ledger(1, 6), _ledger(2, 5), _ledger(3, 4)):  # superset of both, then subsets
        again = await _import(conn, _stmt(rows))
        assert again.nothing_new is True and again.rows_inserted == 0 and again.history_segments == 1
    assert await _counts(conn) == (2, 18)


async def test_honest_a_superset_of_one_stored_export_stores_only_the_new_days(db) -> None:
    conn, _ = db
    await _import(conn, _stmt(_ledger(2, 5)))
    wider = await _import(conn, _stmt(_ledger(1, 6)))
    assert (wider.rows_inserted, wider.rows_duplicate, wider.history_segments) == (6, 12, 1)


async def test_honest_a_partial_last_day_then_a_later_export_that_overlaps_it(db) -> None:
    conn, _ = db
    cut = await _import(conn, _stmt(_ledger(1, 3, upto=at(3, 14))))
    assert cut.rows_inserted == 8  # day 3 stopped after its 12:00 posting
    later = await _import(conn, _stmt(_ledger(3, 5)))
    assert (later.rows_inserted, later.rows_duplicate, later.history_segments) == (7, 2, 1)
    assert await _counts(conn) == (2, 15)


async def test_honest_an_older_month_backfilled_after_a_newer_one(db) -> None:
    conn, _ = db
    await _import(conn, _stmt(_ledger(5, 7)))
    older = await _import(conn, _stmt(_ledger(1, 4)))
    assert older.history_segments == 1 and older.rows_inserted == 12
    joined = await _import(conn, _stmt(_ledger(1, 5)))  # overlaps the newer stored one
    assert joined.history_segments == 1 and joined.nothing_new is True


# ── I9: every refusal is exactly its template, nothing else ─────────────────

_I5A = r"statement disagrees with rows already stored \(\d+ stored row\(s\) in its settled period are not in it\)"
_I5B = r"statement disagrees with rows already stored \(\d+ new row\(s\) fall inside the settled period of a stored statement\)"


async def test_i9_every_i5_refusal_message_is_exactly_its_template(db) -> None:
    import re

    conn, _ = db
    await _stored_everything(conn)
    for variant, build in _DISHONEST.items():
        with pytest.raises(BankImportError) as info:
            await _import(conn, _stmt(build(_full_ledger())))
        msg = str(info.value)
        assert re.fullmatch(_I5A, msg) or re.fullmatch(_I5B, msg), (variant, msg)
    # and each rule alone, by its own template
    zero = (_MID, "MISCELLANEOUS CHARGES", "0.00", "0.00")
    with pytest.raises(BankImportError, match=_I5B):
        await _import(conn, _stmt(_with(_full_ledger(), [zero])))


async def test_i9_the_i5a_message_alone_is_exactly_its_template(db) -> None:
    import re

    conn, _ = db
    await _import(conn, _stmt(_with(_full_ledger(), [(_MID, "MISCELLANEOUS CHARGES", "0.00", "0.00")])))
    with pytest.raises(BankImportError) as info:
        await _import(conn, _stmt(_full_ledger()))
    assert re.fullmatch(_I5A, str(info.value))


async def test_i5a_new_days_do_not_mask_a_stored_row_the_new_statement_omits(db) -> None:
    """A dishonest statement that brings NEW days and also omits a stored row of a settled
    period: the new rows must not be counted as if they were stored rows it contains."""
    conn, _ = db
    zero = (at(3, 10), "MISCELLANEOUS CHARGES", "0.00", "0.00")
    await _import(conn, _stmt(_with(_ledger(1, 5), [zero])))
    before = await _counts(conn)
    with pytest.raises(BankImportError, match=_I5A):
        await _import(conn, _stmt(_ledger(1, 7)))  # days 6-7 are new; the zero row of day 3 is missing
    assert await _counts(conn) == before


# ── I7: history segments, counted without regard to order ───────────────────


async def test_i7_one_statement_is_one_segment_and_the_stored_column_equals_the_returned_value(db) -> None:
    conn, _ = db
    r = await _import(conn, _stmt(_ledger(1, 3)))
    assert r.history_segments == 1
    assert await conn.fetchval("SELECT history_segments FROM bank_statements WHERE id=$1", r.statement_id) == 1


@pytest.mark.parametrize("order", [((1, 3), (4, 6)), ((4, 6), (1, 3))], ids=["oldest_first", "newest_first"])
async def test_i7_two_statements_that_chain_are_one_segment_in_both_import_orders(db, order) -> None:
    conn, _ = db
    for d1, d2 in order:
        result = await _import(conn, _stmt(_ledger(d1, d2)))
    assert result.history_segments == 1


async def test_i7_a_real_hole_is_two_segments_and_the_missing_statement_closes_it(db) -> None:
    conn, _ = db
    await _import(conn, _stmt(_ledger(1, 2)))
    holed = await _import(conn, _stmt(_ledger(5, 6)))  # days 3-4 not imported yet
    assert holed.history_segments == 2
    assert await conn.fetchval("SELECT history_segments FROM bank_statements WHERE id=$1", holed.statement_id) == 2
    closed = await _import(conn, _stmt(_ledger(3, 4)))
    assert closed.history_segments == 1


async def test_i7_three_segments(db) -> None:
    conn, _ = db
    for d in (1, 3, 5):
        result = await _import(conn, _stmt(_ledger(d, d)))
    assert result.history_segments == 3


async def test_i7_a_hole_of_50_between_adjacent_statements_is_seen(db) -> None:
    conn, _ = db
    await _import(conn, _stmt(_ledger(3, 4)))
    assert (await _import(conn, _stmt(_ledger(1, 2), shift="50.00"))).history_segments == 2
    assert (await _import(conn, _stmt(_ledger(5, 6), shift="50.00"))).history_segments == 3


async def test_i7_two_segments_that_open_on_the_same_amount_are_still_two(db) -> None:
    conn, _ = db
    await _import(conn, build_statement([(at(1, 9), "DIRECT DEBIT", "100.00", "0.00")], opening="1000.00"))
    two = await _import(conn, build_statement([(at(5, 9), "DIRECT DEBIT", "10.00", "0.00")], opening="1000.00"))
    assert two.history_segments == 2  # counted by occurrences, not by distinct amounts


async def test_i7_a_statement_ending_on_a_one_second_inversion_followed_by_the_next_is_one_segment(db) -> None:
    conn, _ = db
    base, p1, p2 = _batch_tail()
    await _import(conn, _stmt(base + [p1, p2]))
    from decimal import Decimal as D

    closing = D(OPENING) + sum(D(r[3]) - D(r[2]) for r in base + [p1, p2])
    result = await _import(conn, build_statement([(at(8, 9), "DIRECT DEBIT", "100.00", "0.00")], opening=f"{closing:.2f}"))
    assert result.history_segments == 1


async def test_i7_a_same_second_row_cannot_hide_a_real_hole(db) -> None:
    conn, _ = db
    rows = [
        (at(2, 9), "DIRECT DEBIT", "10.00", "0.00"),
        (at(3, 9), "DIRECT DEBIT", "10.00", "0.00"),
        (at(3, 9), "DIRECT DEBIT", "40.00", "0.00"),  # closing 940.00
    ]
    await _import(conn, build_statement(rows, opening="1000.00"))
    result = await _import(conn, build_statement([(at(5, 9), "DIRECT DEBIT", "1.00", "0.00")], opening="980.00"))
    assert result.history_segments == 2


async def test_i7_zero_amount_rows_and_a_debit_its_reversal_and_the_same_debit_again_change_nothing(db) -> None:
    conn, _ = db
    rows = [
        (at(1, 9), "DIRECT DEBIT", "100.00", "0.00"),
        (at(2, 9), "MISCELLANEOUS CHARGES", "0.00", "0.00"),
        (at(2, 9), "MISCELLANEOUS CHARGES", "0.00", "0.00"),
        (at(2, 10), "DIRECT DEBIT", "5.00", "0.00"),
        (at(2, 10), "REMITTANCE CR - BIFAST", "0.00", "5.00"),
        (at(2, 10), "DIRECT DEBIT", "5.00", "0.00"),
    ]
    first = await _import(conn, build_statement(rows, opening="1000.00"))
    assert first.rows_inserted == 6 and first.history_segments == 1
    nxt = await _import(conn, build_statement([(at(4, 9), "DIRECT DEBIT", "1.00", "0.00")], opening="895.00"))
    assert nxt.history_segments == 1


async def test_i7_a_segment_whose_closing_equals_its_opening_is_still_one(db) -> None:
    conn, _ = db
    rows = [(at(1, 9), "DIRECT DEBIT", "100.00", "0.00"), (at(2, 9), "REMITTANCE CR - BIFAST", "0.00", "100.00")]
    result = await _import(conn, build_statement(rows, opening="1000.00"))
    assert result.history_segments == 1  # the sum is 0: the answer is 1, never 0


async def test_i7_rows_written_under_238_alone_are_ignored() -> None:
    conn, name = await _create_db(with_363=False)
    try:
        sid = await conn.fetchval("INSERT INTO bank_statements (source_format, txn_count) VALUES ('csv', 3) RETURNING id")
        for amount in (10, 20, 30):
            await conn.execute(
                "INSERT INTO bank_transactions (statement_id, txn_date, amount_idr, direction, bank_account_id)"
                " VALUES ($1, '2026-06-25', $2, 'credit', $3)", sid, amount, ACCOUNT,
            )
        await conn.execute(_sql(M363))
        result = await _import(conn, _stmt(_ledger(1, 2)))
        assert result.history_segments == 1  # the three old rows (NULL balances) are not segments
    finally:
        await _drop_db(conn, name)


async def test_i7_nothing_new_returns_the_current_count(db) -> None:
    conn, _ = db
    await _import(conn, _stmt(_ledger(1, 2)))
    await _import(conn, _stmt(_ledger(5, 6)))  # two segments now
    again = await _import(conn, _stmt(_ledger(5, 6)))
    assert again.nothing_new is True and again.history_segments == 2


async def test_i7_stated_limit_a_hole_whose_missing_movements_cancel_out_is_not_seen(db) -> None:
    """Balances cannot show a break whose missing movements net to zero: pinned on purpose."""
    conn, _ = db
    first = _stmt(_ledger(1, 2))
    await _import(conn, first)
    # day 3 (not imported) credited and debited the same amount, so day 4 opens where day 2 closed
    cont = await _import(conn, build_statement(_ledger(4, 5), opening=f"{first.closing_balance:.2f}"))
    assert cont.history_segments == 1  # a real hole, invisible by construction


async def test_i7_another_accounts_history_does_not_count(db) -> None:
    conn, _ = db
    await _import(conn, _stmt(_ledger(1, 2)))
    await _import(conn, _stmt(_ledger(5, 6)))  # two segments on the first account
    other = await _import(conn, build_statement(_ledger(1, 2), opening=OPENING, account="700000000042"))
    assert other.history_segments == 1  # its own, single, chain; the first account's hole is not its


async def test_i7_another_accounts_balance_cannot_cancel_an_opening_of_this_one(db) -> None:
    """Account Y holds a row whose balance equals the opening amount of X's second segment:
    X must still report 2 (the balances CTE must be filtered by account too)."""
    conn, _ = db
    await _import(conn, _stmt(_ledger(1, 2)))
    second = _stmt(_ledger(5, 6))
    await _import(conn, second)
    v = second.opening_balance
    y = build_statement(
        [(at(8, 9), "DIRECT DEBIT", "1.00", "0.00")], opening=f"{v + 1:.2f}", account="700000000042"
    )
    assert y.closing_balance == v
    await _import(conn, y)
    again = await _import(conn, second)  # nothing new for X: returns X's current count
    assert again.nothing_new is True and again.history_segments == 2


async def test_i7_an_amount_that_more_rows_close_on_than_open_on_cannot_cancel_another_segments_break(db) -> None:
    """One segment dips to 90.00 twice and returns to 100.00 once (A(90) = 2 > B(90) = 1); a
    second, separate segment opens at 500.00. Each unbroken run is counted, so the answer is 2:
    a negative term must never offset a positive one."""
    conn, _ = db
    dip = [
        (at(1, 9), "DIRECT DEBIT", "10.00", "0.00"),
        (at(2, 9), "REMITTANCE CR - BIFAST", "0.00", "10.00"),
        (at(3, 9), "DIRECT DEBIT", "10.00", "0.00"),
    ]
    await _import(conn, build_statement(dip, opening="100.00"))
    result = await _import(
        conn, build_statement([(at(6, 9), "DIRECT DEBIT", "100.00", "0.00")], opening="500.00")
    )
    assert result.history_segments == 2


async def test_i5_a_settled_row_that_differs_only_in_description_and_kind_is_the_same_row(db) -> None:
    """Identity is instant, debit, credit and balance. A re-export that labels one settled row
    differently is nothing new, and the stored row keeps what it had."""
    conn, _ = db
    await _import(conn, _stmt(_full_ledger()))
    relabelled = _stmt(_full_ledger())
    target = next(t for t in relabelled.transactions if t.posted_at == at(4, 12))
    stored_before = await conn.fetchrow(
        "SELECT description, kind FROM bank_transactions WHERE posted_at = $1", at(4, 12)
    )
    target.description, target.kind = "relabelled by a later export", "other"
    result = await _import(conn, relabelled)
    assert result.nothing_new is True and result.rows_inserted == 0
    stored_after = await conn.fetchrow(
        "SELECT description, kind FROM bank_transactions WHERE posted_at = $1", at(4, 12)
    )
    assert tuple(stored_after) == tuple(stored_before) != ("relabelled by a later export", "other")
    assert await _counts(conn) == (1, 21)


async def test_i7_stated_limit_a_stretch_that_returns_to_its_starting_amount_is_not_a_segment_of_its_own(db) -> None:
    """Pinned on purpose. A lone zero-amount row far from everything, and a day that opens and
    closes on the same amount, each add a balance before and a balance after that cancel: the
    count (a lower bound) does not grow."""
    conn, _ = db
    await _import(conn, _stmt(_ledger(1, 3)))
    zero = await _import(conn, build_statement([(at(20, 9), "MISCELLANEOUS CHARGES", "0.00", "0.00")], opening="123.00"))
    assert zero.history_segments == 1
    round_trip = [
        (at(25, 9), "DIRECT DEBIT", "100.00", "0.00"),
        (at(25, 10), "REMITTANCE CR - BIFAST", "0.00", "100.00"),
    ]
    day = await _import(conn, build_statement(round_trip, opening="777.00"))
    assert day.history_segments == 1


async def test_i7_never_refuses_a_hole(db) -> None:
    conn, _ = db
    await _import(conn, build_statement(_first_rows(), opening="1000000.00"))
    far = await _import(conn, build_statement([(at(20, 9), "DIRECT DEBIT", "1.00", "0.00")], opening="5.00"))
    assert far.history_segments == 2 and far.statement_id is not None


# ── concurrency: the per-account advisory lock ──────────────────────────────


async def _waiting_on_advisory_locks(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )


async def _wait_until_waiting(conn: asyncpg.Connection, n: int, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await _waiting_on_advisory_locks(conn) >= n:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"expected {n} import(s) waiting on the per-account advisory lock; the lock is not the first thing taken")


async def test_i1_two_exports_that_disagree_imported_together_store_exactly_one(db) -> None:
    """The lock must be the FIRST statement: with it removed nothing waits; with it after the
    first read the second import decides on a stale view and is not refused."""
    conn, name = db
    zero = (at(3, 10), "MISCELLANEOUS CHARGES", "0.00", "0.00")
    x = build_statement(_with(_ledger(1, 5), [zero]), opening=OPENING)
    # y has the same rows WITHOUT the zero-amount one: every row of y is already in x, so only
    # rule (a), read AFTER x committed, can refuse it (a stale read would say "nothing new")
    y = build_statement(_ledger(1, 5), opening=OPENING)
    holder = await asyncpg.connect(_dsn(), database=name)
    c1 = await asyncpg.connect(_dsn(), database=name)
    c2 = await asyncpg.connect(_dsn(), database=name)
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, $2))", ACCOUNT, bank_import_service._LOCK_NAMESPACE
        )
        first = asyncio.create_task(_import(c1, x))
        await _wait_until_waiting(holder, 1)
        second = asyncio.create_task(_import(c2, y))
        await _wait_until_waiting(holder, 2)
        await tx.rollback()
        got = await asyncio.gather(first, second, return_exceptions=True)
    finally:
        await asyncio.gather(holder.close(), c1.close(), c2.close())
    assert got[0].statement_id is not None  # x went first and is stored
    assert isinstance(got[1], BankImportError)  # y, judged AFTER x committed, is refused
    assert await _counts(conn) == (1, len(x.transactions))


async def test_imports_of_different_accounts_do_not_wait_on_each_other(db) -> None:
    conn, name = db
    holder = await asyncpg.connect(_dsn(), database=name)
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, $2))", "999999999999", bank_import_service._LOCK_NAMESPACE
        )
        result = await asyncio.wait_for(
            _import(conn, build_statement(_first_rows(), opening="1000000.00")), timeout=5
        )
        assert result.rows_inserted == 3
        await tx.rollback()
    finally:
        await holder.close()


# ── I10: the migration ─────────────────────────────────────────────────────


async def _dump(conn: asyncpg.Connection) -> list:
    """Every row of both tables, system columns included: an UPDATE that rewrites a row to
    the same values still changes xmin, so a forward SQL that touches rows shows here."""
    out = []
    for table in ("bank_statements", "bank_transactions"):
        out.append([tuple(r.values()) for r in await conn.fetch(f"SELECT xmin::text AS _xmin, * FROM {table} ORDER BY id")])
    return out


async def _catalog(conn: asyncpg.Connection) -> dict:
    cols = await conn.fetch(
        "SELECT table_name, column_name, data_type, is_nullable, column_default, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name LIKE 'bank_%' "
        "ORDER BY 1, 2"
    )
    idx = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename LIKE 'bank_%' ORDER BY 1"
    )
    cons = await conn.fetch(
        "SELECT conrelid::regclass::text AS t, conname, pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE connamespace = 'public'::regnamespace AND conrelid::regclass::text LIKE 'bank_%' ORDER BY 1, 2"
    )
    return {"cols": [tuple(r.values()) for r in cols], "idx": [tuple(r.values()) for r in idx],
            "cons": [tuple(r.values()) for r in cons]}


async def test_i10_applied_twice_changes_nothing_in_either_table_old_style_rows_included() -> None:
    conn, name = await _create_db(with_363=False)
    try:
        sid = await conn.fetchval("INSERT INTO bank_statements (source_format, txn_count) VALUES ('csv', 2) RETURNING id")
        for amount in (4000000, 12):
            await conn.execute(
                "INSERT INTO bank_transactions (statement_id, txn_date, amount_idr, direction, bank_account_id)"
                " VALUES ($1, '2026-06-25', $2, 'credit', 'LEGACY')", sid, amount,
            )
        await conn.execute(_sql(M363))
        await _import(conn, _stmt(_ledger(1, 2)))  # plus rows written by the import itself
        before = await _dump(conn)
        catalog_before = await _catalog(conn)
        await conn.execute(_sql(M363))
        assert await _dump(conn) == before  # same values AND same row versions: nothing was rewritten
        assert await _catalog(conn) == catalog_before
    finally:
        await _drop_db(conn, name)


_OLD_TXN_COLS = (
    "xmin::text AS _xmin, id, statement_id, txn_date, description, amount_idr, direction, "
    "running_balance_idr, bank_account_id, reconciled_status, matched_practice_id, "
    "matched_invoice_id, raw_row::text AS raw_row, created_at"
)
_OLD_STMT_COLS = (
    "xmin::text AS _xmin, id, bank_code, account_label, uploaded_by, uploaded_at, source_filename, "
    "source_format, period_start, period_end, opening_balance_idr, closing_balance_idr, "
    "parse_status, balance_check_ok, txn_count"
)


async def test_i10_the_first_application_leaves_every_old_row_and_old_column_untouched() -> None:
    conn, name = await _create_db(with_363=False)
    try:
        sid = await conn.fetchval(
            "INSERT INTO bank_statements (source_format, txn_count, uploaded_by, source_filename) "
            "VALUES ('csv', 2, 'old@example.test', 'old.csv') RETURNING id"
        )
        for amount, desc in ((4000000, "old one"), (12, "old two")):
            await conn.execute(
                "INSERT INTO bank_transactions (statement_id, txn_date, description, amount_idr, direction,"
                " bank_account_id, raw_row) VALUES ($1, '2026-06-25', $2, $3, 'credit', 'LEGACY', '{\"k\": 1}')",
                sid, desc, amount,
            )
        before = (
            await conn.fetch(f"SELECT {_OLD_TXN_COLS} FROM bank_transactions ORDER BY id"),
            await conn.fetch(f"SELECT {_OLD_STMT_COLS} FROM bank_statements ORDER BY id"),
        )
        await conn.execute(_sql(M363))
        after = (
            await conn.fetch(f"SELECT {_OLD_TXN_COLS} FROM bank_transactions ORDER BY id"),
            await conn.fetch(f"SELECT {_OLD_STMT_COLS} FROM bank_statements ORDER BY id"),
        )
        assert [list(map(tuple, t)) for t in after] == [list(map(tuple, t)) for t in before]  # values AND xmin
    finally:
        await _drop_db(conn, name)


async def test_i10_every_existing_column_and_constraint_is_unchanged_by_the_forward_sql() -> None:
    conn, name = await _create_db(with_363=False)
    try:
        pristine = await _catalog(conn)
        await conn.execute(_sql(M363))
        after = await _catalog(conn)
        existing = {(c[0], c[1]) for c in pristine["cols"]}
        assert [c for c in after["cols"] if (c[0], c[1]) in existing] == pristine["cols"]  # type, null, default
        assert after["cons"] == pristine["cons"]  # no constraint added, dropped or changed
        assert {c[:2] for c in after["cols"]} > existing  # and the forward SQL did add columns
    finally:
        await _drop_db(conn, name)


async def test_i10_rows_written_under_238_alone_stay_valid_after_363() -> None:
    conn, name = await _create_db(with_363=False)
    try:
        sid = await conn.fetchval("INSERT INTO bank_statements (source_format, txn_count) VALUES ('csv', 1) RETURNING id")
        tid = await conn.fetchval(
            "INSERT INTO bank_transactions (statement_id, txn_date, amount_idr, direction, bank_account_id)"
            " VALUES ($1, '2026-06-25', 4000000, 'credit', 'LEGACY') RETURNING id",
            sid,
        )
        await conn.execute(_sql(M363))
        row = await conn.fetchrow("SELECT * FROM bank_transactions WHERE id = $1", tid)
        assert row["amount_idr"] == 4000000 and row["reconciled_status"] == "unmatched"
        assert row["posted_at"] is None and row["debit"] is None and row["dup_ordinal"] == 0
        from backend.services.accounting.cashout_service import list_bank_transactions

        assert [r["id"] for r in await list_bank_transactions(conn)] == [tid]  # the legacy reader query
        await conn.execute("UPDATE bank_transactions SET reconciled_status = 'manual' WHERE id = $1", tid)
    finally:
        await _drop_db(conn, name)


async def test_i10_the_rollback_section_removes_every_column_and_index_it_added() -> None:
    conn, name = await _create_db(with_363=False)
    try:
        pristine = await _catalog(conn)  # 238 + 239 only
        await conn.execute(_sql(M363))
        assert await _catalog(conn) != pristine  # the forward SQL did add things
        await conn.execute(_sql(M363, rollback=True))
        assert await _catalog(conn) == pristine  # and the rollback took every one of them away
        await conn.execute(_sql(M363))  # forward again works
    finally:
        await _drop_db(conn, name)


async def test_i10_forward_sql_stays_inside_the_runner_rules() -> None:
    text = _sql(M363).upper()
    for banned in ("DROP DATABASE", "DROP SCHEMA", "TRUNCATE"):
        assert banned not in text
    assert "SET LOCK_TIMEOUT = '5S'" in text and "SET STATEMENT_TIMEOUT = '60S'" in text
    assert ACCOUNT  # imported for the synthetic builder


# ── the PDF readers store through the same path ─────────────────────────────


@pytest.mark.parametrize("bank", ["mandiri", "permata"])
async def test_pdf_statements_store_their_bank_and_format_and_reimport_is_idempotent(db, bank) -> None:
    from backend.services.accounting.bank_pdf_readers import (
        parse_mandiri_pages,
        parse_permata_pages,
    )
    from backend.tests.services.test_accounting_bank_pdf_readers import P_DAYS, _mandiri, _permata

    conn, _ = db
    stmt = parse_mandiri_pages([_mandiri()]) if bank == "mandiri" else parse_permata_pages([_permata(P_DAYS)])
    first = await _import(conn, stmt, "a.pdf")
    assert first.rows_inserted == len(stmt.transactions) and not first.nothing_new
    row = await conn.fetchrow("SELECT bank_code, source_format, account_number FROM bank_statements")
    assert (row["bank_code"], row["source_format"]) == (f"bank_{bank}", "pdf")
    assert row["account_number"] == stmt.account_number
    again = await _import(conn, stmt, "a.pdf")
    assert again.nothing_new and again.rows_duplicate == len(stmt.transactions)
    assert await _counts(conn) == (1, len(stmt.transactions))


# ── Permata days: balances are derived from the file's row order ─────────────


def _permata_statement(days, closing):
    from backend.services.accounting.bank_pdf_readers import parse_permata_pages
    from backend.tests.fixtures.bank_pdf_builder import permata_page

    return parse_permata_pages([permata_page(days, closing=closing)])


_CR = (1, "03-Dec-2025", "374", "0.00", "100.00", ["ALPHA"])
_DB = (2, "03-Dec-2025", "427", "50.00", "0.00", ["BETA"])
_REVERSAL = (2, "03-Dec-2025", "427", "100.00", "0.00", ["BETA"])
_FEE = (3, "04-Dec-2025", "427", "50.00", "0.00", ["FEE"])


def _day(rows, opening="1,000.00", debit="50.00", credit="100.00"):
    return ("03-Dec-2025", opening, rows, debit, credit)


async def test_permata_the_same_day_listed_in_another_order_is_refused_and_adds_nothing(db) -> None:
    conn, _ = db
    first = _permata_statement([_day([_CR, _DB])], ("31-Dec-2025", "1,050.00"))
    swapped = _permata_statement([_day([_DB, _CR])], ("31-Dec-2025", "1,050.00"))
    await _import(conn, first, "a.pdf")
    with pytest.raises(BankImportError, match="different order"):
        await _import(conn, swapped, "b.pdf")
    assert await _counts(conn) == (1, 2)


async def test_permata_a_partial_day_then_the_full_day_adds_only_the_new_rows(db) -> None:
    conn, _ = db
    partial = _permata_statement([_day([_CR], debit="0.00")], ("03-Dec-2025", "1,100.00"))
    full = _permata_statement([_day([_CR, _DB])], ("03-Dec-2025", "1,050.00"))
    assert (await _import(conn, partial, "a.pdf")).rows_inserted == 1
    result = await _import(conn, full, "b.pdf")
    assert (result.rows_inserted, result.rows_duplicate) == (1, 1)
    assert (await _counts(conn))[1] == 2
    # and the partial file again, after the full one, changes nothing
    assert (await _import(conn, partial, "a.pdf")).nothing_new
    assert (await _counts(conn))[1] == 2


async def test_permata_the_same_file_twice_adds_nothing(db) -> None:
    conn, _ = db
    stmt = _permata_statement([_day([_CR, _DB])], ("03-Dec-2025", "1,050.00"))
    await _import(conn, stmt, "a.pdf")
    assert (await _import(conn, stmt, "a.pdf")).nothing_new
    assert await _counts(conn) == (1, 2)


async def test_permata_two_identical_rows_of_one_day_survive_both_files(db) -> None:
    conn, _ = db
    rows = [_CR, _REVERSAL, (3, "03-Dec-2025", "374", "0.00", "100.00", ["ALPHA"])]
    day = _day(rows, debit="100.00", credit="200.00")
    short = _permata_statement([day], ("03-Dec-2025", "1,100.00"))
    longer = _permata_statement(
        [day, ("04-Dec-2025", "1,100.00", [(4, "04-Dec-2025", "427", "50.00", "0.00", ["FEE"])], "50.00", "0.00")],
        ("04-Dec-2025", "1,050.00"),
    )
    await _import(conn, short, "a.pdf")
    assert (await _counts(conn))[1] == 3
    assert (await _import(conn, longer, "b.pdf")).rows_inserted == 1
    assert (await _import(conn, short, "a.pdf")).nothing_new
    assert (await _counts(conn))[1] == 4
