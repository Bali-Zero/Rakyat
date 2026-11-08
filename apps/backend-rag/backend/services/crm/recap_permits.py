"""Permit documents for the kita client recap (RECAP_SPEC.md, "Permit documents").

The recap does not take a family or a code from ``resolve_permit_label``: that resolver keeps the
first family keyword and the first parsable token, so it can hide an alternative. This module reads
the same two signals and keeps every family and every code-shaped token it sees. A document it
cannot classify with certainty is ``classifiable=False``, and the caller omits what depends on it.

Pure functions, no I/O, no logging: a value read from a document never leaves through here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from backend.services.crm.permit_label import (
    FAMILY_EVISA,
    FAMILY_KITAP,
    FAMILY_KITAS,
    code_shaped_tokens,
    permit_families,
)
from backend.services.crm.recap_grammar import decode_jsonb, parse_date

ISSUE_DATE_KEYS: Final = ("issue_date", "date_of_issue", "issued_date", "tanggal_terbit")
STAY_FAMILIES: Final = frozenset({FAMILY_KITAS, FAMILY_KITAP, FAMILY_EVISA})
GENERIC_CODE: Final = {FAMILY_KITAS: "KITAS", FAMILY_KITAP: "KITAP", FAMILY_EVISA: "EVISA"}
_PERMIT_DOCUMENT_TYPES: Final = frozenset({"visa", "immigration_document"})

ISSUE_NONE: Final = "none"
ISSUE_CONFLICTING: Final = "conflicting"
ISSUE_DATE: Final = "date"


@dataclass(frozen=True)
class PermitDoc:
    classifiable: bool
    family: str | None
    code: str | None  # a precise visa_types code, or the family's generic code
    precise: bool
    expiry: date | None
    issue_state: str
    issue: date | None


def issue_date_of(raw_response: Mapping[str, Any]) -> tuple[str, date | None]:
    """The four-alias consensus: none / one date / conflicting (unparseable or differing)."""
    present = [raw_response[k] for k in ISSUE_DATE_KEYS if k in raw_response]
    if not present:
        return ISSUE_NONE, None
    parsed = [parse_date(v) for v in present]
    if any(d is None for d in parsed) or len(set(parsed)) != 1:
        return ISSUE_CONFLICTING, None
    return ISSUE_DATE, parsed[0]


def _signals(doc: Mapping[str, Any]) -> tuple[list[str], bool, Mapping[str, Any]]:
    """(string signals with `_` read as a space, any malformed, raw_response if a dict)."""
    texts: list[str] = []
    malformed = False
    dtype = doc.get("document_type")
    if isinstance(dtype, str):
        texts.append(dtype.replace("_", " "))
    elif dtype is not None:
        malformed = True
    raw: Mapping[str, Any] = {}
    ok, ocr = decode_jsonb(doc.get("ocr_extracted_data"))
    if not ok:
        return texts, True, raw
    if ocr is not None:
        if not isinstance(ocr, dict):
            return texts, True, raw
        resp = ocr.get("raw_response")
        if resp is not None:
            if not isinstance(resp, dict):
                return texts, True, raw
            raw = resp
            if "visa_type" in resp and resp["visa_type"] is not None:
                if isinstance(resp["visa_type"], str):
                    texts.append(resp["visa_type"].replace("_", " "))
                else:
                    malformed = True
    return texts, malformed, raw


def classify_document(
    doc: Mapping[str, Any], catalogue: Mapping[str, str] | None
) -> PermitDoc | None:
    """``None`` when the document is not a permit document; else its (maybe unclassifiable) record."""
    catalogue = catalogue or {}
    texts, malformed, raw = _signals(doc)
    families: set[str] = set()
    for text in texts:
        families |= permit_families(text)
    codes: set[str] = set()
    unresolved = False
    shaped = False
    for text in texts:
        for parses in code_shaped_tokens(text):
            shaped = True
            known = [code for code, _ in parses if code in catalogue]
            if len(known) == 1:
                codes.add(known[0])
            else:
                unresolved = True
    dtype = doc.get("document_type")
    generic_type = isinstance(dtype, str) and dtype.strip().lower() in _PERMIT_DOCUMENT_TYPES
    if not (families or shaped or malformed or generic_type):
        return None

    expiry = parse_date(doc.get("expiry_date"))
    issue_state, issue = issue_date_of(raw)
    if malformed or unresolved or len(codes) > 1 or len(families) != 1:
        return PermitDoc(False, None, None, False, expiry, issue_state, issue)
    family = next(iter(families))
    if codes:
        return PermitDoc(True, family, next(iter(codes)), True, expiry, issue_state, issue)
    code = GENERIC_CODE.get(family)
    # ITK and MERP carry no generic code; they never feed the code-bearing statements.
    return PermitDoc(True, family, code, False, expiry, issue_state, issue)


@dataclass(frozen=True)
class CurrentPermit:
    code: str
    expiry: date
    issue_state: str
    issue: date | None


def pick_current_permit(candidates: list[PermitDoc]) -> CurrentPermit | None:
    """I1/I2: the latest-expiry candidate, with the tie rule. ``None`` omits the clause.

    The caller has already checked that candidates exist, are classifiable and are dated.
    """
    latest = max(c.expiry for c in candidates if c.expiry is not None)
    tied = [c for c in candidates if c.expiry == latest]
    if len({c.family for c in tied}) != 1:
        return None
    precise = {c.code for c in tied if c.precise}
    if len(precise) > 1:
        return None
    if precise:
        code = next(iter(precise))
        same = [c for c in tied if c.precise and c.code == code]
    else:
        code = tied[0].code
        same = tied
    assert code is not None  # candidates are stay-family documents: always a code
    if any(c.issue_state == ISSUE_CONFLICTING for c in same):
        return CurrentPermit(code, latest, ISSUE_CONFLICTING, None)
    if all(c.issue_state == ISSUE_DATE and c.issue == same[0].issue for c in same):
        return CurrentPermit(code, latest, ISSUE_DATE, same[0].issue)
    return CurrentPermit(code, latest, ISSUE_NONE, None)
