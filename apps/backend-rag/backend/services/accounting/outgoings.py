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
    then carries that transfer's id and payee. A fee with no such transfer stays unpaired.
    """
    rows = await conn.fetch(
        """
        SELECT bt.id, bt.statement_id, bt.txn_date, bt.posted_at, bt.amount_idr, bt.kind,
               bt.description, bt.raw_row->>'Payment Type' AS payment_type,
               NULLIF(btrim(bt.raw_row->>'Other Reference'), '') AS reference, bs.account_number
        FROM bank_transactions bt
        LEFT JOIN bank_statements bs ON bs.id = bt.statement_id
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
    by_reference: dict[tuple[Any, str], dict[str, list[dict[str, Any]]]] = {}
    for r in rows:
        kind = r["kind"] or "other"
        item = {
            "id": r["id"],
            "txn_date": r["txn_date"],
            "posted_at": r["posted_at"],
            "amount_idr": int(r["amount_idr"]),
            "kind": kind,
            "description": r["description"] or "",
            "company_key": company_key_for_account(r["account_number"]) or OTHER_KEY,
            "reading": read_outgoing(kind, r["description"] or "", r["payment_type"]),
            "fee_of_txn_id": None,
        }
        if kind in ("transfer_out", "transfer_fee") and r["reference"] is not None:
            group = by_reference.setdefault((r["statement_id"], r["reference"]), {})
            group.setdefault(kind, []).append(item)
        items.append(item)
    for group in by_reference.values():
        transfers = group.get("transfer_out", [])
        if len(transfers) != 1:
            continue  # no transfer, or more than one: the fee is not ours to attribute
        transfer = transfers[0]
        for fee in group.get("transfer_fee", []):
            payee = transfer["reading"].get("payee")
            fee["fee_of_txn_id"] = transfer["id"]
            fee["reading"]["payee"] = payee
            fee["reading"]["detail"] = f"fee of the transfer to {payee}"
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
