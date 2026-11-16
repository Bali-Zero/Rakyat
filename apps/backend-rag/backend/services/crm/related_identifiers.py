"""Canonical form and validator of the identifier keys of related subjects.

One canonical function and one validator per type (norm_version 1). The same
canonical form is also available as a SQL expression, so that a value stored
in a CRM column is compared in the form the registry stores.
"""

from __future__ import annotations

import re

NORM_VERSION = 1

PERSON_TYPES = frozenset({"passport", "permit", "nik"})
COMPANY_TYPES = frozenset({"npwp", "nib", "ahu"})
ID_TYPES = PERSON_TYPES | COMPANY_TYPES

MAX_RAW_LENGTH = 64

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_NON_DIGIT = re.compile(r"[^0-9]")
_PERMIT_A = re.compile(r"[0-9A-Z]{9,17}[A-Z]")
_PERMIT_A_PREFIX = re.compile(r"[0-9][A-Z][0-9]|[A-Z][0-9]{2}[A-Z]")
_PERMIT_B = re.compile(r"[A-Z][0-9]{2}[A-Z0-9]{13,18}")
_AHU_LONG = re.compile(r"AHU-([0-9]+)\.AH\.([0-9]{2})\.([0-9]{2})\.TAHUN ([0-9]{4})")
_AHU_SHORT = re.compile(r"([0-9]+)\.([0-9]{2})\.([0-9]{2})\.([0-9]{4})")
_ASCII_WS = " \t\r\n\f\v"


class InvalidIdentifier(ValueError):
    """A value is not a valid key of its type. The message never carries the value."""


def kind_of(id_type: str) -> str:
    """The subject kind a type belongs to."""
    if id_type in PERSON_TYPES:
        return "person"
    if id_type in COMPANY_TYPES:
        return "company"
    raise InvalidIdentifier("unknown identifier type")


def _alnum(value: str) -> str:
    return _NON_ALNUM.sub("", value).upper()


def _digits(value: str) -> str:
    return _NON_DIGIT.sub("", value)


def _ahu(value: str) -> str | None:
    text = value.strip(_ASCII_WS)
    if not text.isascii():
        return None
    text = text.upper()
    match = _AHU_LONG.fullmatch(text) or _AHU_SHORT.fullmatch(text)
    if match is None:
        return None
    number, aa, bb, year = match.groups()
    number = number.lstrip("0") or "0"
    return f"{number}.{aa}.{bb}.{year}"


def canonical_form(id_type: str, value: str) -> str:
    """The canonical form of a value, WITHOUT checking it is valid.

    An AHU value that does not fit the accepted layouts has no canonical form;
    its canonical form is the empty string, which no validator accepts.
    """
    if id_type in ("passport", "permit"):
        return _alnum(value)
    if id_type in ("nik", "nib"):
        return _digits(value)
    if id_type == "npwp":
        digits = _digits(value)
        return "0" + digits if len(digits) == 15 else digits
    if id_type == "ahu":
        return _ahu(value) or ""
    raise InvalidIdentifier("unknown identifier type")


def is_valid(id_type: str, canonical: str) -> bool:
    """Validator of a CANONICAL value (the output of ``canonical_form``)."""
    if id_type == "passport":
        return 6 <= len(canonical) <= 12 and any(c.isdigit() for c in canonical)
    if id_type == "permit":
        if _PERMIT_A.fullmatch(canonical) and _PERMIT_A_PREFIX.match(canonical):
            return True
        return _PERMIT_B.fullmatch(canonical) is not None
    if id_type == "nik":
        return len(canonical) == 16
    if id_type == "npwp":
        return len(canonical) == 16
    if id_type == "nib":
        return len(canonical) == 13
    if id_type == "ahu":
        return _AHU_SHORT.fullmatch(canonical) is not None and canonical.isascii()
    return False


def canonicalize(id_type: str, value: str) -> str:
    """Canonical form of a value, or ``InvalidIdentifier`` when it is not a valid key."""
    if id_type not in ID_TYPES:
        raise InvalidIdentifier("unknown identifier type")
    core = value.strip(_ASCII_WS) if isinstance(value, str) else ""
    if not core or len(core) > MAX_RAW_LENGTH:
        raise InvalidIdentifier("invalid identifier value")
    if any(ord(c) < 32 or ord(c) == 127 for c in core):
        raise InvalidIdentifier("invalid identifier value")
    canonical = canonical_form(id_type, value)
    if not is_valid(id_type, canonical):
        raise InvalidIdentifier("invalid identifier value")
    return canonical


# SQL-side canonical forms for the CRM comparisons. Only the types a CRM column
# can be compared on have one.
_SQL_DIGITS = "regexp_replace({col}, '[^0-9]', '', 'g')"
_SQL_FORMS = {
    "passport": "upper(regexp_replace({col}, '[^A-Za-z0-9]', '', 'g'))",
    "nib": _SQL_DIGITS,
    "npwp": (
        "(CASE WHEN length(" + _SQL_DIGITS + ") = 15 THEN '0' || " + _SQL_DIGITS + " ELSE "
        + _SQL_DIGITS + " END)"
    ),
}  # fmt: skip


def sql_canonical(id_type: str, column: str) -> str:
    """SQL expression applying the canonical form of ``id_type`` to ``column``.

    ``column`` is a trusted column name written in code, never a request value.
    """
    return _SQL_FORMS[id_type].format(col=column)
