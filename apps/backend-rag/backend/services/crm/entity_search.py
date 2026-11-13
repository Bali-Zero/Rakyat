"""Smart search for the practice (Process) and company bars, on the keys of migration 358.

Same behaviour as the client search (``client_search``): case-, accent- and punctuation-
insensitive, word order free, every token must match, phone formats 0 / 62 / +62 are one number
(practices, through the client's phone key). No column is added and no index is created: the
SQL below uses the IDENTICAL expressions migration 358 indexes (``bz_client_key``,
``bz_phone_key``, ``bz_name_key``, ``bz_ident_key``), so the planner can use them. Tokens are
produced by ``client_search.search_tokens`` (``bz_fold`` in SQL).

Companies additionally ignore the legal affixes: a leading PT / CV and a trailing Tbk are dropped
from the typed text by ``strip_company_affixes`` (the rule the invoice-company search and the
save path use), so "PT Acme" finds "Acme Synthetic" and "acme" finds "PT Acme".

A token shorter than ``MIN_TRIGRAM`` cannot be served by a trigram index. When EVERY token is
short the builders return None and the caller keeps its legacy substring search (fast, no
folding); a short token next to a long one only filters the rows the long token found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg

from backend.services.crm.client_search import (
    _DIGITS,
    MIN_NUMERIC_DIGITS,
    MIN_TRIGRAM,
    search_tokens,
    smart_search_available,
)
from backend.services.invoicing.invoice_company import strip_company_affixes

__all__ = [
    "EntitySearch",
    "build_company_search",
    "build_practice_search",
    "escape_like",
    "smart_search_available",
]

# The expressions migration 358 indexes (alias-independent for the planner).
CLIENT_KEY = "public.bz_client_key(c.full_name, c.email)"
CLIENT_PHONE_KEY = "public.bz_phone_key(c.phone, c.phone_normalized, c.whatsapp)"
COMPANY_NAME_KEY = "public.bz_name_key(c.company_name)"
COMPANY_IDENT_KEY = "public.bz_ident_key(c.nib, c.npwp_company)"


@dataclass
class EntitySearch:
    """WHERE fragment (starts with ' AND ') and its params, numbered from ``start_index``."""

    clause: str
    params: list[Any] = field(default_factory=list)
    next_index: int = 0


def escape_like(text: str) -> str:
    """Escape LIKE / ILIKE wildcards so the legacy fallback matches the typed characters."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like(expr: str, param: str) -> str:
    return f"{expr} LIKE '%' || {param} || '%'"


async def _phone_cores(conn: asyncpg.Connection, tokens: list[str]) -> dict[str, str]:
    numeric = [t for t in tokens if _DIGITS.fullmatch(t) and len(t) >= MIN_NUMERIC_DIGITS]
    if not numeric:
        return {}
    cores = await conn.fetchval(
        "SELECT array_agg(public.bz_phone_core(t) ORDER BY n) "
        "FROM unnest($1::text[]) WITH ORDINALITY AS u(t, n)",
        numeric,
    )
    return dict(zip(numeric, cores or [], strict=False))


async def build_practice_search(
    conn: asyncpg.Connection, search: str, start_index: int
) -> EntitySearch | None:
    """AND-clause for ``practices p JOIN clients c JOIN practice_types pt``; None = use legacy.

    A token matches a practice when it is inside the client key (compact folded name and
    e-mail), inside the service name or code, inside the practice number, or, when numeric,
    inside the client's phone key (0 / 62 / +62 are the same number).
    """
    tokens = await search_tokens(conn, search)
    if not tokens:
        return EntitySearch(" AND FALSE", [], start_index)
    if all(len(t) < MIN_TRIGRAM for t in tokens):
        return None
    cores = await _phone_cores(conn, tokens)
    clauses: list[str] = []
    params: list[Any] = []
    index = start_index
    for token in tokens:
        param = f"${index}::text"
        params.append(token)
        index += 1
        arms = [
            _like(CLIENT_KEY, param),
            _like("public.bz_name_key(pt.name)", param),
            _like("public.bz_name_key(pt.code)", param),
        ]
        if _DIGITS.fullmatch(token):
            arms.append(_like("p.id::text", param))
            core = cores.get(token, "")
            if len(core) >= MIN_NUMERIC_DIGITS:
                params.append(core)
                arms.append(_like(CLIENT_PHONE_KEY, f"${index}::text"))
                index += 1
        clauses.append("(" + " OR ".join(arms) + ")")
    return EntitySearch(" AND " + " AND ".join(clauses), params, index)


async def build_company_search(
    conn: asyncpg.Connection, search: str, start_index: int
) -> EntitySearch | None:
    """AND-clause for ``companies c``; None = use the legacy ILIKE on the raw text.

    A token matches a company when it is inside the compact folded name (after the PT / CV / Tbk
    affixes are dropped from the typed text) or, when numeric, inside the NIB / NPWP digits.
    """
    text = search.strip()
    # Affixes are a name rule: a query with no letter (an NIB / NPWP fragment) is left alone.
    stripped = (strip_company_affixes(text) or text) if any(ch.isalpha() for ch in text) else text
    tokens = await search_tokens(conn, stripped)
    if not tokens:
        return EntitySearch(" AND FALSE", [], start_index)
    if all(len(t) < MIN_TRIGRAM for t in tokens):
        return None
    clauses: list[str] = []
    params: list[Any] = []
    index = start_index
    for token in tokens:
        param = f"${index}::text"
        params.append(token)
        index += 1
        arms = [_like(COMPANY_NAME_KEY, param)]
        if _DIGITS.fullmatch(token):
            arms.append(_like(COMPANY_IDENT_KEY, param))
        clauses.append("(" + " OR ".join(arms) + ")")
    return EntitySearch(" AND " + " AND ".join(clauses), params, index)
