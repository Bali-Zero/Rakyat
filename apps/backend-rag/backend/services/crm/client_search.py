"""Smart client search: word order, accents, punctuation, phone formats, linked company.

``GET /api/crm/clients?search=`` builds its WHERE and ORDER BY here. One SQL function,
``public.bz_fold`` (migration 358), folds both the stored keys and the typed query, so the two
can never drift; Python only splits the folded text on spaces. Every token must match (AND), in
any order. A token matches a client when it is contained in ``search_key`` (compact folded name
and e-mail), or in the folded name of a company the client is linked to, or, when it is
numeric, in ``phone_key`` (phone core: 0 / 62 / +62 are the same number) or ``ident_key``
(NIB / NPWP digits) or in a linked company's NIB / NPWP digits. A query made only of digits and
phone punctuation is ONE numeric token.

When migration 358 is not applied yet (``smart_search_available`` is False) the router keeps the
legacy substring search, so a deploy that lands before its migration still answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import asyncpg

MAX_TOKENS = 6
MIN_NUMERIC_DIGITS = 3
_NUMERIC_QUERY = re.compile(r"[\d\s().+\-/]+")
_DIGITS = re.compile(r"[0-9]+")

_COMPANY_NAME = "co.name_key"
_COMPANY_IDENT = "co.ident_key"
_NAME_COMPACT = "c.name_key"


@dataclass
class ClientSearch:
    """WHERE fragment (starts with ' AND '), its params, and the ORDER BY for a search."""

    clause: str
    params: list[Any] = field(default_factory=list)
    order_by: str = ""
    next_index: int = 0


def _token_clause(index: int, numeric: bool) -> str:
    param = f"${index}::text"
    company = f"{_COMPANY_NAME} LIKE '%' || {param} || '%'"
    if numeric:
        company += (
            f" OR (length({param}) >= {MIN_NUMERIC_DIGITS}"
            f" AND {_COMPANY_IDENT} LIKE '%' || {param} || '%')"
        )
    parts = [
        f"c.search_key LIKE '%' || {param} || '%'",
        "c.id IN (SELECT ccl.client_id FROM client_company_links ccl"
        f" JOIN companies co ON co.id = ccl.company_id WHERE {company})",
    ]
    if numeric:
        parts.append(
            f"(length(public.bz_phone_core({param})) >= {MIN_NUMERIC_DIGITS}"
            f" AND c.phone_key LIKE '%' || public.bz_phone_core({param}) || '%')"
        )
        parts.append(
            f"(length({param}) >= {MIN_NUMERIC_DIGITS} AND c.ident_key LIKE '%' || {param} || '%')"
        )
    return "(" + " OR ".join(parts) + ")"


async def smart_search_available(conn: asyncpg.Connection) -> bool:
    """True when migration 358 (function and key columns on clients and companies) is applied."""
    try:
        return bool(
            await conn.fetchval(
                "SELECT to_regprocedure('public.bz_fold(text)') IS NOT NULL AND ("
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema = ANY (current_schemas(false))"
                " AND ((table_name = 'clients' AND column_name IN"
                " ('name_key', 'search_key', 'phone_key', 'ident_key'))"
                " OR (table_name = 'companies' AND column_name IN ('name_key', 'ident_key')))) = 6"
            )
        )
    except asyncpg.PostgresError:
        return False


async def build_client_search(
    conn: asyncpg.Connection, search: str, start_index: int
) -> ClientSearch:
    """The smart WHERE / ORDER BY for a non-blank ``search``.

    A search that folds to nothing searchable (only punctuation) returns a clause that
    matches no row.
    """
    text = search.strip()
    numeric_query = bool(_NUMERIC_QUERY.fullmatch(text))
    digits = "".join(_DIGITS.findall(text)) if numeric_query else ""
    if numeric_query and len(digits) >= MIN_NUMERIC_DIGITS:
        tokens = [digits]
    else:
        folded = await conn.fetchval("SELECT public.bz_fold($1::text)", text)
        tokens = [t for t in (folded or "").split(" ") if t][:MAX_TOKENS]
    if not tokens:
        return ClientSearch(" AND FALSE", [], "c.created_at DESC", start_index)

    clauses: list[str] = []
    params: list[Any] = []
    index = start_index
    for token in tokens:
        clauses.append(_token_clause(index, bool(_DIGITS.fullmatch(token))))
        params.append(token)
        index += 1

    # Ranking, evaluated only for matched rows and on stored keys (no folding per row):
    # 0 exact (every token inside the name and the name is exactly as long as the tokens
    # together, so the name is those words in some order), 1 prefix (the name starts with the
    # first token), 2 every token inside the name, 3 matched through e-mail, phone, identifiers
    # or a linked company; then most recently updated.
    first_param, total_param = f"${index}::text", f"${index + 1}::int"
    params.extend([tokens[0], sum(len(t) for t in tokens)])
    index += 2
    name_all = " AND ".join(
        f"{_NAME_COMPACT} LIKE '%' || ${start_index + i}::text || '%'" for i in range(len(tokens))
    )
    order_by = (
        f"CASE WHEN ({name_all}) AND length({_NAME_COMPACT}) = {total_param} THEN 0"
        f" WHEN {_NAME_COMPACT} LIKE {first_param} || '%' THEN 1"
        f" WHEN ({name_all}) THEN 2"
        " ELSE 3 END, c.updated_at DESC NULLS LAST, c.id"
    )
    return ClientSearch(" AND " + " AND ".join(clauses), params, order_by, index)
