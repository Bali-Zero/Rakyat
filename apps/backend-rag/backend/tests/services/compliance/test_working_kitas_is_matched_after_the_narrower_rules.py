"""Working KITAS matching must not shadow narrower KITAS routes."""

from collections import Counter

import pytest

from backend.services.compliance.renewal_rules import (
    RENEWAL_RULES,
    RULE_PRIORITY_ORDER,
    match_rule,
)


@pytest.mark.parametrize(
    ("visa_type", "expected_rule_id"),
    [
        ("E33G Remote Working", "kitas_remote_worker_extend"),
        ("Working KITAS Dependent", "kitas_dependent_extend"),
        ("Dependent KITAS (of Working KITAS holder)", "kitas_dependent_extend"),
        ("Spouse KITAS of a working holder", "kitas_spouse_extend"),
        ("Retirement KITAS (non-working)", "kitas_retirement_extend"),
    ],
)
def test_narrower_kitas_rules_win_over_working_substrings(
    visa_type: str, expected_rule_id: str
) -> None:
    assert match_rule("kitas", visa_type).rule_id == expected_rule_id


@pytest.mark.parametrize(
    "visa_type",
    ["Working KITAS", "KITAS Work Permit", "IMTA"],
)
def test_working_kitas_still_matches_its_own_names(visa_type: str) -> None:
    assert match_rule("kitas", visa_type).rule_id == "kitas_working_extend"


def test_neighbouring_kitas_rules_keep_matching() -> None:
    assert match_rule("kitas", "KITAS Investor").rule_id == "kitas_investor_extend"
    assert match_rule("kitas", "E33G").rule_id == "kitas_remote_worker_extend"


def test_c18_work_trial_is_not_a_tourist_visa() -> None:
    assert match_rule("visa", "C18 Work Trial").rule_id != "visa_tourist_extension"


def test_each_renewal_rule_is_listed_once_in_priority_order() -> None:
    assert Counter(RULE_PRIORITY_ORDER) == Counter(RENEWAL_RULES.keys())
