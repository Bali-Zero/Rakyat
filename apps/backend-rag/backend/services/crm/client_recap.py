"""AI recap for the kita client card (Immigration / Company tabs).

Pipeline per request: build facts from rows kita already holds -> reduce them
through ONE allow-list (``recap_payload``) -> sha256 of the canonical JSON ->
return the cached row when the hash is unchanged, otherwise render the
deterministic template, upsert, return. No rewrite provider is configured: the
template is the recap, and a caller may still inject a provider. A changed fact
set therefore refreshes the recap by itself.

PII boundary (hard rule): only what ``recap_payload`` lets through reaches the
prompt, and it lets through codes, enums, counts, ISO dates and capital
amounts. Never names (client, family, shareholders, staff, companies),
passport/permit/NIB/NPWP/AHU numbers, addresses, emails, phones, sponsors or
file names. Documents and images are never sent.
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
from zoneinfo import ZoneInfo

from backend.app.utils.json_utils import from_jsonb
from backend.services.common.background import spawn
from backend.services.crm.client_recap_text import (
    RecapProvider,
    based_on,
    configured_provider,
    rewrite_with_retry,
    template_recap,
)
from backend.services.crm.permit_label import (
    FAMILY_EVISA,
    FAMILY_ITK,
    FAMILY_KITAP,
    FAMILY_KITAS,
    resolve_permit_label,
)

logger = logging.getLogger(__name__)

SCOPE_IMMIGRATION = "immigration"
SCOPE_COMPANY = "company"
SCOPES = (SCOPE_IMMIGRATION, SCOPE_COMPANY)

TEMPLATE_MODEL = "template"
REWRITE_RETRY_AFTER_S = 600

# Documents that are one person's stay-permit race (latest wins); ITK stamps
# and MERP re-entry permits are not part of it.
_STAY_RACE_FAMILIES = {FAMILY_KITAS, FAMILY_KITAP, FAMILY_EVISA}
_CLOSED_PRACTICE_STATUSES = {"completed", "cancelled", "canceled", "closed", "done", "rejected"}
# practice_types.category values that are immigration work. The Immigration
# recap calls what it lists "immigration practices", so a tax, corporate or
# property practice must never be counted there.
IMMIGRATION_PRACTICE_CATEGORIES = frozenset({"visa", "visa_extension", "kitap"})
_RELATIONSHIPS = {"spouse", "child", "parent", "sibling", "other"}

_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 /_-]{0,15}$")
_ENUM_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_PRACTICE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_KBLI_RE = re.compile(r"^\d{4,5}$")
_TITLE_RE = re.compile(r"^[A-Za-z0-9 ()/.,&'+-]{1,120}$")
_ISSUE_DATE_KEYS = ("issue_date", "date_of_issue", "issued_date", "tanggal_terbit")

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
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            return None
    return None


def _s_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\d{1,18}", v.strip()):
        return int(v.strip())
    return None


_FAMILY_CODES = {"KITAS / ITAS": "KITAS", "KITAP / ITAP": "KITAP"}


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
    return v.strip().lower() if isinstance(v, str) and _ENUM_RE.match(v.strip().lower()) else None


_SLOT_BANNED_RE = re.compile(
    r"\b(ignore|instructions?|disregard|override|prompt|assistant|rewrite|you|your|draft|https?|www)\b",
    re.IGNORECASE,
)


def _s_slot(v: Any, maxlen: int = 60) -> str | None:
    """Free-text slot that comes from a catalogue/OCR value: shape-checked, length-bounded and
    stripped of anything that reads as an instruction or a meta phrase."""
    t = _s_title(v)
    if not t:
        return None
    t = re.sub(r"\s+", " ", t)
    return None if len(t) > maxlen or _SLOT_BANNED_RE.search(t) else t


def _s_title(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and _TITLE_RE.match(v.strip()) else None


def _s_practice_code(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and _PRACTICE_CODE_RE.match(v.strip()) else None


def _s_kbli(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and _KBLI_RE.match(v.strip()) else None


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {})}


def _valid(v: Any, fn) -> list[Any]:
    if not isinstance(v, Sequence) or isinstance(v, (str, bytes)):
        return []
    out = [fn(x) for x in v]
    return [x for x in out if x not in (None, {}, [])]


def _list(v: Any, fn, limit: int) -> list[Any]:
    return _valid(v, fn)[:limit]


def _person_permit(v: Any) -> dict[str, Any]:
    v = v if isinstance(v, Mapping) else {}
    return _prune(
        {
            "relationship": _s_enum(v.get("relationship"))
            if v.get("relationship") in _RELATIONSHIPS
            else None,
            "permit_code": _s_code(v.get("permit_code")),
            "permit_expiry_date": _s_date(v.get("permit_expiry_date")),
        }
    )


def _practice(v: Any) -> dict[str, Any]:
    v = v if isinstance(v, Mapping) else {}
    return _prune({"type": _s_slot(v.get("type"))})


def _mapping(v: Any) -> Mapping[str, Any]:
    return v if isinstance(v, Mapping) else {}


def _kbli_item(v: Any) -> dict[str, Any]:
    v = v if isinstance(v, Mapping) else {}
    in_2025 = v.get("in_2025")
    return _prune(
        {
            "code": _s_kbli(v.get("code")),
            "title": _s_slot(v.get("title"), 100),
            "in_2025": in_2025 if isinstance(in_2025, bool) else None,
        }
    )


def recap_payload(scope: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """THE allow-list: the only function whose output reaches the prompt.

    Any key not named here is dropped, every value is re-validated against a
    strict shape (code / enum / ISO date / integer), so a free-text field
    (name, number, address, ...) cannot pass even if a builder leaked it.
    """
    f = facts if isinstance(facts, Mapping) else {}
    if scope == SCOPE_IMMIGRATION:
        cp = _mapping(f.get("current_permit"))
        cp_expiry = _s_date(cp.get("expiry_date"))
        practices = _valid(f.get("open_practices"), _practice)
        family = _valid(f.get("family_permits"), _person_permit)
        # The lists below are capped; the counts are taken before the cap so the
        # recap never reports the cap as the total.
        family_early = sum(
            1
            for x in family
            if cp_expiry and x.get("permit_expiry_date") and x["permit_expiry_date"] < cp_expiry
        )
        return _prune(
            {
                "current_permit": _prune(
                    {
                        "code": _s_code(cp.get("code")),
                        "official_name": _s_slot(clean_permit_name(cp.get("official_name")), 80),
                        "status": _s_status(cp.get("status")),
                        "issue_date": _s_date(cp.get("issue_date")),
                        "expiry_date": cp_expiry,
                    }
                ),
                "itk_expiry_date": _s_date(f.get("itk_expiry_date")),
                "itk_status": _s_status(f.get("itk_status")),
                "previous_permits_count": _s_int(f.get("previous_permits_count")),
                "previous_permit_codes": _list(f.get("previous_permit_codes"), _s_code, 10),
                "passport_expiry_date": _s_date(f.get("passport_expiry_date")),
                "passport_status": _s_status(f.get("passport_status")),
                "family_members_count": _s_int(f.get("family_members_count")),
                "family_permits": family[:10],
                "family_permits_end_early_count": family_early or None,
                "open_practices": practices[:10],
                "open_practices_count": len(practices) or None,
            }
        )
    if scope == SCOPE_COMPANY:
        lk = _mapping(f.get("lkpm_latest"))
        kbli = _valid(f.get("kbli"), _kbli_item)
        kbli_2020 = sum(1 for k in kbli if k.get("in_2025") is False)
        return _prune(
            {
                "legal_form": _s_slot(f.get("legal_form")),
                "established_date": _s_date(f.get("established_date")),
                "authorized_capital_idr": _s_int(f.get("authorized_capital_idr")),
                "paid_up_capital_idr": _s_int(f.get("paid_up_capital_idr")),
                "total_shares": _s_int(f.get("total_shares")),
                "shareholders_count": _s_int(f.get("shareholders_count")),
                "directors_count": _s_int(f.get("directors_count")),
                "kbli": kbli[:15],
                "kbli_count": len(kbli) or None,
                "kbli_2020_count": kbli_2020 or None,
                "lkpm_latest": _prune(
                    {
                        "year": _s_int(lk.get("year")),
                        "quarter": _s_code(lk.get("quarter")),
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


def _issue_date(ocr: Any) -> str | None:
    raw = _mapping(ocr).get("raw_response")
    if not isinstance(raw, dict):
        return None
    for key in _ISSUE_DATE_KEYS:
        d = _s_date(raw.get(key))
        if d:
            return d
    return None


def build_immigration_facts(
    *,
    documents: Sequence[Mapping[str, Any]],
    family_members: Sequence[Mapping[str, Any]],
    practices: Sequence[Mapping[str, Any]],
    passport_expiry: Any,
    catalogue: Mapping[str, str] | None,
    today: date | None = None,
) -> dict[str, Any]:
    """Minimal immigration facts. Documents are the client's own (family_member_id
    NULL); deleted/archived/rejected rows are expected to be filtered by the caller
    (and rejected ones are skipped here as well)."""
    stay: list[tuple[Any, ...]] = []
    itk_dates: list[str] = []
    for d in documents:
        if (
            d.get("family_member_id") is not None
            or str(d.get("status") or "").lower() == "rejected"
        ):
            continue
        ocr = from_jsonb(d.get("ocr_extracted_data")) if d.get("ocr_extracted_data") else None
        permit = resolve_permit_label(d.get("document_type"), ocr, catalogue=catalogue)
        if not permit:
            continue
        expiry = _s_date(d.get("expiry_date"))
        if permit["permit_family"] == FAMILY_ITK:
            if expiry:
                itk_dates.append(expiry)
        elif permit["permit_family"] in _STAY_RACE_FAMILIES:
            issue = _issue_date(ocr)
            created = _s_date(d.get("created_at")) or ""
            stay.append((expiry or "", issue or "", created, permit["permit_code"], issue, expiry))
    stay.sort(reverse=True)
    today = today or datetime.now(ZoneInfo("Asia/Makassar")).date()
    facts: dict[str, Any] = {}
    if stay:
        top = stay[0]
        code = top[3]
        name = (catalogue or {}).get(str(code).upper())
        permit_expiry = _s_date(top[5])
        facts["current_permit"] = {
            "code": code,
            "official_name": name,
            "issue_date": top[4],
            "expiry_date": top[5],
            "status": "expired"
            if permit_expiry and date.fromisoformat(permit_expiry) < today
            else "valid",
        }
        facts["previous_permits_count"] = len(stay) - 1
        facts["previous_permit_codes"] = sorted({s[3] for s in stay[1:]})
    if stay:
        issue, expiry = stay[0][4], stay[0][5]
        mine = [
            d for d in itk_dates
            if (issue and d >= issue)
            or (not issue and date.fromisoformat(d) >= today and (not expiry or d <= expiry))
        ]  # fmt: skip
        if mine:
            facts["itk_expiry_date"] = max(mine)
            facts["itk_status"] = "expired" if date.fromisoformat(max(mine)) < today else "valid"
    facts["passport_expiry_date"] = _s_date(passport_expiry)
    if facts["passport_expiry_date"]:
        facts["passport_status"] = (
            "expired" if date.fromisoformat(facts["passport_expiry_date"]) < today else "valid"
        )
    facts["family_members_count"] = len(family_members)
    fam: list[dict[str, Any]] = []
    for fm in family_members:
        p = resolve_permit_label(fm.get("current_visa_type"), None, catalogue=catalogue)
        fam.append(
            {
                "relationship": fm.get("relationship"),
                "permit_code": p["permit_code"] if p else None,
                "permit_expiry_date": fm.get("visa_expiry"),
            }
        )
    facts["family_permits"] = fam
    facts["open_practices"] = [
        {"type": p.get("practice_type_name"), "status": p.get("status")}
        for p in practices
        if str(p.get("status") or "").lower() not in _CLOSED_PRACTICE_STATUSES
        and p.get("practice_type_name")
        and str(p.get("practice_category") or "").lower() in IMMIGRATION_PRACTICE_CATEGORIES
    ]
    return facts


def _cf_value(cf: Mapping[str, Any], key: str) -> Any:
    v = cf.get(key)
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def build_company_facts(
    *,
    company: Mapping[str, Any],
    links: Sequence[Mapping[str, Any]],
    lkpm_latest: Mapping[str, Any] | None,
    kbli_in_2025: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    cf = from_jsonb(company.get("custom_fields")) if company.get("custom_fields") else {}
    cf = cf if isinstance(cf, dict) else {}
    holders = [
        link
        for link in links
        if "shareholder" in str(link.get("role") or "").lower() or link.get("shares_count")
    ]
    cf_holders = _cf_value(cf, "shareholders")
    holders_count = len(holders) or (len(cf_holders) if isinstance(cf_holders, list) else None)
    directors = [
        link for link in links if re.search(r"director|direktur", str(link.get("role") or ""), re.I)
    ]
    kbli: list[dict[str, Any]] = []
    primary = _s_kbli(str(company.get("kbli_code") or "").strip())
    if primary:
        kbli.append({"code": primary, "title": company.get("kbli_description")})
    extra = _cf_value(cf, "kbli_codes")
    for code in extra if isinstance(extra, list) else []:
        c = _s_kbli(str(code).strip())
        if c and c != primary:
            kbli.append({"code": c})
    for item in kbli:
        if kbli_in_2025 and item["code"] in kbli_in_2025:
            item["in_2025"] = kbli_in_2025[item["code"]]
    amendment = company.get("akta_perubahan_date") or _cf_value(cf, "akta_date")
    return {
        "legal_form": company.get("company_type"),
        "established_date": company.get("akta_pendirian_date"),
        "authorized_capital_idr": _cf_value(cf, "authorized_capital"),
        "paid_up_capital_idr": _cf_value(cf, "paid_up_capital"),
        "total_shares": _cf_value(cf, "total_shares"),
        "shareholders_count": holders_count,
        "directors_count": len(directors) or None,
        "kbli": kbli,
        "lkpm_latest": dict(lkpm_latest) if lkpm_latest else None,
        "last_amendment_date": amendment,
    }


# ── DB access + cache ───────────────────────────────────────────────────────


async def fetch_immigration_inputs(conn: Any, client_id: int) -> dict[str, Any]:
    from backend.services.crm.admin_document_visibility import (
        admin_document_not_team_deleted_clause,
    )

    client = await conn.fetchrow("SELECT passport_expiry FROM clients WHERE id = $1", client_id)
    documents = await conn.fetch(
        f"""
        SELECT d.document_type, d.status, d.expiry_date, d.created_at,
               d.family_member_id, d.ocr_extracted_data
          FROM documents d
         WHERE d.client_id = $1
           AND d.deleted_at IS NULL
           AND (d.is_archived IS NULL OR d.is_archived = false)
           AND {admin_document_not_team_deleted_clause("d")}
        """,
        client_id,
    )
    family = await conn.fetch(
        "SELECT relationship, current_visa_type, visa_expiry FROM client_family_members WHERE client_id = $1",
        client_id,
    )
    practices = await conn.fetch(
        """
        SELECT p.status, pt.name AS practice_type_name, pt.category AS practice_category
          FROM practices p
          JOIN practice_types pt ON p.practice_type_id = pt.id
         WHERE p.client_id = $1
           AND pt.category = ANY($2::text[])
        """,
        client_id,
        sorted(IMMIGRATION_PRACTICE_CATEGORIES),
    )
    try:
        rows = await conn.fetch("SELECT upper(code) AS code, name FROM visa_types")
        catalogue: dict[str, str] | None = {r["code"]: r["name"] for r in rows}
    except Exception:
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


async def fetch_company_inputs(conn: Any, client_id: int, company_id: int) -> dict[str, Any]:
    company = await conn.fetchrow(
        """
        SELECT company_type, kbli_code, kbli_description, akta_pendirian_date,
               akta_perubahan_date, custom_fields
          FROM companies WHERE id = $1
        """,
        company_id,
    )
    links = await conn.fetch(
        "SELECT role, shares_count FROM client_company_links WHERE company_id = $1", company_id
    )
    lkpm = None
    try:
        row = await conn.fetchrow(
            # The company's own reports whoever filed them; an unattributed report
            # counts only for a client with exactly one company.
            "SELECT year, quarter, status FROM lkpm_reports WHERE company_id = $2 "
            "OR (client_id = $1 AND company_id IS NULL AND "
            "(SELECT count(*) FROM client_company_links WHERE client_id = $1) = 1) "
            "ORDER BY year DESC, quarter DESC LIMIT 1",
            client_id,
            company_id,
        )
        lkpm = dict(row) if row else None
    except Exception as exc:
        # The exception text can echo a client value; log the type only.
        logger.warning("recap: lkpm lookup failed: %s", type(exc).__name__)
    return {
        "company": dict(company) if company else {},
        "links": [dict(r) for r in links],
        "lkpm_latest": lkpm,
        "kbli_in_2025": await _kbli_in_2025(conn, dict(company) if company else {}),
    }


async def _kbli_in_2025(conn: Any, company: Mapping[str, Any]) -> dict[str, bool]:
    """Code -> in the KBLI 2025 catalogue, from ``kbli_documents`` (the service the
    kbli tools already use). UNKNOWN verdicts are omitted: absence of evidence says nothing."""
    from backend.services.kbli_catalogue_membership import (
        ABSENT,
        CANONICAL,
        catalogue_verdict,
        fetch_catalogue_membership,
    )

    cf = from_jsonb(company.get("custom_fields")) if company.get("custom_fields") else {}
    cf = cf if isinstance(cf, dict) else {}
    extra = _cf_value(cf, "kbli_codes")
    codes = [company.get("kbli_code")] + (extra if isinstance(extra, list) else [])
    out: dict[str, bool] = {}
    for raw in codes[:15]:
        code = _s_kbli(str(raw or "").strip())
        if not code or code in out:
            continue
        verdict = catalogue_verdict(await fetch_catalogue_membership(conn, code))
        if verdict in (CANONICAL, ABSENT):
            out[code] = verdict == CANONICAL
    return out


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
