"""Real-PostgreSQL tests of the companies strip: which statements belong to which issuing company,
how far each company's bank data reaches, and how many of its incoming transfers wait.

Account numbers come from the issuing-entity registry itself; amounts and dates are invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.services.accounting.companies import OTHER_KEY, load_companies
from backend.services.invoicing.entities import ENTITIES
from backend.tests.services.test_accounting_bank_import_integration import _create_db, _drop_db

KEYS = list(ENTITIES)


@pytest.fixture
async def db():
    conn, name = await _create_db()
    try:
        yield conn
    finally:
        await _drop_db(conn, name)


async def _statement(conn, account: str | None, period_end: date | None) -> int:
    return await conn.fetchval(
        "INSERT INTO bank_statements (source_format, account_number, period_end)"
        " VALUES ('csv', $1, $2) RETURNING id",
        account,
        period_end,
    )


_amounts = iter(range(1_000_000, 2_000_000, 1_000))


async def _txn(conn, sid: int, direction: str = "credit", status: str = "unmatched") -> None:
    await conn.execute(
        "INSERT INTO bank_transactions"
        " (statement_id, txn_date, amount_idr, direction, reconciled_status, bank_account_id)"
        " VALUES ($1, '2026-11-03', $2, $3, $4, 'ACC')",
        sid,
        next(_amounts),
        direction,
        status,
    )


def _by_key(rows: list[dict]) -> dict[str, dict]:
    return {r["key"]: r for r in rows}


async def test_every_company_is_listed_even_with_no_statement(db) -> None:
    rows = await load_companies(db)
    assert [r["key"] for r in rows] == KEYS
    for r in rows:
        entity = ENTITIES[r["key"]]
        assert r["company_name"] == entity.company_name
        assert r["bank_name"] == entity.bank_name
        assert r["account_number"] == entity.bank_account_number
        assert (r["data_through"], r["statements"], r["to_reconcile"]) == (None, 0, 0)


async def test_statements_count_for_their_company_by_account_digits(db) -> None:
    first, second = ENTITIES[KEYS[0]], ENTITIES[KEYS[1]]
    s1 = await _statement(db, f"{first.bank_account_number}-IDR", date(2026, 10, 31))
    s2 = await _statement(db, f"{first.bank_account_number}-IDR", date(2026, 11, 17))
    digits = "".join(c for c in second.bank_account_number if c.isdigit())
    s3 = await _statement(db, digits, date(2026, 11, 5))
    await _txn(db, s1)  # waits
    await _txn(db, s1, status="manual")  # linked: not waiting
    await _txn(db, s2)  # waits
    await _txn(db, s2, direction="debit")  # money out: not a transfer to reconcile
    await _txn(db, s3, status="matched")
    got = _by_key(await load_companies(db))
    assert got[first.key]["data_through"] == "2026-11-17"  # the later of its two statements
    assert (got[first.key]["statements"], got[first.key]["to_reconcile"]) == (2, 2)
    assert got[second.key]["data_through"] == "2026-11-05"
    assert (got[second.key]["statements"], got[second.key]["to_reconcile"]) == (1, 0)
    for key in KEYS[2:]:
        assert (got[key]["statements"], got[key]["to_reconcile"]) == (0, 0)
    assert OTHER_KEY not in got


async def test_a_statement_of_an_unknown_account_is_reported_as_other(db) -> None:
    sid = await _statement(db, "999000111", date(2026, 11, 2))
    await _txn(db, sid)
    await _txn(db, sid)
    rows = await load_companies(db)
    assert [r["key"] for r in rows] == [*KEYS, OTHER_KEY]
    other = rows[-1]
    assert (other["data_through"], other["statements"], other["to_reconcile"]) == (
        "2026-11-02",
        1,
        2,
    )
    assert other["company_name"] == "Other accounts"


async def test_a_longer_number_holding_a_companys_digits_is_another_account(db) -> None:
    first = ENTITIES[KEYS[0]]
    digits = "".join(c for c in first.bank_account_number if c.isdigit())
    for account in (f"9{digits}", f"{digits}9", digits[1:], digits[:-1]):
        sid = await _statement(db, account, date(2026, 11, 2))
        await _txn(db, sid)
    got = _by_key(await load_companies(db))
    assert (got[first.key]["statements"], got[first.key]["to_reconcile"]) == (0, 0)
    assert (got[OTHER_KEY]["statements"], got[OTHER_KEY]["to_reconcile"]) == (4, 4)


async def test_a_statement_without_a_period_end_counts_but_dates_nothing(db) -> None:
    first = ENTITIES[KEYS[0]]
    sid = await _statement(db, first.bank_account_number, None)
    await _txn(db, sid)
    got = _by_key(await load_companies(db))[first.key]
    assert (got["data_through"], got["statements"], got["to_reconcile"]) == (None, 1, 1)
