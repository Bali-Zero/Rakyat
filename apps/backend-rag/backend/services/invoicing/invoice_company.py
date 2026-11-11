"""The company an invoice is addressed to ("Invoice to company" on a practice).

``practices.invoice_company_id`` NULL means: bill the individual client. A company
is chosen by id, or typed by name (+ optional NPWP); typing resolves to an existing
company when it can (NPWP digits, then normalised name) and otherwise creates it,
and in every case the individual is linked to it. One function decides, so the
create route, the update route and the form suggestion cannot drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import asyncpg

AUTOFILL_CATEGORY_PREFIXES = ("tax",)
AUTOFILL_CATEGORIES = frozenset({"company", "corporate"})
NEW_LINK_ROLE = "Director"
_MIN_NPWP_DIGITS = 15
_LEADING_LEGAL = re.compile(r"^\s*(pt|cv)\b\.?\s*", re.IGNORECASE)


class InvoiceCompanyError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def normalize_company_name(name: str | None) -> str:
    """Casefold, drop a leading PT/CV, keep only letters and digits."""
    stripped = _LEADING_LEGAL.sub("", (name or "").strip(), count=1)
    return re.sub(r"[^a-z0-9]", "", stripped.casefold())


def normalize_npwp(npwp: str | None) -> str | None:
    digits = re.sub(r"\D", "", npwp or "")
    return digits if len(digits) >= _MIN_NPWP_DIGITS else None


def company_type_from_name(name: str) -> str | None:
    match = _LEADING_LEGAL.match(name)
    return match.group(1).upper() if match else None


def is_director_role(role: str | None) -> bool:
    folded = re.sub(r"[^a-z]", "", (role or "").casefold())
    return folded in {
        "director",
        "direktur",
        "directorutama",
        "direkturutama",
        "presidentdirector",
        "presidendirektur",
    }


def autofill_applies(category: str | None) -> bool:
    folded = (category or "").casefold()
    return folded.startswith(AUTOFILL_CATEGORY_PREFIXES) or folded in AUTOFILL_CATEGORIES


@dataclass(frozen=True)
class Suggestion:
    autofill: bool
    suggested_company_id: int | None
    companies: list[dict[str, Any]]


def suggest(links: list[dict[str, Any]], category: str | None) -> Suggestion:
    """Autofill rule: one active company prefills, several are a choice, none is free text.

    With several, a single director-role company is highlighted as the suggestion
    but the caller still shows the choice; the server never picks one silently.
    """
    active: dict[int, dict[str, Any]] = {}
    for link in links:
        if link.get("status") not in (None, "active") or link.get("end_date"):
            continue
        entry = active.setdefault(link["company_id"], {**link, "is_director": False})
        entry["is_director"] = entry["is_director"] or is_director_role(link.get("role"))
    companies = list(active.values())
    if not autofill_applies(category):
        return Suggestion(False, None, companies)
    if len(companies) == 1:
        return Suggestion(True, companies[0]["company_id"], companies)
    directors = [c for c in companies if c["is_director"]]
    if len(companies) > 1 and len(directors) == 1:
        return Suggestion(True, directors[0]["company_id"], companies)
    return Suggestion(True, None, companies)


async def fetch_client_company_links(
    conn: asyncpg.Connection, client_id: int
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT ccl.company_id, c.company_name, c.npwp_company, ccl.role, ccl.status, ccl.end_date
        FROM client_company_links ccl
        JOIN companies c ON c.id = ccl.company_id
        WHERE ccl.client_id = $1
        ORDER BY ccl.company_id
        """,
        client_id,
    )
    return [dict(row) for row in rows]


async def _find_by_name(conn: asyncpg.Connection, normalized: str) -> list[asyncpg.Record]:
    rows = await conn.fetch(
        "SELECT id, company_name, npwp_company FROM companies"
        " WHERE regexp_replace(lower(company_name), '[^a-z0-9]', '', 'g') LIKE '%' || $1 || '%'",
        normalized,
    )
    return [row for row in rows if normalize_company_name(row["company_name"]) == normalized]


