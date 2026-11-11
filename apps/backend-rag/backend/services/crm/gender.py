"""One encoding for clients.gender: "M" / "F".

That is what the stored rows hold (measured read-only on prod 2026-11-11: M and F make up
all but two of the non-null values, no row holds the words) and the only thing the readers
understand (mouth `genderEnum`, `ClientInfoCard`, the portal profile all compare to "M"/"F").
Every writer goes through here so a row is never stored as "male" next to "M".
"""

from __future__ import annotations

from typing import Any

GENDER_VALUES: tuple[str, ...] = ("M", "F")
_ALIASES = {"m": "M", "male": "M", "f": "F", "female": "F"}


def normalize_gender(value: Any) -> str | None:
    """Strict: for request bodies. Empty -> None; unknown -> ValueError (a 422, not a DB 500)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("gender must be a string")
    key = value.strip().lower()
    if not key:
        return None
    result = _ALIASES.get(key)
    if result is None:
        raise ValueError("gender must be M or F (male and female accepted)")
    return result


def normalize_gender_lenient(value: Any) -> str | None:
    """Lenient: for OCR output, where a misread must skip the column, never fail the scan."""
    try:
        return normalize_gender(value)
    except ValueError:
        return None
