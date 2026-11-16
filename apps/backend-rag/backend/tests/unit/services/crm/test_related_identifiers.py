"""Canonical form and validator of related-subject identifier keys (norm_version 1).

Synthetic vectors only: at least three accepted and three rejected per type.
"""

from __future__ import annotations

import pytest

from backend.services.crm.related_identifiers import (
    NORM_VERSION,
    InvalidIdentifier,
    canonical_form,
    canonicalize,
    kind_of,
    sql_canonical,
)

ACCEPTED = {
    "passport": [
        ("ab 123-456", "AB123456"),
        ("ZZ0000001", "ZZ0000001"),
        ("x1.234.567", "X1234567"),
        ("123456", "123456"),
    ],
    "permit": [
        ("1A2345678B", "1A2345678B"),
        ("a12b-3456789c", "A12B3456789C"),
        ("C12 1234567890123", "C121234567890123"),
        ("9Z8ABCDEFGHIJKLMNZ", "9Z8ABCDEFGHIJKLMNZ"),
    ],
    "nik": [
        ("3201234567890123", "3201234567890123"),
        ("3201 2345 6789 0123", "3201234567890123"),
        ("0000000000000001", "0000000000000001"),
    ],
    "npwp": [
        ("0123456789012345", "0123456789012345"),
        ("12.345.678.9-012.345", "0123456789012345"),
        ("9876543210987654", "9876543210987654"),
    ],
    "nib": [
        ("1234567890123", "1234567890123"),
        ("1234-5678-90123", "1234567890123"),
        ("0000000000001", "0000000000001"),
    ],
    "ahu": [
        ("AHU-0012345.AH.01.01.TAHUN 2020", "12345.01.01.2020"),
        ("12345.01.01.2020", "12345.01.01.2020"),
        ("  ahu-5.ah.02.03.tahun 2019 \n", "5.02.03.2019"),
        ("AHU-000.AH.01.01.TAHUN 2021", "0.01.01.2021"),
    ],
}
REJECTED = {
    "passport": ["ABCDEF", "12345", "A1234567890123", "", "éé" + "1234", "AB 12\x00345"],
    "permit": ["123456789", "AB1234567C", "1A2B", "A1B2345678C", "1A2345678", "c12" + "1" * 12],
    "nik": ["320123456789012", "32012345678901234", "320123456789012A", "", "abcdefghijklmnop"],
    "npwp": ["12345678901234", "01234567890123456", "", "abcdefghijklmno"],
    "nib": ["123456789012", "12345678901234", "ABCDEFGHIJKLM", ""],
    "ahu": [
        "AHU-12345.AH.01.01",
        "AHU-12345.AH.1.01.TAHUN 2020",
        "AHU-12345.AH.01.01.TAHUN 20201",
        "AHU-12345.AH.01.01.TAHUN 2020 X",
        "AHA-1.AH.01.01.TAHUN 2020",
        "AHU-1.AH.01.01.TAHUN  2020",
        "AHU-1.AH.01.01.TAHUN ２０２０",
        "12345.01.01",
        "12345",
    ],
}


@pytest.mark.parametrize(
    ("id_type", "raw", "norm"),
    [(t, raw, norm) for t, pairs in ACCEPTED.items() for raw, norm in pairs],
)
def test_accepted_vectors(id_type: str, raw: str, norm: str) -> None:
    assert canonicalize(id_type, raw) == norm


@pytest.mark.parametrize(
    ("id_type", "raw"), [(t, raw) for t, values in REJECTED.items() for raw in values]
)
def test_rejected_vectors_never_echo_the_value(id_type: str, raw: str) -> None:
    with pytest.raises(InvalidIdentifier) as caught:
        canonicalize(id_type, raw)
    assert not raw or raw not in str(caught.value)


def test_every_type_has_three_accepted_and_three_rejected() -> None:
    for id_type in ("passport", "permit", "nik", "npwp", "nib", "ahu"):
        assert len(ACCEPTED[id_type]) >= 3
        assert len(REJECTED[id_type]) >= 3


def test_npwp_fifteen_and_sixteen_digit_forms_meet() -> None:
    assert canonicalize("npwp", "123456789012345") == canonicalize("npwp", "0123456789012345")


def test_npwp_other_lengths_are_not_padded_or_truncated() -> None:
    assert canonical_form("npwp", "1234567890123") == "1234567890123"
    assert canonical_form("npwp", "12345678901234567") == "12345678901234567"
    assert canonical_form("npwp", "0123456789012345") == "0123456789012345"


def test_digit_types_keep_only_ascii_digits() -> None:
    assert canonical_form("nik", "٣٢" + "0" * 14 + "x") == "0" * 14
    assert canonical_form("nib", "１２١" + "5" * 13) == "5" * 13


def test_ahu_year_is_part_of_the_key() -> None:
    assert canonicalize("ahu", "AHU-1234.AH.01.01.TAHUN 2020") != canonicalize(
        "ahu", "AHU-1234.AH.01.01.TAHUN 2021"
    )


def test_kinds() -> None:
    assert {kind_of(t) for t in ("passport", "permit", "nik")} == {"person"}
    assert {kind_of(t) for t in ("npwp", "nib", "ahu")} == {"company"}
    with pytest.raises(InvalidIdentifier):
        kind_of("visa")


def test_unknown_type_and_non_string_are_refused() -> None:
    with pytest.raises(InvalidIdentifier):
        canonicalize("visa", "ABC12345")
    with pytest.raises(InvalidIdentifier):
        canonicalize("passport", 123456)  # type: ignore[arg-type]
    with pytest.raises(InvalidIdentifier):
        canonicalize("passport", "A1" * 40)


def test_norm_version_is_one() -> None:
    assert NORM_VERSION == 1


def test_sql_canonical_exists_for_the_crm_comparable_types_only() -> None:
    for id_type in ("passport", "npwp", "nib"):
        assert "{col}" not in sql_canonical(id_type, "some_col")
        assert "some_col" in sql_canonical(id_type, "some_col")
    for id_type in ("permit", "nik", "ahu"):
        with pytest.raises(KeyError):
            sql_canonical(id_type, "some_col")