def _ambiguous(rows: list[asyncpg.Record]) -> InvoiceCompanyError:
    return InvoiceCompanyError(
        409,
        {
            "code": "invoice_company_ambiguous",
            "message": "Several companies match",
            "candidates": [{"id": r["id"], "company_name": r["company_name"]} for r in rows],
        },
    )


async def _lock(conn: asyncpg.Connection, *keys: str) -> None:
    for key in sorted(set(keys)):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)


async def resolve_invoice_company(
    conn: asyncpg.Connection,
    client_id: int,
    *,
    company_id: int | None,
    name: str | None,
    npwp: str | None,
    actor: str,
) -> int:
    """Return the company id to store, creating the company and the link as needed.

    Runs in the caller's transaction, after the caller has authorised a write on the
    client. Raises ``InvoiceCompanyError``: 404 unknown id, 422 empty name, 409 when
    several companies match (by NPWP or by name) with the candidates listed.

    An NPWP identifies a company: a typed NPWP that exists wins over the typed name;
    a typed NPWP that exists nowhere never reuses a company that has another NPWP.
    """
    if company_id is not None:
        if not await conn.fetchval("SELECT 1 FROM companies WHERE id = $1", company_id):
            raise InvoiceCompanyError(404, f"Company {company_id} not found")
        resolved = company_id
    else:
        typed = (name or "").strip()
        normalized = normalize_company_name(typed)
        if not normalized:
            raise InvoiceCompanyError(422, "invoice_company_name is empty")
        npwp_typed = (npwp or "").strip() or None
        npwp_digits = normalize_npwp(npwp)
        await _lock(
            conn,
            f"invoice-company:name:{normalized}",
            *([f"invoice-company:npwp:{npwp_digits}"] if npwp_digits else []),
            *([f"invoice-company:npwp-raw:{npwp_typed}"] if npwp_typed else []),
        )
        found: int | None = None
        if npwp_typed:
            by_npwp = await conn.fetch(
                "SELECT id, company_name FROM companies WHERE npwp_company = $1"
                " OR ($2::text IS NOT NULL"
                " AND regexp_replace(npwp_company, '[^0-9]', '', 'g') = $2) ORDER BY id",
                npwp_typed,
                npwp_digits,
            )
            if len(by_npwp) > 1:
                raise _ambiguous(by_npwp)
            found = by_npwp[0]["id"] if by_npwp else None
        if found is None:
            matches = await _find_by_name(conn, normalized)
            if npwp_typed:
                matches = [m for m in matches if not (m["npwp_company"] or "").strip()]
            if len(matches) > 1:
                raise _ambiguous(matches)
            found = matches[0]["id"] if matches else None
        if found is None:
            found = await conn.fetchval(
                "INSERT INTO companies (company_name, company_type, npwp_company, created_by, updated_by)"
                " VALUES ($1, COALESCE($2, 'PT'), $3, $4, $4) RETURNING id",
                typed,
                company_type_from_name(typed),
                npwp_typed,
                actor,
            )
        resolved = found
    await _lock(conn, f"invoice-company:link:{client_id}:{resolved}")
    await conn.execute(
        "INSERT INTO client_company_links (client_id, company_id, role, status)"
        " SELECT $1::int, $2::int, $3::text, 'active'"
        " WHERE NOT EXISTS (SELECT 1 FROM client_company_links WHERE client_id = $1 AND company_id = $2)",
        client_id,
        resolved,
        NEW_LINK_ROLE,
    )
    return resolved


async def fetch_bill_to(
    conn: asyncpg.Connection, company_id: int | None
) -> dict[str, str | None] | None:
    if company_id is None:
        return None
    row = await conn.fetchrow(
        "SELECT company_name, npwp_company, registered_address FROM companies WHERE id = $1",
        company_id,
    )
    if row is None:
        return None
    return {
        "bill_to_company": row["company_name"],
        "bill_to_npwp": row["npwp_company"],
        "bill_to_address": row["registered_address"],
    }
