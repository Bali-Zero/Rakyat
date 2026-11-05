"""Client "needs attention" list for the kita client card.

Pure module: no I/O, no DB, no clock (``today`` is injectable). The profile
endpoint feeds it rows it already fetched and returns the result as two
ADDITIVE keys (``attention`` / ``attention_stats``) next to the legacy
``expiry_alerts`` / ``stats`` that come from the ``client_expiry_alerts_view``
SQL view (same 8/12-month thresholds for everything, no supersession, deleted
documents counted, family items hardcoded as "visa").

Owner's rules (decided 2026-11-03), per PERSON (the client + each family
member):

* Documents that are deleted, archived or rejected never count.
* Immigration documents (stay permits, visit visas, EPO) supersede each other:
  only the LATEST (max expiry; ties -> later issue date, then later upload) is
  live; older ones produce no item. ITK stamps are not part of that race.
* Stay permit (KITAS/KITAP): ``urgent`` at <= 90 days, ``expired`` at <= 0.
* Visit visa: always an item; its date is the expiry of the ITK stamp that
  extends it (else its own expiry with ``note: "itk_missing"``).
* Passport: only the latest per person; ``urgent`` at <= 240 days.
* LKPM windows and SPT deadlines for tax clients. ``overdue`` is time-boxed
  (owner, no noise): only the 30 days after the LKPM window closes or the SPT
  deadline passes, then no item (LKPM: until the next countdown begins). LKPM
  ``overdue`` also needs the company to TRACK LKPM on kita: a filed report
  (submitted/approved, oss_submitted or a receipt) whose window closed within
  the last 12 months; untracked companies get countdown/open/done only.
  Every linked company of a tax client owes its window, with or without a
  report on kita; a client-level report counts for the only linked company.

The permit taxonomy is NOT redefined here: ``permit_label.resolve_permit_label``
supplies the family / index, this module only maps that onto the owner's five
buckets (stay / visit / itk / epo / other) and adds the few keywords the
taxonomy has no family for (VOA, B211, EPO).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.compliance.lkpm_service import QUARTER_DEADLINES
from backend.services.crm.permit_label import (
    FAMILY_EVISA,
    FAMILY_ITK,
    FAMILY_KITAP,
    FAMILY_KITAS,
    resolve_permit_label,
)

WITA = ZoneInfo("Asia/Makassar")

# Thresholds (days)
STAY_URGENT_DAYS = 90
VISIT_URGENT_DAYS = 14
PASSPORT_URGENT_DAYS = 240
LKPM_COUNTDOWN_DAYS = 10
SPT_COUNTDOWN_DAYS = 30
SPT_URGENT_DAYS = 10
OVERDUE_DAYS = 30

SPT_PERSONAL_DEADLINE = (3, 31)
SPT_COMPANY_DEADLINE = (4, 30)

# Classification buckets
STAY = "stay"
VISIT = "visit"
ITK = "itk"
EPO = "epo"

_STATE_ORDER = {
    "expired": 0,
    "overdue": 1,
    "urgent": 2,
    "open": 3,
    "upcoming": 4,
    "done": 5,
}

_GENERIC_TYPES = {"visa", "misc_document", "other", "document", "immigration_document"}

_ITK_RE = re.compile(r"\bitk\b|visit stay|izin tinggal kunjungan")
_EPO_RE = re.compile(r"\bepo\b|exit permit only")
_VISIT_RE = re.compile(
    r"\be?-?voa\b|visa on arrival|\bb[\s-]?211|visa kunjungan|visit visa|visitor visa"
)
_INDEX_RE = re.compile(r"^[A-E]\d")


def today_wita() -> date:
    return datetime.now(WITA).date()


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _ts(value: Any) -> float:
    """Upload time as a sortable number; missing sorts oldest."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float("-inf")


