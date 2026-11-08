"""Boundary and wording tests added after the mutation pass (RECAP_SPEC.md test obligation 7)."""

import json
from datetime import date

import pytest

from backend.services.crm import client_recap as r
from backend.services.crm import client_recap_text as rt
from backend.services.crm.permit_label import permit_families
from backend.services.crm.recap_permits import classify_document
from backend.tests.unit.services.crm.test_recap_company_rows import (
    build,
    link,
)
from backend.tests.unit.services.crm.test_recap_company_rows import (
    payload as co_payload,
)
from backend.tests.unit.services.crm.test_recap_flow import FakeConn
from backend.tests.unit.services.crm.test_recap_immigration_rows import (
    CATALOGUE,
    TODAY,
    doc,
    ocr,
    payload,
)


def test_a_permit_signal_in_a_document_type_without_a_family_still_makes_it_a_permit_document():
    d = doc("passport", visa_type="E23")
    result = classify_document(d, CATALOGUE)
    assert result is not None and result.classifiable is False
    good = doc("kitas", expiry=date(2027, 12, 1), visa_type="E23")
    assert "current_permit" not in payload(documents=[good, d])


@pytest.mark.parametrize(
    ("text", "family"),
    [
        ("permanent stay permit", "kitap"), ("izin tinggal tetap", "kitap"), ("itap", "kitap"),
        ("re-entry", "merp"), ("reentry", "merp"), ("re entry", "merp"),
        ("multiple exit", "merp"), ("izin masuk kembali", "merp"),
        ("limited stay", "kitas"), ("residence permit", "kitas"), ("tinggal terbatas", "kitas"),
        ("itas", "kitas"), ("visit stay", "itk"), ("visit visa", "itk"),
        ("izin tinggal kunjungan", "itk"), ("telex", "evisa"), ("E-Visa", "evisa"),
    ],
)  # fmt: skip
def test_every_family_keyword_is_evaluated_on_its_own(text, family):
    assert permit_families(text) == {family}


def test_two_families_in_one_text_are_both_reported():
    assert permit_families("kitas and re-entry") == {"kitas", "merp"}
    assert permit_families("") == frozenset()


# ── I3 / I6 / I7 boundaries ─────────────────────────────────────────────────


def test_an_itk_expiring_on_the_issue_date_is_eligible_and_the_latest_one_sets_the_status():
    permit = doc("kitas", expiry=date(2027, 3, 14), ocr=ocr("E23", issue_date="2026-03-14"))
    on_issue = doc("itk", expiry=date(2026, 3, 14))
    p = payload(documents=[permit, on_issue])
    assert p["itk_expiry_date"] == "2026-03-14" and p["itk_status"] == "expired"
    future = doc("itk", expiry=date(2027, 1, 1))
    p = payload(documents=[permit, on_issue, future])
    assert p["itk_expiry_date"] == "2027-01-01" and p["itk_status"] == "valid"


def test_two_family_permits_ending_before_are_worded_in_the_plural():
    fam = [{"current_visa_type": "E31", "visa_expiry": date(2027, 1, d)} for d in (1, 2)]
    out = rt.template_recap("immigration", payload(documents=[doc("kitas")], family=fam), TODAY)
    assert "Two family members are on file, two of them with permits that end before" in out


def test_two_open_practices_of_one_type_are_listed_with_including():
    p = {"open_practices_count": 2, "open_practice_types": ["A"]}
    assert rt.template_recap("immigration", p) == "Two immigration practices are open, including A."


def test_the_slot_filter_bounds_are_inclusive():
    assert r._s_slot("x" * 60) == "x" * 60 and r._s_slot("x" * 61) is None
    p = r.recap_payload(
        "immigration", {"current_permit": {"code": "E23", "official_name": "y" * 80}}
    )
    assert p["current_permit"]["official_name"] == "y" * 80
    p = r.recap_payload(
        "immigration", {"current_permit": {"code": "E23", "official_name": "y" * 81}}
    )
    assert "official_name" not in p["current_permit"]


