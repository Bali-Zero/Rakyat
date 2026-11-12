"""Smart client search: word order, accents, punctuation, phone formats, linked company.

``GET /api/crm/clients?search=`` builds its WHERE and ORDER BY here. The search keys are
IMMUTABLE SQL functions of existing columns (migration 358: ``bz_fold``, ``bz_client_key``,
``bz_phone_key``, ``bz_ident_key``, ``bz_name_key``), served by EXPRESSION GIN pg_trgm indexes;
no column is added to ``clients`` or ``companies``. The query below must use the identical
expressions, or the planner cannot use the indexes (``test_client_smart_search_pg`` asserts it
from EXPLAIN). The typed text is folded by the same ``bz_fold`` in SQL; Python only splits the
folded text on spaces.

Every token must match (AND), in any order. A token matches a client when it is contained in
the client key (compact folded name and e-mail), or in the key of a company the client is linked
to, or, when it is numeric, in the phone key (0 / 62 / +62 are the same number) or the NIB /
NPWP digits of the client or of a linked company. A query made only of digits and phone
punctuation is ONE numeric token.

A trigram index cannot serve a token shorter than ``MIN_TRIGRAM`` characters (it would fold
every row, about 1.5 s on 12.5k clients). When EVERY token is shorter, ``build_client_search``
returns None and the router answers with the legacy substring search on the raw fields (fast,
no accent or word-order folding yet). A short token next to a long one is only a filter on the
rows the long token already found.

The router also keeps the legacy search while migration 358 is not applied
(``smart_search_available`` is False), so a deploy that lands before its migration still answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import asyncpg

MAX_TOKENS = 6
MIN_TRIGRAM = 3
MIN_NUMERIC_DIGITS = 3
_NUMERIC_QUERY = re.compile(r"[\d\s().+\-/]+")
_DIGITS = re.compile(r"[0-9]+")

# One definition per key, the same text the migration indexes (aliases do not matter).
CLIENT_EXPR = "public.bz_client_key(c.full_name, c.email)"
PHONE_EXPR = "public.bz_phone_key(c.phone, c.phone_normalized, c.whatsapp)"
CLIENT_IDENT_EXPR = "public.bz_ident_key(c.nib, c.npwp)"
COMPANY_NAME_EXPR = "public.bz_name_key(co.company_name)"
COMPANY_IDENT_EXPR = "public.bz_ident_key(co.nib, co.npwp_company)"
INDEX_NAMES = (
    "idx_clients_smart_key",
    "idx_clients_smart_phone",
    "idx_clients_smart_ident",
    "idx_companies_smart_name",
    "idx_companies_smart_ident",
)
_FUNCTIONS = (
    "public.bz_fold(text)",
    "public.bz_phone_core(text)",
    "public.bz_name_key(text)",
    "public.bz_client_key(text, text)",
    "public.bz_phone_key(text, text, text)",
    "public.bz_ident_key(text, text)",
    "public.bz_name_rank(text, text[])",
)


@dataclass
class ClientSearch:
    """WHERE fragment (starts with ' AND '), its params, and the ORDER BY for a search."""

    clause: str
    params: list[Any] = field(default_factory=list)
    order_by: str = ""
    next_index: int = 0


def _like(expr: str, param: str) -> str:
    return f"{expr} LIKE '%' || {param} || '%'"


def _token_clause(index: int, *, long_token: bool, numeric: bool, core_index: int | None) -> str:
    """OR of the places one token can match; the arms of a long token are all index-served."""
    param = f"${index}::text"
    company_exprs = [COMPANY_NAME_EXPR, COMPANY_IDENT_EXPR]
    company_arms = [_like(COMPANY_NAME_EXPR, param)]
    if numeric and long_token:
        company_arms.append(_like(COMPANY_IDENT_EXPR, param))
    parts = [_like(CLIENT_EXPR, param)]
    if long_token:
        # One ANY(ARRAY(subselect)) per company key: an InitPlan the planner can BitmapOr with the
        # client key. An `IN (SELECT ...)` inside this OR is NOT indexable (seq scan, ~1.3 s), and
        # one subselect with an OR of two company keys loses its indexes too.
        for company_expr in company_exprs[: 2 if numeric else 1]:
            parts.append(
                "c.id = ANY(ARRAY(SELECT ccl.client_id FROM client_company_links ccl"
                f" JOIN companies co ON co.id = ccl.company_id WHERE {_like(company_expr, param)}))"
            )
        if numeric:
            parts.append(_like(CLIENT_IDENT_EXPR, param))
            if core_index is not None:
                parts.append(_like(PHONE_EXPR, f"${core_index}::text"))
    else:
        # A short token only filters rows another (long) token already found.
        parts.append(
            "EXISTS (SELECT 1 FROM client_company_links ccl"
            " JOIN companies co ON co.id = ccl.company_id"
            f" WHERE ccl.client_id = c.id AND ({' OR '.join(company_arms)}))"
        )
    return "(" + " OR ".join(parts) + ")"


async def smart_search_available(conn: asyncpg.Connection) -> bool:
    """True when migration 358 (key functions and the five expression indexes) is applied."""
    try:
        return bool(
            await conn.fetchval(
                "SELECT "
                + " AND ".join(f"to_regprocedure('{fn}') IS NOT NULL" for fn in _FUNCTIONS)
                + " AND (SELECT count(*) FROM pg_indexes"
                " WHERE schemaname = ANY (current_schemas(false))"
                " AND indexname = ANY ($1::text[])) = $2",
                list(INDEX_NAMES),
                len(INDEX_NAMES),
            )
        )
    except asyncpg.PostgresError:
        return False


async def build_client_search(
    conn: asyncpg.Connection, search: str, start_index: int
) -> ClientSearch | None:
    """The smart WHERE / ORDER BY for a non-blank ``search``.

    Returns None when every token is shorter than a trigram (the caller then uses the legacy
    substring search). A search that folds to nothing searchable (only punctuation) returns a
    clause that matches no row.
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
    if all(len(t) < MIN_TRIGRAM for t in tokens):
        return None

    numeric_tokens = [t for t in tokens if _DIGITS.fullmatch(t) and len(t) >= MIN_NUMERIC_DIGITS]
    cores: dict[str, str] = {}
    if numeric_tokens:
        rows = await conn.fetchval(
            "SELECT array_agg(public.bz_phone_core(t) ORDER BY n) "
            "FROM unnest($1::text[]) WITH ORDINALITY AS u(t, n)",
            numeric_tokens,
        )
        cores = dict(zip(numeric_tokens, rows or [], strict=False))

    clauses: list[str] = []
    params: list[Any] = []
    index = start_index
    for token in tokens:
        numeric = bool(_DIGITS.fullmatch(token))
        long_token = len(token) >= MIN_TRIGRAM
        token_index = index
        params.append(token)
        index += 1
        core_index = None
        core = cores.get(token, "")
        if long_token and numeric and len(core) >= MIN_NUMERIC_DIGITS:
            core_index = index
            params.append(core)
            index += 1
        clauses.append(
            _token_clause(
                token_index, long_token=long_token, numeric=numeric, core_index=core_index
            )
        )

    # Ranking (bz_name_rank, migration 358), evaluated only for the rows that matched: exact words
    # in any order, name prefix, every token in the name, other; then most recently updated.
    params.append(tokens)
    order_by = (
        f"public.bz_name_rank(c.full_name, ${index}::text[]), c.updated_at DESC NULLS LAST, c.id"
    )
    index += 1
    return ClientSearch(" AND " + " AND ".join(clauses), params, order_by, index)