def classify_immigration(
    document_type: str | None,
    visa_text: str | None,
    catalogue: Mapping[str, str] | None = None,
) -> tuple[str, str, str | None] | None:
    """Return ``(bucket, label, code)`` or ``None`` for a non-immigration item.

    ``visa_text`` is the OCR ``visa_type`` (documents) or ``current_visa_type``
    (family members). It is only consulted when ``document_type`` is generic or
    itself classifies, so an RPTKA that quotes a KITAS number stays "other".
    """
    dt = str(document_type or "").strip().lower()
    dt_probe = dt.replace("_", " ")
    text = str(visa_text or "").strip().lower()
    trust_text = dt in _GENERIC_TYPES or not dt
    probe = f"{dt_probe} {text}" if trust_text else dt_probe

    # Only the OCR-text path needs a synthetic raw_response for the taxonomy.
    ocr = {"raw_response": {"visa_type": visa_text}} if visa_text else None
    permit = resolve_permit_label(dt or "visa", ocr, catalogue=catalogue)
    family = permit["permit_family"] if permit else None
    code = permit["permit_code"] if permit else None
    code = code if code and _INDEX_RE.match(code) else None

    if _EPO_RE.search(probe):
        return EPO, "EPO", None
    # The taxonomy files "visit visa" under ITK; the owner's VISIT bucket wins
    # unless the text names the stay stamp itself.
    if _ITK_RE.search(probe) or (family == FAMILY_ITK and not _VISIT_RE.search(probe)):
        return ITK, "ITK", code
    if family == FAMILY_KITAP:
        return STAY, "KITAP", code
    if family == FAMILY_KITAS:
        return STAY, "KITAS", code
    if _VISIT_RE.search(probe):
        return VISIT, "Visit visa", code
    if permit is None:
        return None
    if code and code[0] == "E":
        return STAY, "KITAS", code
    if code:
        return VISIT, "Visit visa", code
    if family == FAMILY_EVISA:
        return VISIT, "Visit visa", None
    return None


@dataclass
class _Imm:
    bucket: str
    label: str
    code: str | None
    expiry: date
    issue: date | None
    uploaded: float
    document_id: int | None


def _days(target: date, today: date) -> int:
    return (target - today).days


def _item(
    kind: str,
    subject: str,
    subject_id: int | None,
    subject_name: str | None,
    label: str,
    state: str,
    *,
    code: str | None = None,
    date_: date | None = None,
    opens_on: date | None = None,
    days: int | None = None,
    note: str | None = None,
    document_id: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "subject": subject,
        "subject_id": subject_id,
        "subject_name": subject_name,
        "label": label,
        "code": code,
        "state": state,
        "date": date_.isoformat() if date_ else None,
        "opens_on": opens_on.isoformat() if opens_on else None,
        "days": days,
        "note": note,
        "document_id": document_id,
    }


def _usable(doc: Mapping[str, Any]) -> bool:
    return (
        doc.get("deleted_at") is None
        and not doc.get("is_archived")
        and str(doc.get("status") or "").lower() not in {"rejected", "deleted"}
    )


def _visa_text(doc: Mapping[str, Any]) -> str | None:
    ocr = doc.get("ocr_extracted_data")
    if isinstance(ocr, dict):
        raw = ocr.get("raw_response")
        if isinstance(raw, dict) and raw.get("visa_type"):
            return str(raw["visa_type"])
    return None


