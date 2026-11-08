"""AI recap for the kita client card (Immigration / Company tabs).

Every statement follows one row of RECAP_SPEC.md (numbers spec v3.3, strict parsing): a recorded
value read through a strict grammar, or a count over identities the database defines. A value
that does not parse is omitted, never repaired. A count or a selection over a set with any
unparseable or unknown member is omitted as a whole.

Pipeline per request: build facts from rows kita already holds -> reduce them through ONE
allow-list (``recap_payload``) -> sha256 of the canonical JSON -> return the cached row when the
hash is unchanged, otherwise store the deterministic template, return it, and let a configured
provider rewrite it in the background. A changed fact set therefore refreshes the recap by itself;
an unchanged one never costs a model call.

PII boundary (hard rule): only what ``recap_payload`` lets through reaches the template, the cache
or a prompt, and it lets through codes, enums, counts, ISO dates, capital amounts and the names of
database catalogue rows (``visa_types.name``, ``practice_types.name``). Never names of a client,
family member, shareholder, staff member or company; never passport/permit/NIB/NPWP/AHU numbers,
addresses, emails, phones, sponsors or file names; never company free text such as
``kbli_description``. Documents and images are never sent. No log line carries a client value:
exception type names and fixed reason codes only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from backend.services.common.background import spawn
from backend.services.crm.client_attention import _usable
from backend.services.crm.client_recap_text import (
    RecapProvider,
    based_on,
    configured_provider,
    rewrite_with_retry,
    template_recap,
)
from backend.services.crm.permit_label import FAMILY_ITK
from backend.services.crm.recap_grammar import (
    CODE_LIST_MALFORMED,
    DIRECTOR,
    IMMIGRATION,
    LINK_ROLES,
    OPEN,
    PRACTICE_CATEGORIES,
    PRACTICE_STATUSES,
    SHAREHOLDER,
    UNKNOWN,
    classify,
    closed_key,
    decode_jsonb,
    legal_form,
    lkpm_status,
    parse_code_list,
    parse_date,
    parse_kbli_code,
    parse_lkpm_period,
    parse_nonneg_int,
    today_wita,
)
from backend.services.crm.recap_permits import (
    ISSUE_CONFLICTING,
    STAY_FAMILIES,
    classify_document,
    pick_current_permit,
)
from backend.services.kbli_catalogue_membership import (
    ABSENT,
    CANONICAL,
    catalogue_verdict,
    fetch_catalogue_membership,
)
from backend.services.kbli_catalogue_membership import UNKNOWN as VERDICT_UNKNOWN

logger = logging.getLogger(__name__)

SCOPE_IMMIGRATION = "immigration"
SCOPE_COMPANY = "company"
# The switch of PR #31. The recap serves again since the strict-parsing lane (RECAP_SPEC.md v3.3)
# landed; setting this to False answers text=null (the card hides it) with no fact query,
# no generation and no cache write.
RECAP_ENABLED = True
SCOPES = (SCOPE_IMMIGRATION, SCOPE_COMPANY)

TEMPLATE_MODEL = "template"
REWRITE_RETRY_AFTER_S = 600
# Bounded output size: more distinct KBLI codes than this omit C5 and C6 (and cap the catalogue
# round trips a single recap request can make).
MAX_KBLI_CODES = 50
MAX_PRACTICE_TYPE_NAMES = 4

_FAMILY_CODES = {"KITAS / ITAS": "KITAS", "KITAP / ITAP": "KITAP"}
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 /_-]{0,15}$")
_QUARTER_RE = re.compile(r"^Q[1-4]$")
_TITLE_RE = re.compile(r"^[A-Za-z0-9 ()/.,&'+-]{1,120}$")

_SQL_SELECT = """
    SELECT facts_hash, text, model, generated_at
      FROM client_ai_recaps
     WHERE client_id = $1 AND scope = $2 AND subject_key = $3
"""
_SQL_UPSERT = """
    INSERT INTO client_ai_recaps (client_id, scope, subject_key, facts_hash, text, model, generated_at)
    VALUES ($1, $2, $3, $4, $5, $6, now())
    ON CONFLICT (client_id, scope, subject_key) DO UPDATE
       SET facts_hash = EXCLUDED.facts_hash,
           text = EXCLUDED.text,
           model = EXCLUDED.model,
           generated_at = EXCLUDED.generated_at
    RETURNING generated_at