def test_a_zero_capital_is_a_recorded_value_and_survives_the_allow_list():
    assert r.recap_payload("company", {"authorized_capital_idr": 0}) == {
        "authorized_capital_idr": 0
    }
    assert co_payload(company={"custom_fields": json.dumps({"paid_up_capital": "0"})}) == {
        "paid_up_capital_idr": 0
    }


@pytest.mark.asyncio
async def test_exactly_fifty_codes_are_checked_against_the_catalogue():
    codes = [f"{10000 + i:05d}" for i in range(50)]
    conn = FakeConn(company={"custom_fields": json.dumps({"kbli_codes": codes})})
    out = await r.fetch_company_inputs(conn, 55)
    assert len(out["kbli_verdicts"]) == 50


# ── C4 share counts ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("shares", [-1, 1.5, "100", True])
def test_an_unreadable_share_count_omits_the_shareholder_count_even_beside_a_holder(shares):
    p = co_payload(links=[link(1, "shareholder"), link(2, "director", shares)])
    assert "shareholders_count" not in p and p["directors_count"] == 1


def test_one_share_makes_a_shareholder():
    assert co_payload(links=[link(1, "commissioner", 1)]) == {"shareholders_count": 1}


# ── template wording and fitting ────────────────────────────────────────────


def test_the_char_cap_is_450_and_the_template_drops_trailing_clauses_to_fit():
    assert rt.MAX_CHARS == 450
    names = ["N" * 59 + c for c in "ABCD"]
    p = {
        "current_permit": {"code": "E23", "official_name": "Y" * 70, "expiry_date": "2027-03-14", "status": "valid"},
        "open_practices_count": 4, "open_practice_types": names,
        "itk_expiry_date": "2027-01-15", "itk_status": "valid",
        "passport_expiry_date": "2027-02-01", "passport_status": "valid",
        "family_members_count": 2,
    }  # fmt: skip
    out = rt.template_recap("immigration", p, TODAY)
    assert len(out) <= 450 and "family" not in out and "Four immigration practices" in out
    one = {"current_permit": dict(p["current_permit"], official_name="Z" * 460)}
    assert len(rt.template_recap("immigration", one, TODAY)) > 450  # a single clause is never cut


def test_a_builder_payload_that_renders_exactly_max_chars_keeps_every_clause():
    documents = [
        doc("kitas", expiry=date(2027, 3, 14), ocr=ocr("E23", issue_date="2026-01-01")),
        doc("itk", expiry=date(2027, 2, 1)),
    ]
    practices = [
        {"id": i, "status": "on_process", "category": "kitas", "practice_type_name": letter * 33}
        for i, letter in enumerate("ABCD")
    ]
    facts = r.build_immigration_facts(
        documents=documents,
        family_members=[{"current_visa_type": "E31", "visa_expiry": date(2027, 1, 1)}],
        practices=practices,
        passport_expiry=date(2027, 5, 1),
        catalogue={"E23": "YY"},
        today=TODAY,
    )
    out = rt.template_recap("immigration", r.recap_payload("immigration", facts), TODAY)
    assert len(out) == rt.MAX_CHARS
    assert "one family member is on file" in out
    assert "one of them with a permit that ends before the main permit" in out


def test_clauses_fold_into_at_most_three_sentences():
    two = rt.template_recap(
        "company", {"legal_form": "PT", "established_date": "2024-02-01", "kbli_count": 1}
    )
    assert two.count(". ") == 1 and two.endswith("line.")
    three = rt.template_recap(
        "company", {"legal_form": "PT", "kbli_count": 1, "last_amendment_date": "2025-06-01"}
    )
    assert three.count(". ") == 2
    four = rt.template_recap(
        "company",
        {
            "legal_form": "PT",
            "kbli_count": 1,
            "last_amendment_date": "2025-06-01",
            "shareholders_count": 1,
        },
    )
    assert four.count(". ") == 2 and " and " in four


@pytest.mark.parametrize(("n", "word"), [(0, "zero"), (1, "one"), (10, "ten"), (11, "11")])
def test_number_words(n, word):
    assert rt.num(n) == word