def _person_permit_items(
    subject: str,
    subject_id: int | None,
    name: str | None,
    immigration: list[_Imm],
    today: date,
) -> list[dict[str, Any]]:
    live_pool = [i for i in immigration if i.bucket in (STAY, VISIT, EPO)]
    if not live_pool:
        return []
    live = max(live_pool, key=lambda i: (i.expiry, i.issue or date.min, i.uploaded))
    if live.bucket == EPO:
        return []

    if live.bucket == STAY:
        days = _days(live.expiry, today)
        if days <= 0:
            state = "expired"
        elif days <= STAY_URGENT_DAYS:
            state = "urgent"
        else:
            return []
        return [
            _item(
                "permit",
                subject,
                subject_id,
                name,
                live.label,
                state,
                code=live.code,
                date_=live.expiry,
                days=days,
                document_id=live.document_id,
            )
        ]

    # VISIT: the ITK stamp that extends it decides the date.
    candidates = []
    for itk in (i for i in immigration if i.bucket == ITK):
        if live.issue is not None and itk.issue is not None:
            ok = itk.issue >= live.issue
        elif live.uploaded != float("-inf"):
            ok = itk.uploaded > live.uploaded
        else:
            # A permit read from the family member's own fields has no issue or
            # upload time: an ITK extends it when it runs at least to its end.
            ok = itk.expiry >= live.expiry
        if ok:
            candidates.append(itk)
    if candidates:
        itk = max(candidates, key=lambda i: (i.expiry, i.issue or date.min, i.uploaded))
        target, note, doc_id = itk.expiry, None, itk.document_id
    else:
        target, note, doc_id = live.expiry, "itk_missing", live.document_id
    days = _days(target, today)
    state = "expired" if days <= 0 else "urgent" if days <= VISIT_URGENT_DAYS else "upcoming"
    return [
        _item(
            "permit",
            subject,
            subject_id,
            name,
            live.label,
            state,
            code=live.code,
            date_=target,
            days=days,
            note=note,
            document_id=doc_id,
        )
    ]


def _passport_item(
    subject: str,
    subject_id: int | None,
    name: str | None,
    field_expiry: date | None,
    docs: list[Mapping[str, Any]],
    today: date,
) -> dict[str, Any] | None:
    cands: list[tuple[date, float, int | None]] = []
    if field_expiry:
        cands.append((field_expiry, float("-inf"), None))
    for d in docs:
        exp = _as_date(d.get("expiry_date"))
        if exp:
            cands.append((exp, _ts(d.get("created_at")), d.get("id")))
    if not cands:
        return None
    # Dedupe: the same expiry from the person's field and a document is one
    # passport; the document (carries an id) wins the tie.
    exp, _, doc_id = max(cands, key=lambda c: (c[0], c[2] is not None, c[1]))
    days = _days(exp, today)
    if days <= 0:
        state = "expired"
    elif days <= PASSPORT_URGENT_DAYS:
        state = "urgent"
    else:
        return None
    return _item(
        "passport",
        subject,
        subject_id,
        name,
        "Passport",
        state,
        date_=exp,
        days=days,
        document_id=doc_id,
    )


def _quarter_window(quarter: str, year: int) -> tuple[date, date] | None:
    spec = QUARTER_DEADLINES.get(quarter)
    if not spec:
        return None
    month, day = spec
    close_year = year + 1 if quarter == "Q4" else year
    return date(close_year, month, 1), date(close_year, month, day)


def _lkpm_filed(r: Mapping[str, Any]) -> bool:
    return (
        str(r.get("status") or "").lower() in {"submitted", "approved"}
        or bool(r.get("oss_submitted"))
        or bool(r.get("has_receipts"))
    )


def _filed_recently(r: Mapping[str, Any], today: date) -> bool:
    window = _quarter_window(str(r.get("quarter") or "").upper(), r.get("year") or 0)
    return bool(window) and window[1] >= today - timedelta(days=365)


