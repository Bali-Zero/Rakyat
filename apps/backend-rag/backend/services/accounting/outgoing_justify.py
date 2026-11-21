"""Justify a bank debit, and take the justification back the same day.

Append-only: a justification is undone, never edited or deleted, and a later one justifies the
debit again. Nothing here writes to `weekly_cashout` or any cash-out table (owner 2026-11-22).
"""

from __future__ import annotations

from typing import Any

import asyncpg

from backend.services.accounting.companies import OTHER_KEY, company_key_for_account
from backend.services.accounting.reconcile_service import _UNDO_TZ

CATEGORIES = (
    "pnbp_case",
    "vendor_case",
    "staff_reimbursement",
    "payroll",
    "cash_withdrawal",
    "bank_charges",
    "tax",
    "own_transfer",
    "other",
)
LINE_CATEGORIES = tuple(c for c in CATEGORIES if c != "cash_withdrawal")

# The transfer a fee was charged for: same statement, same bank "Other Reference", exactly one.
PAIR_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT count(*) AS n, min(t.id) AS id, min(t.description) AS description
        FROM bank_transactions t
        WHERE bt.kind = 'transfer_fee'
          AND t.kind = 'transfer_out'
          AND t.statement_id = bt.statement_id
          AND NULLIF(btrim(t.raw_row->>'Other Reference'), '')
              = NULLIF(btrim(bt.raw_row->>'Other Reference'), '')
    ) pair ON true
"""


class OutgoingError(Exception):
    """A refusal with its HTTP status (404, 409 or 422) and a fixed, value-free text."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


async def justify_outgoing(
    conn: asyncpg.Connection,
    *,
    txn_id: int,
    category: str,
    practice_id: int | None,
    recipient: str | None,
    note: str | None,
    lines: list[dict[str, Any]] | None,
    justified_by: str,
) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise OutgoingError(422, f"category must be one of: {', '.join(CATEGORIES)}")
    recipient, note = _clean(recipient), _clean(note)
    lines = lines or []
    if category != "cash_withdrawal" and lines:
        raise OutgoingError(422, "lines are only for a cash withdrawal")
    if category == "other" and not note:
        raise OutgoingError(422, "category other needs a note")
    if category == "cash_withdrawal" and not (recipient and note and lines):
        raise OutgoingError(
            422, "a cash withdrawal needs a recipient, a note and at least one line"
        )
    for line in lines:
        if line["category"] not in LINE_CATEGORIES:
            raise OutgoingError(
                422, f"a line category must be one of: {', '.join(LINE_CATEGORIES)}"
            )
        if not _clean(line.get("note")):
            raise OutgoingError(422, "every line needs a note")
        if int(line["amount_idr"]) <= 0:
            raise OutgoingError(422, "every line amount must be above zero")
    if conn.is_in_transaction():
        raise OutgoingError(500, "justify runs in a transaction of its own")
    async with conn.transaction():
        bt = await conn.fetchrow(
            f"""
            SELECT bt.id, bt.amount_idr, bt.direction, bt.kind, bs.account_number,
                   CASE WHEN pair.n = 1 THEN pair.id END AS fee_of_txn_id
            FROM bank_transactions bt
            LEFT JOIN bank_statements bs ON bs.id = bt.statement_id
            {PAIR_LATERAL}
            WHERE bt.id = $1
            FOR UPDATE OF bt
            """,
            txn_id,
        )
        if bt is None or bt["direction"] != "debit":
            raise OutgoingError(404, "no such debit")
        if bt["kind"] == "cash_withdrawal" and category != "cash_withdrawal":
            raise OutgoingError(
                422,
                "a cash withdrawal is justified as a cash withdrawal: recipient, note and lines",
            )
        if bt["kind"] == "transfer_fee" and bt["fee_of_txn_id"] is not None:
            raise OutgoingError(409, "a transfer fee follows its transfer: justify the transfer")
        if await conn.fetchval(
            "SELECT 1 FROM outgoing_justifications WHERE txn_id = $1 AND undone_at IS NULL", txn_id
        ):
            raise OutgoingError(409, "this debit is already justified: undo it first")
        if category == "cash_withdrawal":
            gap = int(bt["amount_idr"]) - sum(int(line["amount_idr"]) for line in lines)
            if gap:
                raise OutgoingError(
                    422,
                    f"the lines must sum to the withdrawal: {abs(gap)} IDR "
                    f"{'missing' if gap > 0 else 'too much'}",
                )
        practice_ids = {p for p in [practice_id, *(line.get("practice_id") for line in lines)] if p}
        if practice_ids:
            found = await conn.fetchval(
                "SELECT count(*) FROM practices WHERE id = ANY($1::int[])", list(practice_ids)
            )
            if found != len(practice_ids):
                raise OutgoingError(422, "a case id does not exist")
        jid = await conn.fetchval(
            """
            INSERT INTO outgoing_justifications
                (txn_id, company_key, category, practice_id, recipient, note, justified_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
            """,
            txn_id,
            company_key_for_account(bt["account_number"]) or OTHER_KEY,
            category,
            practice_id,
            recipient,
            note,
            justified_by,
        )
        for line in lines:
            await conn.execute(
                "INSERT INTO outgoing_justification_lines"
                " (justification_id, amount_idr, category, note, practice_id)"
                " VALUES ($1, $2, $3, $4, $5)",
                jid,
                int(line["amount_idr"]),
                line["category"],
                _clean(line["note"]),
                line.get("practice_id"),
            )
    return {"txn_id": txn_id, "justification_id": jid, "state": "justified"}


async def undo_justification(
    conn: asyncpg.Connection, *, txn_id: int, undone_by: str
) -> dict[str, Any]:
    """Take back the active justification, only on the day (WITA) it was made."""
    if conn.is_in_transaction():
        raise OutgoingError(500, "undo runs in a transaction of its own")
    async with conn.transaction():
        if (
            await conn.fetchval("SELECT id FROM bank_transactions WHERE id = $1 FOR UPDATE", txn_id)
            is None
        ):
            raise OutgoingError(404, "no such debit")
        row = await conn.fetchrow(
            "SELECT id, (justified_at AT TIME ZONE $2)::date = (now() AT TIME ZONE $2)::date"
            " AS today FROM outgoing_justifications WHERE txn_id = $1 AND undone_at IS NULL",
            txn_id,
            _UNDO_TZ,
        )
        if row is None:
            raise OutgoingError(409, "this debit has no justification to undo")
        if not row["today"]:
            raise OutgoingError(409, "justified before today: it cannot be undone")
        await conn.execute(
            "UPDATE outgoing_justifications SET undone_by = $2, undone_at = now() WHERE id = $1",
            row["id"],
            undone_by,
        )
    return {"txn_id": txn_id, "justification_id": row["id"], "state": "to_justify"}
