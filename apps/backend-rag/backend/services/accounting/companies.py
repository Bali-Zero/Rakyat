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

from backend.services.invoicing.entities import ENTITIES

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


def _later(a: date | None, b: date | None) -> date | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


async def load_companies(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every issuing company, in the registry's order, with its statement coverage."""
    statements = await conn.fetch(
        """
        SELECT bs.account_number, bs.account_name, MAX(bs.period_end) AS through,
               COUNT(*) AS statements
        FROM bank_statements bs
        GROUP BY bs.account_number, bs.account_name
        """
    )
    waiting = await conn.fetch(
        """
        SELECT bs.account_number, COUNT(*) AS to_reconcile
        FROM bank_transactions bt
        JOIN bank_statements bs ON bs.id = bt.statement_id
        WHERE bt.direction = 'credit' AND bt.reconciled_status = 'unmatched'
        GROUP BY bs.account_number
        """
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
    for row in statements:
        key = company_key_for_account(row["account_number"]) or OTHER_KEY
        entry = per_key.setdefault(
            key,
            {
                "key": OTHER_KEY,
                "company_name": "Other accounts",
                "bank_name": None,
                "account_number": None,
                "data_through": None,
                "statements": 0,
                "to_reconcile": 0,
            },
        )
        entry["data_through"] = _later(entry["data_through"], row["through"])
        entry["statements"] += int(row["statements"])
    for row in waiting:
        key = company_key_for_account(row["account_number"]) or OTHER_KEY
        per_key[key]["to_reconcile"] += int(row["to_reconcile"])
    for entry in per_key.values():
        if entry["data_through"] is not None:
            entry["data_through"] = entry["data_through"].isoformat()
    return list(per_key.values())