def _lkpm_items(
    company_names: Mapping[int | None, str | None],
    reports: Sequence[Mapping[str, Any]],
    today: date,
    owing: Sequence[int],
) -> list[dict[str, Any]]:
    # Every company in ``owing`` gets its window even with no report on kita;
    # a client-level report (company_id NULL) belongs to the only linked
    # company when there is exactly one, else it keeps its own group.
    by_company: dict[int | None, list[Mapping[str, Any]]] = {cid: [] for cid in owing}
    sole = next(iter(company_names)) if len(company_names) == 1 else None
    for r in reports:
        cid = r.get("company_id")
        if cid is None and sole is not None:
            cid = sole
        by_company.setdefault(cid, []).append(r)

    items: list[dict[str, Any]] = []
    for company_id, rows in by_company.items():
        # Contiguous periods: [opens - 10d, next quarter's opens - 10d).
        spans = []
        for year in (today.year - 1, today.year, today.year + 1):
            for quarter in ("Q1", "Q2", "Q3", "Q4"):
                w = _quarter_window(quarter, year)
                if w:
                    spans.append((w[0], w[1], quarter, year))
        spans.sort()
        countdown = timedelta(days=LKPM_COUNTDOWN_DAYS)
        chosen = None
        for idx, (opens, close, quarter, year) in enumerate(spans[:-1]):
            if opens - countdown <= today < spans[idx + 1][0] - countdown:
                chosen = (opens, close, quarter, year)
                break
        if not chosen:
            continue
        opens, close, quarter, year = chosen
        done = any(
            str(r.get("quarter") or "").upper() == quarter
            and r.get("year") == year
            and _lkpm_filed(r)
            for r in rows
        )
        # Only companies that TRACK LKPM on kita (a filed report whose window
        # closed within the last 12 months) can be overdue.
        tracked = any(_lkpm_filed(r) and _filed_recently(r, today) for r in rows)
        label = f"LKPM {quarter} {year}"
        name = company_names.get(company_id)
        common = {"code": None, "date_": close}
        if today > close:
            if done or not tracked or today > close + timedelta(days=OVERDUE_DAYS):
                continue
            state, days, opens_on = "overdue", _days(close, today), None
        elif today >= opens:
            state, days, opens_on = ("done" if done else "open"), _days(close, today), None
        else:
            state = "done" if done else "upcoming"
            days = _days(opens, today)
            opens_on = None if done else opens
            if done:
                days = _days(close, today)
        items.append(
            _item(
                "lkpm",
                "company",
                company_id,
                name,
                label,
                state,
                opens_on=opens_on,
                days=days,
                **common,
            )
        )
    return items


def _spt_item(
    kind: str,
    subject: str,
    subject_id: int | None,
    name: str | None,
    label_prefix: str,
    deadline_md: tuple[int, int],
    have_doc: bool,
    today: date,
) -> dict[str, Any] | None:
    fiscal_year = today.year - 1
    deadline = date(today.year, *deadline_md)
    if not (deadline - timedelta(days=SPT_COUNTDOWN_DAYS) <= today <= date(today.year, 12, 31)):
        return None
    days = _days(deadline, today)
    if have_doc:
        state = "done"
    elif days < 0:
        if days < -OVERDUE_DAYS:
            return None
        state = "overdue"
    elif days <= SPT_URGENT_DAYS:
        state = "urgent"
    else:
        state = "upcoming"
    return _item(
        kind,
        subject,
        subject_id,
        name,
        f"{label_prefix} {fiscal_year}",
        state,
        date_=deadline,
        days=days,
    )


# company_documents.document_type values that prove a company's annual SPT:
# the return itself, or its electronic receipt (BPE).
COMPANY_SPT_DOCUMENT_TYPES = frozenset({"spt", "spt_badan", "spt_tahunan_badan", "bpe_spt_badan"})


