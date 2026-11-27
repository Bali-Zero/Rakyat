"""Email normalisation helper of the client repository."""

from __future__ import annotations

import pytest

from backend.db.repositories.client_repository import _normalize_email


@pytest.mark.parametrize(
    ("raw_email", "expected"),
    [
        (" Uxxxxxxx2@gmail.com ", "uxxxxxxx2@gmail.com"),
        ("TEAM@BaliZero.COM", "team@balizero.com"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_email_lowercases_and_trims(
    raw_email: str | None,
    expected: str | None,
) -> None:
    assert _normalize_email(raw_email) == expected
