"""Store a parsed bank statement (spec SPEC-bank-statement-import, guarantees I1 to I10).

`import_statement` writes one parsed CIMB statement to `bank_statements` and
`bank_transactions` (migration 238, extended by 363) in ONE transaction: the statement
row plus every transaction not yet stored, or nothing. A transaction's identity is its
account number, instant, debit, credit, balance and its ordinal among identical rows of
the statement; a unique index enforces it, and one INSERT ... ON CONFLICT DO NOTHING
RETURNING makes the inserted count real.

This module never logs and never puts content of the statement in a message: refusals
are fixed text and counts (I9).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from backend.services.accounting.bank_parser import (
    TIMESTAMP_JITTER,
    ParsedStatement,
    check_running_balance,
)

_SOURCE_FILENAME_MAX = 255
_LOCK_NAMESPACE = 363  # second key of the per-account advisory lock


class BankImportError(Exception):
    """The statement is refused and nothing was stored. The message is fixed text and counts."""


class _NothingNew(Exception):
    """Internal signal: every row is already stored. Rolls the transaction back cleanly."""


@dataclass(frozen=True)
class ImportResult:
    statement_id: int | None
    rows_inserted: int
    rows_duplicate: int
    history_segments: int  # unbroken balance chains in the account's stored history (I7)
    nothing_new: bool


def _round_idr(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


async def _history_segments(conn: Any, account: str) -> int:
    """Spec I7: how many unbroken balance chains the account's stored history holds, counted
    without regard to order. For each amount v: B(v) = rows whose balance before is v, A(v) =
    rows whose balance is v; the answer is the sum of max(0, B(v) - A(v)), or 1 if that is 0."""
    return await conn.fetchval(
        """
        WITH b AS (
            SELECT balance - credit + debit AS v, count(*) AS n
            FROM bank_transactions
            WHERE bank_account_id = $1 AND posted_at IS NOT NULL
            GROUP BY 1),
        a AS (
            SELECT balance AS v, count(*) AS n
            FROM bank_transactions
            WHERE bank_account_id = $1 AND posted_at IS NOT NULL
            GROUP BY 1)
        SELECT greatest(coalesce(sum(greatest(b.n - coalesce(a.n, 0), 0)), 0), 1)::int
        FROM b LEFT JOIN a ON a.v = b.v
        """,
        account,
    )


async def _finalize_statement(
    conn: Any, statement_id: int, inserted: int, duplicate: int, segments: int
) -> None:
    await conn.execute(
        "UPDATE bank_statements SET rows_inserted = $2, rows_duplicate = $3, history_segments = $4 "
        "WHERE id = $1",
        statement_id, inserted, duplicate, segments,
    )


async def _refuse_reordered_days(
    conn: Any, account: str, txns: list[Any], ordinals: list[int]
) -> None:
    """For a statement whose balances are derived from the rows before them (Permata), the same
    movements listed in another order get other balances, so they would be stored a second time.
    On every day of the statement that the account already stores, the stored rows and the
    statement's rows of that day must agree: one set contains the other (a partial day before the
    full day, or the full day before a partial one). Anything else is refused, whole."""
    mine: dict[Any, set[tuple[Any, ...]]] = {}
    for t, n in zip(txns, ordinals, strict=True):
        mine.setdefault(t.posted_at.date(), set()).add((t.posted_at, t.debit, t.credit, t.balance, n))
    stored_rows = await conn.fetch(
        "SELECT posted_at, debit, credit, balance, dup_ordinal FROM bank_transactions "
        "WHERE bank_account_id = $1 AND posted_at IS NOT NULL AND posted_at::date = ANY($2::date[])",
        account, list(mine),
    )
    stored: dict[Any, set[tuple[Any, ...]]] = {}
    for r in stored_rows:
        stored.setdefault(r["posted_at"].date(), set()).add(
            (r["posted_at"], r["debit"], r["credit"], r["balance"], r["dup_ordinal"])
        )
    for day, rows in stored.items():
        if not (rows <= mine[day] or mine[day] <= rows):
            raise BankImportError(
                "this statement lists a day's transactions in a different order than the "
                "statement already stored; nothing was imported"
            )


async def import_statement(
    conn: Any,
    statement: ParsedStatement,
    *,
    uploaded_by: str,
    source_filename: str | None,
) -> ImportResult:
    """Store `statement`, or refuse it with `BankImportError`; see the module docstring."""
    txns = statement.transactions
    account = statement.account_number
    if not txns:  # I2: also keeps min()/max() below from raising on an empty sequence
        raise BankImportError("statement has no transactions")
    if any(t.posted_at is None or t.balance is None or t.debit is None or t.credit is None for t in txns):
        # I2: a statement that did not come from the CIMB parser has no exact values to store
        raise BankImportError("statement rows lack an exact timestamp or exact amounts")
    mismatches = check_running_balance(txns)
    if mismatches:  # I2
        raise BankImportError(
            f"statement failed its running-balance check ({len(mismatches)} row(s) out of place)"
        )
    if not account:  # I2
        raise BankImportError("statement has no account number")

    # Ordinals: identical rows of this statement (a zero amount, or a debit, its reversal and the
    # same debit again within one second) are numbered oldest first, so all are kept.
    seen: dict[tuple[Any, ...], int] = {}
    ordinals: list[int] = []
    for t in txns:
        key = (t.posted_at, t.debit, t.credit, t.balance)
        ordinals.append(seen.get(key, 0))
        seen[key] = ordinals[-1] + 1

    stamps = [t.posted_at for t in txns if t.posted_at is not None]  # all of them, by the guard
    first_at, last_at = min(stamps), max(stamps)
    opening, closing = statement.opening_balance, statement.closing_balance
    kept = statement.kept_columns
    filename = None if source_filename is None else source_filename[:_SOURCE_FILENAME_MAX]

    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, $2))", account, _LOCK_NAMESPACE
            )
            if statement.balances_derived:
                await _refuse_reordered_days(conn, account, txns, ordinals)
            # I5 (a), part 1: what the account already stores inside the new statement's
            # SETTLED period (up to, not including, its last instant minus the jitter)
            settled_end = last_at - TIMESTAMP_JITTER
            stored_settled = await conn.fetchval(
                "SELECT count(*) FROM bank_transactions "
                "WHERE bank_account_id = $1 AND posted_at >= $2 AND posted_at < $3",
                account, first_at, settled_end,
            )
            statement_id = await conn.fetchval(
                """
                INSERT INTO bank_statements
                    (bank_code, source_format, parse_status, balance_check_ok, uploaded_by,
                     source_filename, period_start, period_end, opening_balance_idr,
                     closing_balance_idr, txn_count, account_number, account_name,
                     period_first_at, period_last_at, opening_balance, closing_balance,
                     order_decided_by)
                VALUES ($15, $16, 'parsed', TRUE, $1,
                        $2, $3, $4, $5,
                        $6, $7, $8, $9,
                        $10, $11, $12, $13,
                        $14)
                RETURNING id
                """,
                uploaded_by, filename, first_at.date(), last_at.date(), _round_idr(opening),
                _round_idr(closing), len(txns), account, statement.account_name,
                first_at, last_at, opening, closing,
                statement.order_decided_by, statement.bank_code, statement.source_format,
            )
            inserted_rows = await conn.fetch(
                """
                INSERT INTO bank_transactions
                    (statement_id, txn_date, description, amount_idr, direction,
                     running_balance_idr, bank_account_id, raw_row, posted_at, debit, credit,
                     balance, kind, dup_ordinal)
                SELECT $1, t.txn_date, t.description, t.amount_idr, t.direction, t.rb, $2,
                       t.raw_row::jsonb, t.posted_at, t.debit, t.credit, t.balance, t.kind,
                       t.dup_ordinal
                FROM unnest($3::date[], $4::text[], $5::bigint[], $6::text[], $7::bigint[],
                            $8::text[], $9::timestamp[], $10::numeric[], $11::numeric[],
                            $12::numeric[], $13::text[], $14::int[])
                     AS t(txn_date, description, amount_idr, direction, rb, raw_row, posted_at,
                          debit, credit, balance, kind, dup_ordinal)
                ON CONFLICT (bank_account_id, posted_at, debit, credit, balance, dup_ordinal)
                    WHERE posted_at IS NOT NULL DO NOTHING
                RETURNING id, posted_at
                """,
                statement_id, account,
                [t.txn_date for t in txns],
                [t.description for t in txns],
                [t.amount_idr for t in txns],
                [t.direction for t in txns],
                [t.running_balance_idr for t in txns],
                [json.dumps({k: t.raw_row.get(k, "") for k in kept}) for t in txns],
                [t.posted_at for t in txns],
                [t.debit for t in txns],
                [t.credit for t in txns],
                [t.balance for t in txns],
                [t.kind for t in txns],
                ordinals,
            )
            inserted = len(inserted_rows)
            duplicate = len(txns) - inserted
            # I5 (a): a stored row of the new statement's settled period that is not among its rows
            rows_settled = sum(1 for t in txns if first_at <= t.posted_at < settled_end)  # type: ignore[operator]
            inserted_settled = sum(1 for r in inserted_rows if first_at <= r["posted_at"] < settled_end)
            missing = stored_settled - (rows_settled - inserted_settled)
            if missing > 0:
                raise BankImportError(
                    "statement disagrees with rows already stored "
                    f"({missing} stored row(s) in its settled period are not in it)"
                )
            # I5 (b): a row not yet stored inside the settled period of an already stored statement
            added = await conn.fetchval(
                """
                SELECT count(*) FROM unnest($1::timestamp[]) AS p(at)
                WHERE EXISTS (
                    SELECT 1 FROM bank_statements s
                    WHERE s.account_number = $2 AND s.id <> $3
                      AND p.at >= s.period_first_at AND p.at < s.period_last_at - $4::interval)
                """,
                [r["posted_at"] for r in inserted_rows], account, statement_id, TIMESTAMP_JITTER,
            )
            if added:
                raise BankImportError(
                    "statement disagrees with rows already stored "
                    f"({added} new row(s) fall inside the settled period of a stored statement)"
                )
            # I7: describes the history as it will be (also on the nothing-new path, where it
            # is simply the current count)
            segments = await _history_segments(conn, account)
            if inserted == 0:  # I4
                raise _NothingNew
            await _finalize_statement(conn, statement_id, inserted, duplicate, segments)
    except _NothingNew:
        return ImportResult(None, 0, len(txns), segments, True)
    return ImportResult(statement_id, inserted, duplicate, segments, False)
