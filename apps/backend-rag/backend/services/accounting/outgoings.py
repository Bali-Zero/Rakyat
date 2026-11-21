"""Outgoings: the debits of the imported statements still to justify, each with a reading.

Read only. A reading is the bank's own text taken apart (payee and purpose of a transfer, the
e-billing code of a PNBP payment, the cheque of a cash withdrawal) and the transfer a fee
belongs to. It is shown to the accountant and never stored: justifying an outgoing is a later
step with its own writer.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

from backend.services.accounting.companies import OTHER_KEY, company_key_for_account

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


async def load_outgoings(conn: asyncpg.Connection, *, limit: int = 1000) -> dict[str, Any]:
    """Unjustified debits newest first, with their reading, and the totals per kind.

    A transfer fee is paired with the transfer of the same statement that carries the same bank
    "Other Reference" (the bank posts both under one; 60 of 60 unique on 2026-11-21); the fee
    then carries that transfer's id and payee. A fee with no such transfer, or with two, stays
    unpaired. The transfer may be listed or not (justified, or beyond the limit).
    """
    # The pair is looked for among ALL rows of the statement, not only the listed ones: a
    # reference shared by two transfers stays unpaired even when the limit hides one of them.
    rows = await conn.fetch(
        """
        SELECT bt.id, bt.txn_date, bt.posted_at, bt.amount_idr, bt.kind, bt.description,
               bt.raw_row->>'Payment Type' AS payment_type, bs.account_number,
               CASE WHEN pair.n = 1 THEN pair.id END AS fee_of_txn_id,
               CASE WHEN pair.n = 1 THEN pair.description END AS fee_of_description
        FROM bank_transactions bt
        LEFT JOIN bank_statements bs ON bs.id = bt.statement_id
        LEFT JOIN LATERAL (
            SELECT count(*) AS n, min(t.id) AS id, min(t.description) AS description
            FROM bank_transactions t
            WHERE bt.kind = 'transfer_fee'
              AND t.kind = 'transfer_out'
              AND t.statement_id = bt.statement_id
              AND NULLIF(btrim(t.raw_row->>'Other Reference'), '')
                  = NULLIF(btrim(bt.raw_row->>'Other Reference'), '')
        ) pair ON true
        WHERE bt.direction = 'debit' AND bt.reconciled_status = 'unmatched'
        ORDER BY bt.txn_date DESC, bt.posted_at DESC NULLS LAST, bt.id DESC
        LIMIT $1
        """,
        limit,
    )
    totals = await conn.fetch(
        """
        SELECT COALESCE(kind, 'other') AS kind, count(*) AS n, COALESCE(sum(amount_idr), 0) AS total
        FROM bank_transactions
        WHERE direction = 'debit' AND reconciled_status = 'unmatched'
        GROUP BY 1
        ORDER BY 3 DESC, 1
        """
    )
    items: list[dict[str, Any]] = []
    for r in rows:
        kind = r["kind"] or "other"
        reading = read_outgoing(kind, r["description"] or "", r["payment_type"])
        if r["fee_of_txn_id"] is not None:
            payee = read_outgoing("transfer_out", r["fee_of_description"] or "", None).get("payee")
            reading["payee"] = payee
            reading["detail"] = f"fee of the transfer to {payee}"
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
            }
        )
    by_kind = [
        {
            "kind": t["kind"],
            "label": READING.get(t["kind"], "Other"),
            "n": t["n"],
            "total_idr": int(t["total"]),
        }
        for t in totals
    ]
    return {
        "items": items,
        "by_kind": by_kind,
        "n": sum(k["n"] for k in by_kind),
        "total_idr": sum(k["total_idr"] for k in by_kind),
    }