def build_client_attention(
    *,
    client: Mapping[str, Any],
    family_members: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    company_links: Sequence[Mapping[str, Any]],
    lkpm_reports: Sequence[Mapping[str, Any]],
    company_documents: Sequence[Mapping[str, Any]] = (),
    catalogue: Mapping[str, str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Build ``{"attention": [...], "attention_stats": {...}}`` for one client.

    ``documents`` rows carry ``document_type``, ``status``, ``expiry_date``,
    ``issue_date``, ``fiscal_year``, ``family_member_id``, ``created_at``,
    ``deleted_at``, ``is_archived`` and the raw ``ocr_extracted_data``.
    ``lkpm_reports`` rows carry ``company_id``, ``quarter``, ``year``,
    ``status``, ``oss_submitted`` and ``has_receipts``.
    """
    today = today or today_wita()
    docs = [d for d in documents if _usable(d)]
    items: list[dict[str, Any]] = []

    persons: list[tuple[str, int | None, str | None, int | None, Mapping[str, Any]]] = [
        ("client", None, client.get("full_name"), None, client)
    ]
    for fm in family_members:
        persons.append(("family_member", fm.get("id"), fm.get("full_name"), fm.get("id"), fm))

    for subject, subject_id, name, fm_id, row in persons:
        mine = [d for d in docs if (d.get("family_member_id") or None) == fm_id]
        immigration: list[_Imm] = []
        for d in mine:
            exp = _as_date(d.get("expiry_date"))
            if exp is None:
                continue
            cls = classify_immigration(d.get("document_type"), _visa_text(d), catalogue)
            if cls is None:
                continue
            bucket, label, code = cls
            immigration.append(
                _Imm(
                    bucket,
                    label,
                    code,
                    exp,
                    _as_date(d.get("issue_date")),
                    _ts(d.get("created_at")),
                    d.get("id"),
                )
            )
        if subject == "family_member":
            exp = _as_date(row.get("visa_expiry"))
            cls = classify_immigration("visa", row.get("current_visa_type"), catalogue)
            if exp and cls:
                immigration.append(_Imm(cls[0], cls[1], cls[2], exp, None, float("-inf"), None))
        items += _person_permit_items(subject, subject_id, name, immigration, today)

        passports = [d for d in mine if str(d.get("document_type") or "").lower() == "passport"]
        p = _passport_item(
            subject, subject_id, name, _as_date(row.get("passport_expiry")), passports, today
        )
        if p:
            items.append(p)

    company_names = {
        cl.get("company_id"): cl.get("company_name")
        for cl in company_links
        if cl.get("company_id") is not None
    }
    lkpm_rows = list(lkpm_reports)
    is_tax_client = bool(str(client.get("tax_consultant") or "").strip()) or bool(lkpm_rows)
    # A tax client's linked companies all owe LKPM, reported on kita or not.
    owing = list(company_names) if is_tax_client else []
    items += _lkpm_items(company_names, lkpm_rows, today, owing)

    if is_tax_client:
        fiscal_year = today.year - 1
        client_docs = [d for d in docs if not d.get("family_member_id")]

        def has_spt(doc_type: str) -> bool:
            return any(
                str(d.get("document_type") or "").lower() == doc_type
                and d.get("fiscal_year") == fiscal_year
                for d in client_docs
            )

        spt_p = _spt_item(
            "spt_personal",
            "client",
            None,
            client.get("full_name"),
            "SPT personal",
            SPT_PERSONAL_DEADLINE,
            has_spt("spt_personal"),
            today,
        )
        if spt_p:
            items.append(spt_p)
        # A company's SPT is done only on evidence for THAT company: an SPT row in
        # company_documents for the fiscal year. A client-level "spt" document
        # names no company, so it can stand for one only when the client has
        # exactly one linked company; with two or more it would close the alarm
        # of a company that never filed.
        filed_companies = {
            cd.get("company_id")
            for cd in company_documents
            if str(cd.get("document_type") or "").lower() in COMPANY_SPT_DOCUMENT_TYPES
            and cd.get("fiscal_year") == fiscal_year
        }
        client_level_spt = has_spt("spt") and len(company_links) == 1
        for cl in company_links:
            company_done = client_level_spt or cl.get("company_id") in filed_companies
            spt_c = _spt_item(
                "spt_company",
                "company",
                cl.get("company_id"),
                cl.get("company_name"),
                "SPT company",
                SPT_COMPANY_DEADLINE,
                company_done,
                today,
            )
            if spt_c:
                items.append(spt_c)

    items.sort(
        key=lambda i: (
            _STATE_ORDER[i["state"]],
            i["days"] if i["days"] is not None else 10**9,
        )
    )
    count = {s: sum(1 for i in items if i["state"] == s) for s in _STATE_ORDER}
    stats = {
        "expired": count["expired"] + count["overdue"],
        "urgent": count["urgent"] + count["open"],
        "upcoming": count["upcoming"],
        "done": count["done"],
    }
    return {"attention": items, "attention_stats": stats}
