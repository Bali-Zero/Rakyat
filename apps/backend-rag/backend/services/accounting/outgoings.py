"""Outgoings: the debits of the imported statements still to justify, each with a reading.

A reading is the bank's own text taken apart (payee and purpose of a transfer, the e-billing
code of a PNBP payment, the cheque of a cash withdrawal) and the transfer a fee belongs to. It
is shown and never stored. Each debit also carries its state: `justified` when an active
justification stands (outgoing_justify.py writes it), or, for a transfer fee, when its paired
transfer is justified; otherwise `to_justify`.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

from backend.services.accounting.companies import OTHER_KEY, company_key_for_account
from backend.services.accounting.outgoing_justify import PAIR_LATERAL

# "OCTO Biz TO <payee> IBI<reference> <BIC> <purpose>" — 60 of 60 transfers on 2026-11-21.
_TRANSFER = re.compile(r"^OCTO Biz TO (?P<payee>.+?) IBI\d+\s+[A-Z]{8}(?:\s+(?P<purpose>.*?))?\s*$")
# "EMPN BILLING  <code>" — 245 of 245 PNBP payments.
_BILLING = re.compile(r"^EMPN BILLING\s+(?P<code>\d+)")
# "BDS  CLEARING <branch> <7-digit cheque><who>" — the name is missing on some.
_WITHDRAWAL = re.compile(
    r"^BDS\s+CLEARING\s+(?P<branch>.+?)\s+(?P<cheque>\d{7})(?P<who>\D.*?)?\s*$"
)

READING = {
    "government_fee": "PNBP",
    "transfer_out": "Transfer",
    "transfer_fee": "Transfer fee",
    "cash_withdrawal": "Cash withdrawal",
    "payroll": "Payroll",
    "bank_fee": "Bank charges",
    "tax": "Tax",
    "other": "Other",
}


def read_outgoing(kind: str, description: str, payment_type: str | None) -> dict[str, Any]:
    """What the bank's text says about one debit: a label and the parts worth showing."""
    text = (description or "").strip()
    out: dict[str, Any] = {
        "label": READING.get(kind, "Other"),
        "detail": text or payment_type or "",
    }
    if kind == "government_fee" and (m := _BILLING.match(text)):
        out["billing_code"] = m["code"]
        out["detail"] = f"e-billing {m['code']}"
    elif kind == "transfer_out" and (m := _TRANSFER.match(text)):
        out["payee"] = m["payee"].strip()
        out["purpose"] = (m["purpose"] or "").strip()
        out["detail"] = out["purpose"] or "no purpose written"
    elif kind == "cash_withdrawal" and (m := _WITHDRAWAL.match(text)):
        out["cheque"] = m["cheque"]
        out["payee"] = (m["who"] or "").strip() or None
        out["detail"] = f"{m['branch'].strip()}, cheque {m['cheque']}"
    elif kind == "other" and "BPJS" in text.upper():
        out["label"] = "BPJS"
    return out


_STATES = ("to_justify", "justified")

# Every unmatched debit with its pair and its justification; the state is derived here once so
# the list and the totals cannot disagree.
_BASE = f"""
    SELECT bt.id, bt.txn_date, bt.posted_at, bt.amount_idr, bt.kind, bt.description,
           bt.raw_row->>'Payment Type' AS payment_type, bs.account_number,
           CASE WHEN pair.n = 1 THEN pair.id END AS fee_of_txn_id,
           CASE WHEN pair.n = 1 THEN pair.description END AS fee_of_description,
           COALESCE(j.id, pj.id) AS justification_id,
           CASE WHEN pj.id IS NOT NULL AND j.id IS NULL THEN pair.id END AS follows_txn_id,
           (COALESCE(j.id, pj.id) IS NOT NULL) AS is_justified
    FROM bank_transactions bt
    LEFT JOIN bank_statements bs ON bs.id = bt.statement_id
    {PAIR_LATERAL}
    LEFT JOIN outgoing_justifications j ON j.txn_id = bt.id AND j.undone_at IS NULL
    LEFT JOIN outgoing_justifications pj
           ON pair.n = 1 AND pj.txn_id = pair.id AND pj.undone_at IS NULL
    WHERE bt.direction = 'debit' AND bt.reconciled_status = 'unmatched'
"""


