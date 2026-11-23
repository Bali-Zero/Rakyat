"""The four issuing companies as the accounting page shows them: bank, account, statement coverage.

A statement belongs to a company when its account number, digits only, equals the company's bank
account number, digits only (a statement's number can carry a currency suffix, a company's can
carry dashes). A statement whose account matches no company is reported once as "other", so no
imported money hides from the page.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import asyncpg

from backend.services.accounting.period import month_bounds, month_condition
from backend.services.invoicing.entities import DEFAULT_ENTITY_KEY, ENTITIES

OTHER_KEY = "other"


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def company_key_for_account(account_number: str | None) -> str | None:
    """The issuing company owning this bank account, or None when no company does.

    Equality of the whole digit string, never containment: an account whose digits only
    start or end with a company's is another account.
    """
    digits = _digits(account_number)
    if not digits:
        return None
    for entity in ENTITIES.values():
        if _digits(entity.bank_account_number) == digits:
            return entity.key
    return None


def account_key_for_account(account_number: str | None) -> str | None:
    """A stable, account-specific key for an account outside the registry."""
    digits = _digits(account_number)
    return f"account:{digits}" if digits else None


def account_for_company_key(company_key: str) -> str | None:
    """Digits of exactly one account selected by the accounting UI, if valid."""
    entity = ENTITIES.get(company_key)
    if entity:
        return _digits(entity.bank_account_number)
    if company_key.startswith("account:"):
        digits = _digits(company_key.removeprefix("account:"))
        return digits or None
    return None


def company_key_for_issuer(issuer_entity_key: str | None) -> str:
    """The company that issued an invoice: its stored issuer, or the default issuer when NULL.

    Invoices made before issuer routing carry NULL and were issued by the default company, the
    rule the invoice service itself applies to them.
    """
    return issuer_entity_key or DEFAULT_ENTITY_KEY


def _later(a: date | None, b: date | None) -> date | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


async def load_companies(
    conn: asyncpg.Connection, *, month: date | None = None
) -> list[dict[str, Any]]:
    """Every issuing company, in the registry's order, with its statement coverage."""
    bounds = month_bounds(month) if month else ()
    through = "LEAST(MAX(bs.period_end), $2::date)" if month else "MAX(bs.period_end)"
    overlap = "WHERE bs.period_end >= $1::date AND bs.period_start <= $2::date" if month else ""
    statements = await conn.fetch(
        f"""
        SELECT bs.account_number, bs.account_name, {through} AS through,
               COUNT(*) AS statements
        FROM bank_statements bs
        {overlap}
        GROUP BY bs.account_number, bs.account_name
        """,
        *bounds,
    )
    params = []
    period = month_condition("bt.txn_date", month, params)
    waiting = await conn.fetch(
        f"""
        SELECT bs.account_number, COUNT(*) AS to_reconcile
        FROM bank_transactions bt
        LEFT JOIN bank_statements bs ON bs.id = bt.statement_id
        WHERE bt.direction = 'credit' AND bt.reconciled_status = 'unmatched'{period}
        GROUP BY bs.account_number
        """,
        *params,
    )
    per_key: dict[str, dict[str, Any]] = {}
    for entity in ENTITIES.values():
        per_key[entity.key] = {
            "key": entity.key,
            "company_name": entity.company_name,
            "bank_name": entity.bank_name,
            "account_number": entity.bank_account_number,
            "data_through": None,
            "statements": 0,
            "to_reconcile": 0,
        }
    def account_key(row: Any) -> str:
        return (
            company_key_for_account(row["account_number"])
            or account_key_for_account(row["account_number"])
            or OTHER_KEY
        )

    def ensure_unknown(key: str, row: Any) -> None:
        if key not in per_key:
            per_key[key] = {
                "key": key,
                "company_name": "Unassigned bank account",
                "bank_name": None,
                "account_number": row["account_number"],
                "data_through": None,
                "statements": 0,
                "to_reconcile": 0,
            }
    for row in statements:
        key = account_key(row)
        ensure_unknown(key, row)
        entry = per_key[key]
        entry["data_through"] = _later(entry["data_through"], row["through"])
        entry["statements"] += int(row["statements"])
    for row in waiting:
        key = account_key(row)
        ensure_unknown(key, row)
        per_key[key]["to_reconcile"] += int(row["to_reconcile"])
    for entry in per_key.values():
        if entry["data_through"] is not None:
            entry["data_through"] = entry["data_through"].isoformat()
    return list(per_key.values())
