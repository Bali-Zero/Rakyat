"""A visa index code is not matched inside a longer number.

The renewal rules find their rule by patterns inside the client's visa type. "c1" is the
tourist visa; C10, C12 and C18 are other visas that happen to start with the same characters.
"""

from __future__ import annotations

import pytest

from backend.services.compliance.renewal_rules import (
    RENEWAL_RULES,
    RULE_PRIORITY_ORDER,
    match_rule,
)


@pytest.mark.parametrize(
    "visa_type",
    ["C10 Speaker Visa", "C18 Work Trial", "C12", "c10", "Visa C18"],
)
def test_a_longer_index_is_not_the_tourist_visa(visa_type: str) -> None:
    assert match_rule("visa", visa_type).rule_id == "generic_visa_renewal"


@pytest.mark.parametrize(
    "visa_type",
    ["C1", "C1 Tourism", "c1 tourist visa", "Tourist Visa", "Visa C1 (single entry)", "C1/tourism"],
)
def test_the_tourist_visa_still_finds_its_rule(visa_type: str) -> None:
    assert match_rule("visa", visa_type).rule_id == "visa_tourist_extension"


@pytest.mark.parametrize(
    ("visa_type", "rule_id"),
    [
        ("B1", "visa_voa_extension"),
        ("B1 Visa on Arrival", "visa_voa_extension"),
        ("e-VOA", "visa_voa_extension"),
        ("B10", "generic_visa_renewal"),
    ],
)
def test_the_arrival_visa_code_follows_the_same_rule(visa_type: str, rule_id: str) -> None:
    assert match_rule("visa", visa_type).rule_id == rule_id


@pytest.mark.parametrize(
    ("document_type", "visa_type"),
    [
        ("kitas", "E33E Senior"),
        ("kitas", "E33F Senior"),
        ("kitas", "E33G Remote Worker"),
        ("kitas", "E33 Second Home"),
        ("kitas", "KITAS Investor"),
        ("kitas", "Working KITAS"),
        ("visa", "E33"),
    ],
)
def test_codes_followed_by_a_letter_match_as_before(document_type: str, visa_type: str) -> None:
    """A letter after the code is the same family: only a following digit makes another code."""
    after = match_rule(document_type, visa_type).rule_id

    lowered = visa_type.lower()
    before = "generic_visa_renewal"
    for rule_id in RULE_PRIORITY_ORDER:
        rule = RENEWAL_RULES[rule_id]
        if document_type not in rule.document_types:
            continue
        if "*" in rule.visa_type_patterns or any(
            p.lower() in lowered for p in rule.visa_type_patterns
        ):
            before = rule_id
            break
    assert after == before