async def load_outgoings(
    conn: asyncpg.Connection, *, limit: int = 1000, state: str | None = None
) -> dict[str, Any]:
    """Debits newest first, each with its reading and state, and the totals per kind.

    `state` filters the list (`to_justify` or `justified`); none lists both. `by_kind`, `n` and
    `total_idr` always count what is still to justify; `n_justified` and `justified_total_idr`
    count the rest.

    A transfer fee is paired with the transfer of the same statement that carries the same bank
    "Other Reference" (the bank posts both under one; 60 of 60 unique on 2026-11-21); the fee
    then carries that transfer's id and payee, and is justified when that transfer is. A fee
    with no such transfer, or with two, stays unpaired. The pair is looked for among ALL rows
    of the statement, not only the listed ones.
    """
    if state is not None and state not in _STATES:
        raise ValueError(f"state must be one of {_STATES}")
    rows = await conn.fetch(
        f"""
        SELECT * FROM ({_BASE}) b
        WHERE $2::text IS NULL OR is_justified = ($2::text = 'justified')
        ORDER BY txn_date DESC, posted_at DESC NULLS LAST, id DESC
        LIMIT $1
        """,
        limit,
        state,
    )
    totals = await conn.fetch(
        f"""
        SELECT COALESCE(kind, 'other') AS kind, is_justified, count(*) AS n,
               COALESCE(sum(amount_idr), 0) AS total
        FROM ({_BASE}) b
        GROUP BY 1, 2
        """
    )
    justified = await _justifications(conn, [r["justification_id"] for r in rows])
    items: list[dict[str, Any]] = []
    for r in rows:
        kind = r["kind"] or "other"
        reading = read_outgoing(kind, r["description"] or "", r["payment_type"])
        if r["fee_of_txn_id"] is not None:
            payee = read_outgoing("transfer_out", r["fee_of_description"] or "", None).get("payee")
            reading["payee"] = payee
            reading["detail"] = f"fee of the transfer to {payee}"
        justification = justified.get(r["justification_id"])
        if justification is not None and r["follows_txn_id"] is not None:
            justification = {**justification, "follows_txn_id": r["follows_txn_id"]}
        items.append(
            {
                "id": r["id"],
                "txn_date": r["txn_date"],
                "posted_at": r["posted_at"],
                "amount_idr": int(r["amount_idr"]),
                "kind": kind,
                "description": r["description"] or "",
                "company_key": company_key_for_account(r["account_number"]) or OTHER_KEY,
                "reading": reading,
                "fee_of_txn_id": r["fee_of_txn_id"],
                "state": "justified" if r["is_justified"] else "to_justify",
                "justification": justification,
            }
        )
    open_totals: dict[str, dict[str, Any]] = {}
    done_n = done_total = 0
    for t in totals:
        if t["is_justified"]:
            done_n += t["n"]
            done_total += int(t["total"])
        else:
            slot = open_totals.setdefault(
                t["kind"],
                {"kind": t["kind"], "label": READING.get(t["kind"], "Other"), "n": 0, "total_idr": 0},
            )
            slot["n"] += t["n"]
            slot["total_idr"] += int(t["total"])
    by_kind = sorted(open_totals.values(), key=lambda k: (-k["total_idr"], k["kind"]))
    return {
        "items": items,
        "by_kind": by_kind,
        "n": sum(k["n"] for k in by_kind),
        "total_idr": sum(k["total_idr"] for k in by_kind),
        "n_justified": done_n,
        "justified_total_idr": done_total,
    }


async def _justifications(
    conn: asyncpg.Connection, ids: list[int | None]
) -> dict[int, dict[str, Any]]:
    """The justifications behind the listed debits, with their breakdown lines."""
    wanted = sorted({i for i in ids if i is not None})
    if not wanted:
        return {}
    heads = await conn.fetch(
        "SELECT id, category, practice_id, recipient, note, justified_by, justified_at"
        " FROM outgoing_justifications WHERE id = ANY($1::bigint[])",
        wanted,
    )
    line_rows = await conn.fetch(
        "SELECT justification_id, amount_idr, category, note, practice_id"
        " FROM outgoing_justification_lines WHERE justification_id = ANY($1::bigint[]) ORDER BY id",
        wanted,
    )
    out = {
        h["id"]: {
            "category": h["category"],
            "practice_id": h["practice_id"],
            "recipient": h["recipient"],
            "note": h["note"],
            "lines": [],
            "by": h["justified_by"],
            "at": h["justified_at"],
        }
        for h in heads
    }
    for ln in line_rows:
        out[ln["justification_id"]]["lines"].append(
            {
                "amount_idr": int(ln["amount_idr"]),
                "category": ln["category"],
                "note": ln["note"],
                "practice_id": ln["practice_id"],
            }
        )
    return out