"""


# ── value sanitizers (the allow-list is built from these) ───────────────────


def _s_date(v: Any) -> str | None:
    d = parse_date(v)
    return d.isoformat() if d else None


def _s_int(v: Any) -> int | None:
    """A real non-negative ``int`` only: the builders emit ints, the allow-list takes nothing else."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _s_count(v: Any) -> int | None:
    n = _s_int(v)
    return n or None


def _s_code(v: Any) -> str | None:
    if not (isinstance(v, str) and _CODE_RE.match(v.strip())):
        return None
    code = v.strip().upper()
    return _FAMILY_CODES.get(code, code)


def clean_permit_name(name: Any) -> str | None:
    """Same rule as the frontend cleanPermitLabel: drop "(...)" qualifiers, collapse spaces."""
    if not isinstance(name, str):
        return None
    cleaned = re.sub(r"\s+", " ", re.sub(r"\s*\([^)]*\)", "", name)).strip()
    return _FAMILY_CODES.get(cleaned.upper(), cleaned) or None


def _s_status(v: Any) -> str | None:
    return v if v in ("valid", "expired") else None


def _s_enum(v: Any) -> str | None:
    return v if v in ("draft", "pending", "submitted", "approved") else None


def _s_quarter(v: Any) -> str | None:
    return v if isinstance(v, str) and _QUARTER_RE.match(v) else None


_SLOT_BANNED_RE = re.compile(
    r"\b(ignore|instructions?|disregard|override|prompt|assistant|rewrite|you|your|draft|https?|www)\b",
    re.IGNORECASE,
)


def _s_slot(v: Any, maxlen: int = 60) -> str | None:
    """A label from a database catalogue: shape-checked, length-bounded and stripped of anything
    that reads as an instruction or a meta phrase."""
    t = _s_title(v)
    if not t:
        return None
    t = re.sub(r"\s+", " ", t)
    return None if len(t) > maxlen or _SLOT_BANNED_RE.search(t) else t


def _s_title(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and _TITLE_RE.match(v.strip()) else None


def _s_legal_form(v: Any) -> str | None:
    return legal_form(v)


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {})}


def _list(v: Any, fn, limit: int) -> list[Any]:
    if not isinstance(v, Sequence) or isinstance(v, (str, bytes)):
        return []
    out = [fn(x) for x in v]
    return [x for x in out if x not in (None, {}, [])][:limit]


def _mapping(v: Any) -> Mapping[str, Any]:
    return v if isinstance(v, Mapping) else {}