def test_fallback_status_is_judged_against_today_when_the_payload_has_none():
    today = date(2026, 11, 7)
    assert "runs until 7 November 2026" in rt.template_recap(
        "immigration", {"itk_expiry_date": "2026-11-07"}, today
    )
    assert "expires on 7 November 2026" in rt.template_recap(
        "immigration", {"passport_expiry_date": "2026-11-07"}, today
    )
    assert "expired on 6 November 2026" in rt.template_recap(
        "immigration", {"itk_expiry_date": "2026-11-06"}, today
    )


def test_a_permit_named_like_its_code_is_not_given_a_second_code():
    p = {
        "current_permit": {
            "code": "KITAS",
            "official_name": "KITAS",
            "expiry_date": "2027-03-14",
            "status": "valid",
        }
    }
    assert rt.template_recap("immigration", p).startswith(
        "The client's current permit is KITAS, valid"
    )


def test_a_lkpm_period_needs_both_year_and_quarter_and_equal_capitals_are_part_of():
    assert rt.template_recap("company", {"lkpm_latest": {"year": 2026}}) == ""
    assert rt.template_recap("company", {"lkpm_latest": {"quarter": "Q1"}}) == ""
    both = {"authorized_capital_idr": 1_000_000_000, "paid_up_capital_idr": 1_000_000_000}
    assert "of which IDR 1.0 billion is paid up" in rt.template_recap("company", both)


def test_based_on_labels_the_company_groups_it_states():
    assert rt.based_on("company", {"kbli_not_in_catalogue": True}) == ["KBLI"]
    assert rt.based_on("company", {"paid_up_capital_idr": 5}) == ["capital"]
    assert (
        rt.based_on("immigration", {"paid_up_capital_idr": 5, "kbli_not_in_catalogue": True}) == []
    )
    assert rt.based_on("company", {"kbli_count": 2, "kbli_not_in_catalogue": True}) == ["KBLI"]


# ── the validator reads amounts exactly ─────────────────────────────────────


def test_full_digit_amounts_are_checked_exactly_even_at_the_end_of_a_sentence():
    p = {"authorized_capital_idr": 1_049_000_000, "legal_form": "PT"}
    ok, reasons = rt.validate_recap(
        "The company is a PT. Authorized capital is IDR 1,049,000,000.", p
    )
    assert ok, reasons
    ok, reasons = rt.validate_recap(
        "The company is a PT. Authorized capital is IDR 1,049,000,001.", p
    )
    assert not ok and "invented_amount" in reasons
    ok, reasons = rt.validate_recap(
        "The company is a PT. Authorized capital is IDR 1.0 billion.", p
    )
    assert not ok and "invented_amount" in reasons


def test_the_build_and_the_template_agree_on_the_example_in_the_spec():
    out = rt.template_recap(
        "company",
        co_payload(company={"custom_fields": json.dumps({"authorized_capital": 1049000000})}),
    )
    assert out == "Authorized capital is IDR 1,049,000,000."
    assert build(company={}) is not None


def test_three_clauses_make_exactly_three_sentences():
    out = rt.template_recap(
        "company", {"legal_form": "PT", "kbli_count": 1, "last_amendment_date": "2025-06-01"}
    )
    assert out == (
        "The company is a PT. It is registered under one KBLI line. "
        "The last amendment dates from 1 June 2025."
    )


def test_the_payload_status_wins_over_the_date_for_the_itk_wording():
    future = {"itk_expiry_date": "2999-01-01", "itk_status": "expired"}
    assert "expired on 1 January 2999" in rt.template_recap("immigration", future)
    past = {"itk_expiry_date": "2000-01-01", "itk_status": "valid"}
    assert "runs until 1 January 2000" in rt.template_recap("immigration", past)


def test_one_family_permit_ending_before_is_singular():
    p = {"family_members_count": 2, "family_permits_ending_before_main_count": 1}
    assert rt.template_recap("immigration", p) == (
        "Two family members are on file, one of them with a permit that ends before the main permit."
    )


def test_every_lkpm_status_label_passes_the_allow_list_and_whitespace_collapses_in_names():
    for status in ("draft", "pending", "submitted", "approved"):
        got = r.recap_payload(
            "company", {"lkpm_latest": {"year": 2026, "quarter": "Q1", "status": status}}
        )
        assert got["lkpm_latest"]["status"] == status
    assert r._s_slot("Visa   Extension") == "Visa Extension"
