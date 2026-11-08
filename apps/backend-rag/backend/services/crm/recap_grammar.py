"""Strict input grammars for the kita client recap (RECAP_SPEC.md, "Grammars").

Every parser answers a value or ``None``. ``None`` means *unparseable*: the caller omits the
statement, it never repairs, strips, rounds or guesses. Nothing here logs, and nothing here
raises on a bad value, so a client value cannot reach a log line or a traceback from this module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

WITA: Final = ZoneInfo("Asia/Makassar")

_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INT_PLAIN_RE = re.compile(r"0|[1-9][0-9]*")
_INT_GROUPED_RE = {
    ".": re.compile(r"[1-9][0-9]{0,2}(?:\.[0-9]{3})+"),
    ",": re.compile(r"[1-9][0-9]{0,2}(?:,[0-9]{3})+"),
}
_KBLI_RE = re.compile(r"[0-9]{5}")
_QUARTER_RE = re.compile(r"Q[1-4]")


def today_wita() -> date:
    return datetime.now(WITA).date()


def parse_date(v: Any) -> date | None:
    """A ``date`` (not a datetime), an aware datetime reduced to its WITA date, or ``YYYY-MM-DD``."""
    if isinstance(v, datetime):
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            return None
        return v.astimezone(WITA).date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and _ISO_DATE_RE.fullmatch(v):
        try:
            return date(int(v[:4]), int(v[5:7]), int(v[8:10]))
        except ValueError:
            return None
    return None


def parse_nonneg_int(v: Any) -> int | None:
    """A non-bool ``int`` >= 0, or ASCII digits ungrouped (no leading zero) or grouped in threes."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if not isinstance(v, str):
        return None
    if _INT_PLAIN_RE.fullmatch(v):
        return int(v)
    for sep, rx in _INT_GROUPED_RE.items():
        if rx.fullmatch(v):
            return int(v.replace(sep, ""))
    return None


def parse_kbli_code(v: Any) -> str | None:
    return v if isinstance(v, str) and _KBLI_RE.fullmatch(v) else None


CODE_LIST_EMPTY: Final = "empty"
CODE_LIST_ACCEPTED: Final = "accepted"
CODE_LIST_MALFORMED: Final = "malformed"


def parse_code_list(container: Any, key: str) -> tuple[str, list[str]]:
    """The value at ``container[key]``: (state, strings). Elements are NOT code-checked here."""
    if container is None:
        return CODE_LIST_EMPTY, []
    if not isinstance(container, Mapping):
        return CODE_LIST_MALFORMED, []
    v = container.get(key)
    if v is None:
        return CODE_LIST_EMPTY, []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return CODE_LIST_MALFORMED, []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        return CODE_LIST_MALFORMED, []
    return (CODE_LIST_ACCEPTED, list(v)) if v else (CODE_LIST_EMPTY, [])


def parse_lkpm_period(year: Any, quarter: Any) -> tuple[int, int] | None:
    """(year, quarter number) or ``None``: year 2000-2100, quarter ``Q1``..``Q4`` or int 1..4."""
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
        return None
    if isinstance(quarter, bool):
        return None
    if isinstance(quarter, int):
        return (year, quarter) if 1 <= quarter <= 4 else None
    if isinstance(quarter, str) and _QUARTER_RE.fullmatch(quarter):
        return year, int(quarter[1])
    return None


def closed_key(v: Any) -> str | None:
    """Trim and lower-case a string for a closed-set match; any non-string is ``None``."""
    return v.strip().lower() if isinstance(v, str) else None


def classify(v: Any, sets: Mapping[str, frozenset[str]], default: str = "unknown") -> str:
    key = closed_key(v)
    if key is not None:
        for name, members in sets.items():
            if key in members:
                return name
    return default


def decode_jsonb(v: Any) -> tuple[bool, Any]:
    """(ok, value) for a JSONB column: ``None`` is absent, a str decodes once, else as is."""
    if v is None:
        return True, None
    if isinstance(v, str):
        try:
            return True, json.loads(v)
        except ValueError:
            return False, None
    return True, v


# ── closed sets (measured on production 2026-11-05, RECAP_SPEC.md "Closed sets") ─────────────

IMMIGRATION: Final = "immigration"
NON_IMMIGRATION: Final = "non_immigration"
OPEN: Final = "open"
CLOSED: Final = "closed"
SHAREHOLDER: Final = "shareholder"
DIRECTOR: Final = "director"
OTHER_KNOWN: Final = "other_known"
UNKNOWN: Final = "unknown"

PRACTICE_CATEGORIES: Final[dict[str, frozenset[str]]] = {
    IMMIGRATION: frozenset(
        {"kitas", "kitap", "visa", "visa_extension", "single_entry_visa", "multiple_entry_visa"}
    ),
    NON_IMMIGRATION: frozenset(
        {
            "company",
            "corporate",
            "property",
            "tax",
            "tax_annual",
            "tax_monthly",
            "tax_monthly_bundled",
            "tax_standalone",
        }
    ),
}
PRACTICE_STATUSES: Final[dict[str, frozenset[str]]] = {
    OPEN: frozenset({"on_process", "sending_invoice", "waiting_documents", "inquiry"}),
    CLOSED: frozenset({"completed", "cancelled", "canceled", "closed", "done", "rejected"}),
}
LINK_ROLES: Final[dict[str, frozenset[str]]] = {
    SHAREHOLDER: frozenset({"shareholder", "pemegang saham"}),
    DIRECTOR: frozenset(
        {
            "director",
            "direktur",
            "direktur utama",
            "director_utama",
            "president_director",
            "presiden direktur",
        }
    ),
    OTHER_KNOWN: frozenset(
        {"commissioner", "komisaris", "komisaris utama", "presiden komisaris", "contact"}
    ),
}
LKPM_STATUSES: Final = frozenset({"draft", "pending", "submitted", "approved"})
LEGAL_FORMS: Final = frozenset(
    {"pt pma", "pt", "pma", "cv", "yayasan", "ud", "pmdn non fasilitas", "swasta nasional"}
)


def legal_form(v: Any) -> str | None:
    """The recorded legal form, trimmed, when it is in the closed set."""
    key = closed_key(v)
    return v.strip() if key in LEGAL_FORMS else None  # type: ignore[union-attr]


def lkpm_status(v: Any) -> str | None:
    key = closed_key(v)
    return key if key in LKPM_STATUSES else None