def recap_payload(scope: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """THE allow-list: the only function whose output reaches the template, the cache or a prompt.

    Any key not named here is dropped, every value is re-validated against a strict shape
    (code / enum / ISO date / integer), so a free-text field (name, number, address, ...)
    cannot pass even if a builder leaked it.
    """
    f = facts if isinstance(facts, Mapping) else {}
    if scope == SCOPE_IMMIGRATION:
        cp = _mapping(f.get("current_permit"))
        return _prune(
            {
                "current_permit": _prune(
                    {
                        "code": _s_code(cp.get("code")),
                        "official_name": _s_slot(clean_permit_name(cp.get("official_name")), 80),
                        "status": _s_status(cp.get("status")),
                        "issue_date": _s_date(cp.get("issue_date")),
                        "expiry_date": _s_date(cp.get("expiry_date")),
                    }
                ),
                "itk_expiry_date": _s_date(f.get("itk_expiry_date")),
                "itk_status": _s_status(f.get("itk_status")),
                "passport_expiry_date": _s_date(f.get("passport_expiry_date")),
                "passport_status": _s_status(f.get("passport_status")),
                "family_members_count": _s_count(f.get("family_members_count")),
                "family_permits_ending_before_main_count": _s_count(
                    f.get("family_permits_ending_before_main_count")
                ),
                "open_practices_count": _s_count(f.get("open_practices_count")),
                "open_practice_types": _list(
                    f.get("open_practice_types"), _s_slot, MAX_PRACTICE_TYPE_NAMES
                ),
            }
        )
    if scope == SCOPE_COMPANY:
        lk = _mapping(f.get("lkpm_latest"))
        return _prune(
            {
                "legal_form": _s_legal_form(f.get("legal_form")),
                "established_date": _s_date(f.get("established_date")),
                "authorized_capital_idr": _s_int(f.get("authorized_capital_idr")),
                "paid_up_capital_idr": _s_int(f.get("paid_up_capital_idr")),
                "shareholders_count": _s_count(f.get("shareholders_count")),
                "directors_count": _s_count(f.get("directors_count")),
                "kbli_count": _s_count(f.get("kbli_count")),
                "kbli_not_in_catalogue": True if f.get("kbli_not_in_catalogue") is True else None,
                "lkpm_latest": _prune(
                    {
                        "year": _s_int(lk.get("year")),
                        "quarter": _s_quarter(lk.get("quarter")),
                        "status": _s_enum(lk.get("status")),
                    }
                ),
                "last_amendment_date": _s_date(f.get("last_amendment_date")),
            }
        )
    raise ValueError(f"unknown recap scope: {scope!r}")


def facts_hash(payload: Mapping[str, Any]) -> str:
    from backend.services.crm import client_recap_text as _text

    canonical = json.dumps(
        {"v": _text.RECAP_SCHEMA_VERSION, "facts": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── fact builders (pure) ────────────────────────────────────────────────────


def _status_of(d: date, today: date) -> str:
    return "expired" if d < today else "valid"


def open_immigration_practices(
    practices: Sequence[Mapping[str, Any]],
) -> tuple[int, list[str]] | None:
    """I7: (count, up to four type names), or ``None`` when a row could be an open immigration
    practice without being counted. Every row arrives: nothing was filtered in SQL."""
    rows: dict[Any, tuple[str, str, str | None]] = {}
    for i, p in enumerate(practices):
        key = p.get("id")
        key = ("row", i) if key is None else key
        cat = classify(p.get("category"), PRACTICE_CATEGORIES)
        name = p.get("practice_type_name")
        status = classify(p.get("status"), PRACTICE_STATUSES)
        name = name if isinstance(name, str) else None
        if key in rows and rows[key] != (status, cat, name):
            cat, name = UNKNOWN, None  # one primary key, two type answers: not a certain category
        rows[key] = (status, cat, name)
    count = 0
    names: set[str] = set()
    for status, cat, name in rows.values():
        if status == UNKNOWN and cat in (IMMIGRATION, UNKNOWN):
            return None
        if status == OPEN and cat == UNKNOWN:
            return None
        if status == OPEN and cat == IMMIGRATION:
            count += 1
            if name:
                names.add(name)
    return count, sorted(names)[:MAX_PRACTICE_TYPE_NAMES]


def build_immigration_facts(
    *,
    documents: Sequence[Mapping[str, Any]],
    family_members: Sequence[Mapping[str, Any]],
    practices: Sequence[Mapping[str, Any]],
    passport_expiry: Any,
    catalogue: Mapping[str, str] | None,
    today: date | None = None,
) -> dict[str, Any]:
    """Immigration facts, one RECAP_SPEC row at a time (I1-I7). Documents arrive unfiltered: Rule L
    (``_usable``) and the client's-own test are applied here, after classification."""
    today = today or today_wita()
    facts: dict[str, Any] = {}

    permit_docs = []
    for d in documents:
        if d.get("family_member_id") is not None or not _usable(d):
            continue
        pd = classify_document(d, catalogue)
        if pd is not None:
            permit_docs.append(pd)

    current = None
    stay = [p for p in permit_docs if p.family in STAY_FAMILIES]
    if (
        stay
        and all(p.classifiable for p in permit_docs)
        and all(p.expiry is not None for p in stay)
    ):
        current = pick_current_permit(stay)
    if current:
        facts["current_permit"] = {
            "code": current.code,
            "official_name": (catalogue or {}).get(current.code),
            "issue_date": current.issue.isoformat() if current.issue else None,
            "expiry_date": current.expiry.isoformat(),
            "status": _status_of(current.expiry, today),
        }
        itks = [p for p in permit_docs if p.family == FAMILY_ITK]
        if (
            itks
            and all(p.expiry is not None for p in itks)
            and current.issue_state != ISSUE_CONFLICTING
        ):
            eligible = [
                p.expiry
                for p in itks
                if p.expiry is not None
                and (
                    p.expiry >= current.issue
                    if current.issue is not None
                    else today <= p.expiry <= current.expiry
                )
            ]
            if eligible:
                facts["itk_expiry_date"] = max(eligible).isoformat()
                facts["itk_status"] = _status_of(max(eligible), today)

    passport = parse_date(passport_expiry)
    if passport:
        facts["passport_expiry_date"] = passport.isoformat()
        facts["passport_status"] = _status_of(passport, today)

    facts["family_members_count"] = len(family_members)
    if current:
        holders = [
            fm
            for fm in family_members
            if _has_text_or_value(fm.get("current_visa_type")) or fm.get("visa_expiry") is not None
        ]
        ends = [parse_date(fm.get("visa_expiry")) for fm in holders]
        if all(e is not None for e in ends):
            facts["family_permits_ending_before_main_count"] = sum(
                1 for e in ends if e is not None and e < current.expiry
            )

    open_practices = open_immigration_practices(practices)
    if open_practices:
        facts["open_practices_count"], facts["open_practice_types"] = open_practices
    return facts


def _has_text_or_value(v: Any) -> bool:
    return bool(v.strip()) if isinstance(v, str) else v is not None


def collect_kbli_codes(company: Mapping[str, Any]) -> list[str] | None:
    """The distinct KBLI codes on file, or ``None`` when the set is malformed (C5/C6 omitted)."""
    codes: list[str] = []
    primary = company.get("kbli_code")
    if primary is not None and primary != "":
        code = parse_kbli_code(primary)
        if code is None:
            return None
        codes.append(code)
    ok, cf = decode_jsonb(company.get("custom_fields"))
    if not ok:
        return None
    state, items = parse_code_list(cf, "kbli_codes")
    if state == CODE_LIST_MALFORMED:
        return None
    for item in items:
        code = parse_kbli_code(item)
        if code is None:
            return None
        codes.append(code)
    return list(dict.fromkeys(codes))


def _link_counts(links: Sequence[Mapping[str, Any]]) -> tuple[int | None, int | None]:
    """C4: (shareholders, directors) as distinct client ids, each ``None`` when it cannot be exact."""
    holders: set[Any] = set()
    directors: set[Any] = set()
    holders_exact = directors_exact = True
    for link in links:
        role = classify(link.get("role"), LINK_ROLES)
        shares = link.get("shares_count")
        if shares is None:
            shares = 0
        if isinstance(shares, bool) or not isinstance(shares, int) or shares < 0:
            holders_exact = False  # an unreadable share count: the link might be a holder
            shares = 0
        if role == SHAREHOLDER or shares > 0:
            holders.add(link.get("client_id"))
        elif role == UNKNOWN:
            holders_exact = False
        if role == DIRECTOR:
            directors.add(link.get("client_id"))
        elif role == UNKNOWN:
            directors_exact = False
    return (len(holders) if holders_exact else None, len(directors) if directors_exact else None)


def latest_lkpm(
    company_id: Any,
    linked_client_ids: set[Any],
    rows: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    """C7: the latest period of this company's reports, or ``None`` (omit) when it cannot be exact."""
    if rows is None:
        return None
    candidates = []
    for row in rows:
        if row.get("company_id") is None:
            if row.get("client_id") in linked_client_ids:
                return None  # a client-level row cannot be attributed and could be the latest
        elif row.get("company_id") == company_id:
            candidates.append(row)
    if not candidates:
        return None
    periods = [parse_lkpm_period(r.get("year"), r.get("quarter")) for r in candidates]
    if any(p is None for p in periods):
        return None
    latest = max(p for p in periods if p is not None)
    statuses = {
        closed_key(r.get("status")) for r, p in zip(candidates, periods, strict=True) if p == latest
    }
    out: dict[str, Any] = {"year": latest[0], "quarter": f"Q{latest[1]}"}
    if len(statuses) == 1:
        out["status"] = lkpm_status(next(iter(statuses)))
    return out


def build_company_facts(
    *,
    company: Mapping[str, Any],
    links: Sequence[Mapping[str, Any]],
    lkpm_rows: Sequence[Mapping[str, Any]] | None,
    company_id: Any = None,
    kbli_verdicts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Company facts, one RECAP_SPEC row at a time (C1-C7)."""
    ok, cf = decode_jsonb(company.get("custom_fields"))
    cf = cf if ok and isinstance(cf, Mapping) else {}
    facts: dict[str, Any] = {
        "legal_form": legal_form(company.get("company_type")),
        "established_date": _s_date(company.get("akta_pendirian_date")),
        "last_amendment_date": _s_date(company.get("akta_perubahan_date")),
        "authorized_capital_idr": parse_nonneg_int(cf.get("authorized_capital")),
        "paid_up_capital_idr": parse_nonneg_int(cf.get("paid_up_capital")),
    }
    facts["shareholders_count"], facts["directors_count"] = _link_counts(links)

    codes = collect_kbli_codes(company)
    if codes and len(codes) <= MAX_KBLI_CODES:
        verdicts = [(kbli_verdicts or {}).get(c, VERDICT_UNKNOWN) for c in codes]
        if all(v == CANONICAL for v in verdicts):
            facts["kbli_count"] = len(codes)
        elif VERDICT_UNKNOWN not in verdicts and ABSENT in verdicts:
            facts["kbli_not_in_catalogue"] = True

    linked = {link.get("client_id") for link in links}
    facts["lkpm_latest"] = latest_lkpm(company_id, linked, lkpm_rows)
    return facts


# ── DB access + cache ───────────────────────────────────────────────────────

# No status filter and no category filter, and a LEFT JOIN: a row whose status or type cannot be
# classified must reach the builder (RECAP_SPEC I7, Rule 0.4).
_SQL_PRACTICES = """
    SELECT p.id, p.status, pt.category AS category, pt.name AS practice_type_name
      FROM practices p
      LEFT JOIN practice_types pt
        ON pt.id = p.practice_type_id
        OR (p.practice_type_id IS NULL AND pt.code = p.practice_type_code)
     WHERE p.client_id = $1
"""
# Every document of the client, live or not: Rule L (`_usable`) decides in the builder.
_SQL_DOCUMENTS = """
    SELECT d.document_type, d.status, d.expiry_date, d.deleted_at, d.is_archived,
           d.family_member_id, d.ocr_extracted_data
      FROM documents d
     WHERE d.client_id = $1
"""
_SQL_LKPM = """
    SELECT client_id, company_id, year, quarter, status
      FROM lkpm_reports
     WHERE company_id = $1
        OR (company_id IS NULL
            AND client_id IN (SELECT client_id FROM client_company_links WHERE company_id = $1))
"""


async def fetch_immigration_inputs(conn: Any, client_id: int) -> dict[str, Any]:
    client = await conn.fetchrow("SELECT passport_expiry FROM clients WHERE id = $1", client_id)
    documents = await conn.fetch(_SQL_DOCUMENTS, client_id)
    family = await conn.fetch(
        "SELECT current_visa_type, visa_expiry FROM client_family_members WHERE client_id = $1",
        client_id,
    )
    practices = await conn.fetch(_SQL_PRACTICES, client_id)
    try:
        rows = await conn.fetch("SELECT upper(code) AS code, name FROM visa_types")
        catalogue: dict[str, str] | None = {r["code"]: r["name"] for r in rows}
    except Exception as exc:
        logger.warning("recap: visa catalogue lookup failed: %s", type(exc).__name__)
        catalogue = None
    return {
        "documents": [dict(r) for r in documents],
        "family_members": [dict(r) for r in family],
        "practices": [dict(r) for r in practices],
        "passport_expiry": client["passport_expiry"] if client else None,
        "catalogue": catalogue,
    }


async def resolve_company_id(conn: Any, client_id: int, company_id: int | None) -> int | None:
    if company_id is not None:
        return await conn.fetchval(
            "SELECT company_id FROM client_company_links WHERE client_id = $1 AND company_id = $2",
            client_id,
            company_id,
        )
    return await conn.fetchval(
        "SELECT company_id FROM client_company_links WHERE client_id = $1 "
        "ORDER BY is_primary DESC, company_id LIMIT 1",
        client_id,
    )


async def fetch_company_inputs(conn: Any, company_id: int) -> dict[str, Any]:
    """Whichever linked client opens the recap, the inputs are the same (RECAP_SPEC C7)."""
    company = await conn.fetchrow(
        """
        SELECT company_type, kbli_code, akta_pendirian_date, akta_perubahan_date, custom_fields
          FROM companies WHERE id = $1
        """,
        company_id,
    )
    links = await conn.fetch(
        "SELECT client_id, role, shares_count FROM client_company_links WHERE company_id = $1",
        company_id,
    )
    lkpm = None
    try:
        lkpm = [dict(r) for r in await conn.fetch(_SQL_LKPM, company_id)]
    except Exception as exc:
        # The exception text can echo a client value; log the type only.
        logger.warning("recap: lkpm lookup failed: %s", type(exc).__name__)
    company_row = dict(company) if company else {}
    return {
        "company": company_row,
        "links": [dict(r) for r in links],
        "lkpm_rows": lkpm,
        "company_id": company_id,
        "kbli_verdicts": await _kbli_verdicts(conn, company_row),
    }


async def _kbli_verdicts(conn: Any, company: Mapping[str, Any]) -> dict[str, str]:
    """Code -> CANONICAL / ABSENT / UNKNOWN for the codes on file; nothing is asked of the
    catalogue when the set is malformed or over the bound."""
    codes = collect_kbli_codes(company)
    if not codes or len(codes) > MAX_KBLI_CODES:
        return {}
    return {code: catalogue_verdict(await fetch_catalogue_membership(conn, code)) for code in codes}


_locks: dict[tuple[int, str, str], asyncio.Lock] = {}


def _lock_for(key: tuple[int, str, str]) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def _out(
    scope: str,
    row: Mapping[str, Any] | None,
    based: Sequence[str] = (),
) -> dict[str, Any]:
    if row is None or not row["text"]:
        return {
            "scope": scope, "text": None, "generated_at": None, "model": None,
            "stale": False, "source": None, "based_on": [],
        }  # fmt: skip
    return {
        "scope": scope,
        "text": row["text"],
        "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
        "model": row["model"],
        "stale": False,
        "source": "template" if row["model"] == TEMPLATE_MODEL else "ai",
        "based_on": list(based),
    }


def empty_recap(scope: str) -> dict[str, Any]:
    return _out(scope, None)


_SQL_UPDATE_IF_SAME_FACTS = """
    UPDATE client_ai_recaps SET text = $5, model = $6, generated_at = now()
     WHERE client_id = $1 AND scope = $2 AND subject_key = $3 AND facts_hash = $4
"""
_rewriting: set[tuple[int, str, str]] = set()


async def rewrite_in_background(
    pool: Any,
    key: tuple[int, str, str],
    digest: str,
    payload: Mapping[str, Any],
    template: str,
    provider: RecapProvider,
) -> bool:
    """Ask the provider for a rewrite; store it only if the validator accepts it and the
    stored facts are still the ones it was written for. Never raises."""
    client_id, scope, subject_key = key
    try:
        text, ok, _, reasons = await rewrite_with_retry(provider, template, payload, scope)
        if not ok:
            logger.info("recap rewrite rejected for client %s/%s: %s", client_id, scope, reasons)
            return False
        async with pool.acquire() as conn:
            await conn.execute(
                _SQL_UPDATE_IF_SAME_FACTS, client_id, scope, subject_key, digest, text,
                f"{provider.name}:{provider.model}",
            )  # fmt: skip
        return True
    except Exception as exc:
        logger.warning(
            "recap rewrite failed for client %s/%s: %s", client_id, scope, type(exc).__name__
        )
        return False
    finally:
        _rewriting.discard(key)


async def get_recap(
    pool: Any,
    client_id: int,
    scope: str,
    subject_key: str,
    facts: Mapping[str, Any],
    provider: RecapProvider | None = None,
) -> dict[str, Any]:
    """Stale-while-revalidate. ``facts`` are raw builder output; the allow-list runs here.

    Unchanged facts return the cached row. Changed facts store and return the deterministic
    template at once; a configured provider then rewrites it in the background and the
    validated text replaces the template for the next view.
    """
    payload = recap_payload(scope, facts)
    if not payload:
        return _out(scope, None)
    digest = facts_hash(payload)
    key = (client_id, scope, subject_key)
    based = based_on(scope, payload)
    provider = provider if provider is not None else configured_provider()
    async with _lock_for(key):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL_SELECT, client_id, scope, subject_key)
        same = bool(row and row["facts_hash"] == digest and row["text"])
        template = template_recap(scope, payload)
        if not same:
            if not template:
                return _out(scope, None)
            async with pool.acquire() as conn:
                generated_at = await conn.fetchval(
                    _SQL_UPSERT, client_id, scope, subject_key, digest, template, TEMPLATE_MODEL
                )
            row = {
                "text": template, "model": TEMPLATE_MODEL, "generated_at": generated_at,
                "facts_hash": digest,
            }  # fmt: skip
            retry = True
        else:
            assert row is not None
            age = (datetime.now(timezone.utc) - row["generated_at"]).total_seconds()
            retry = row["model"] == TEMPLATE_MODEL and age > REWRITE_RETRY_AFTER_S
        if provider and retry and template and key not in _rewriting:
            _rewriting.add(key)
            spawn(
                rewrite_in_background(pool, key, digest, payload, template, provider),
                name=f"recap-rewrite-{client_id}-{scope}",
            )
    return _out(scope, row, based)
