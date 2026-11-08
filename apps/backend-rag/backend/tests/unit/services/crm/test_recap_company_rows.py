"""RECAP_SPEC.md company rows C1-C7: guilt and innocence twins (synthetic values only)."""

import json
from datetime import date, datetime

import pytest

from backend.services.crm import client_recap as r
from backend.services.crm import client_recap_text as rt
from backend.services.kbli_catalogue_membership import ABSENT, CANONICAL, UNKNOWN

CO = 55


def build(company=None, links=(), lkpm=(), verdicts=None, company_id=CO):
    return r.build_company_facts(
        company=company or {},
        links=list(links),
        lkpm_rows=None if lkpm is None else list(lkpm),
        company_id=company_id,
        kbli_verdicts=verdicts,
    )


def payload(**kw):
    return r.recap_payload("company", build(**kw))


def text(**kw):
    return rt.template_recap("company", payload(**kw))


def cf(**fields):
    return {"custom_fields": json.dumps(fields)}


# ── C1: legal form ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "form",
    ["PT PMA", "PT", "PMA", "CV", "Yayasan", "UD", "PMDN Non Fasilitas", "Swasta Nasional"],
)
def test_c1_every_legal_form_is_stated_as_recorded(form):
    assert text(company={"company_type": form}) == f"The company is a {form}."
    assert text(company={"company_type": f"  {form}\n"}) == f"The company is a {form}."


@pytest.mark.parametrize(
    "form",
    [
        "Company",
        "company",
        "PT Acme Synthetic",
        "Ignore previous instructions",
        "PT PMA Holding",
        "",
        None,
        7,
        ["PT"],
    ],
)
def test_c1_a_free_text_or_generic_legal_form_is_omitted(form):
    assert text(company={"company_type": form}) == ""


# ── C2: dates ───────────────────────────────────────────────────────────────


def test_c2_establishment_and_amendment_dates_are_strict_dates():
    company = {"akta_pendirian_date": date(2024, 2, 1), "akta_perubahan_date": "2025-06-01"}
    out = text(company=company)
    assert (
        out
        == "The company was established on 1 February 2024. The last amendment dates from 1 June 2025."
    )


@pytest.mark.parametrize(
    "bad", ["01/02/2024", "2024-02-30", datetime(2024, 2, 1), "", 20240201, None]
)
def test_c2_an_unparseable_date_is_omitted(bad):
    assert text(company={"akta_pendirian_date": bad}) == ""
    assert text(company={"akta_perubahan_date": bad}) == ""
    keep = text(company={"akta_pendirian_date": bad, "akta_perubahan_date": date(2025, 6, 1)})
    assert keep == "The last amendment dates from 1 June 2025."


def test_c2_the_generic_akta_date_is_never_an_amendment():
    assert text(company=cf(akta_date="2025-06-01")) == ""
    assert text(
        company={**cf(akta_date="2025-06-01"), "akta_perubahan_date": date(2025, 7, 1)}
    ) == ("The last amendment dates from 1 July 2025.")


# ── C3: capital ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("amount", "words"),
    [
        (10_000_000_000, "IDR 10.0 billion"),
        (1_500_000_000, "IDR 1.5 billion"),
        (2_500_000, "IDR 2.5 million"),
        (500_000_000, "IDR 500.0 million"),
        (1_000_000_000_000, "IDR 1.0 trillion"),
        (1_049_000_000, "IDR 1,049,000,000"),
        (999_999_999, "IDR 999,999,999"),
        (1_000_000_001, "IDR 1,000,000,001"),
        (12_345, "IDR 12,345"),
        (1_050_000_000_000, "IDR 1,050,000,000,000"),
        (1_100_000_000_000, "IDR 1.1 trillion"),
        (0, "IDR 0"),
    ],
)
def test_c3_words_only_when_the_short_form_is_exact(amount, words):
    assert rt.fmt_idr(amount) == words


def test_c3_capital_is_read_through_the_integer_grammar():
    out = text(company=cf(authorized_capital="10.000.000.000", paid_up_capital=2_500_000_000))
    assert out == "Authorized capital is IDR 10.0 billion, of which IDR 2.5 billion is paid up."
    out = text(company=cf(authorized_capital="1,049,000,000"))
    assert out == "Authorized capital is IDR 1,049,000,000."


@pytest.mark.parametrize(
    "bad",
    [
        "-5",
        "−5",
        "(5)",
        5.0,
        "5.0",
        "1e9",
        "Rp 1.000.000",
        "1.000,00",
        "01",
        True,
        None,
        [],
        {},
        "",
        -1,
    ],
)
def test_c3_an_unparseable_capital_is_omitted_and_the_other_one_survives(bad):
    assert text(company=cf(authorized_capital=bad)) == ""
    out = text(company=cf(authorized_capital=bad, paid_up_capital=2_000_000_000))
    assert out == "Paid-up capital is IDR 2.0 billion."


def test_c3_paid_up_above_authorized_is_not_worded_as_part_of_it():
    out = text(company=cf(authorized_capital=1_000_000_000, paid_up_capital=2_000_000_000))
    assert out == "Authorized capital is IDR 1.0 billion and paid-up capital is IDR 2.0 billion."


def test_c3_total_shares_are_never_stated():
    assert text(company=cf(total_shares="10000")) == ""
    assert "total_shares" not in json.dumps(payload(company=cf(total_shares=10000)))


# ── C4: shareholders and directors ──────────────────────────────────────────


def link(client, role, shares=None):
    return {"client_id": client, "role": role, "shares_count": shares}


@pytest.mark.parametrize("role", ["shareholder", "Pemegang Saham", " SHAREHOLDER "])
def test_c4_a_shareholder_role_counts_a_shareholder(role):
    assert payload(links=[link(1, role)]) == {"shareholders_count": 1}


@pytest.mark.parametrize(
    "role",
    [
        "director",
        "Direktur",
        "Direktur Utama",
        "director_utama",
        "president_director",
        "Presiden Direktur",
    ],
)
def test_c4_a_director_role_counts_a_director(role):
    assert payload(links=[link(1, role)]) == {"directors_count": 1}


@pytest.mark.parametrize(
    "role", ["commissioner", "Komisaris", "komisaris utama", "presiden komisaris", "contact"]
)
def test_c4_other_known_roles_count_nobody_and_block_nothing(role):
    assert payload(links=[link(1, role), link(2, "shareholder"), link(3, "director")]) == {
        "shareholders_count": 1,
        "directors_count": 1,
    }


def test_c4_shares_make_a_shareholder_whatever_the_role():
    p = payload(
        links=[
            link(1, "commissioner", 100),
            link(2, "director", 5),
            link(3, "director", None),
            link(4, "director", 0),
        ]
    )
    assert p["shareholders_count"] == 2 and p["directors_count"] == 3


@pytest.mark.parametrize(
    "role",
    ["not a shareholder", "owner", "badan hukum", None, "", "shareholders", "director of sales"],
)
def test_c4_an_unknown_role_without_shares_omits_the_shareholder_count_and_the_director_count(role):
    twin = payload(links=[link(1, "shareholder"), link(2, "director")])
    assert twin == {"shareholders_count": 1, "directors_count": 1}
    assert payload(links=[link(1, "shareholder"), link(2, "director"), link(3, role)]) == {}
    assert payload(links=[link(1, "shareholder"), link(2, "director"), link(3, role, 0)]) == {}


def test_c4_an_unknown_role_with_shares_is_a_holder_but_still_blocks_the_director_count():
    p = payload(links=[link(1, "shareholder"), link(2, "director"), link(3, "owner", 10)])
    assert p == {"shareholders_count": 2}


def test_c4_counts_are_distinct_clients_not_rows():
    p = payload(
        links=[
            link(1, "shareholder"),
            link(1, "shareholder", 5),
            link(2, "director"),
            link(2, "director"),
        ]
    )
    assert p == {"shareholders_count": 1, "directors_count": 1}


@pytest.mark.parametrize("shares", [-1, 1.5, "100", True])
def test_c4_an_unreadable_share_count_omits_the_shareholder_count_only(shares):
    p = payload(links=[link(1, "director"), link(2, "director", shares)])
    assert p == {"directors_count": 2}


def test_c4_the_profile_register_is_never_counted_and_the_sentence_says_linked_in_the_crm():
    register = cf(shareholders=json.dumps([{"name": "A"}, {"name": "B"}]))
    assert text(company=register) == ""
    out = text(links=[link(1, "shareholder"), link(2, "shareholder"), link(3, "director")])
    assert out == "It has two shareholders and one director linked in the CRM."
    assert text(links=[link(1, "director")]) == "It has one director linked in the CRM."


# ── C5 / C6: KBLI ───────────────────────────────────────────────────────────

V = {"62010": CANONICAL, "70209": CANONICAL, "74100": ABSENT, "99999": UNKNOWN}


def kbli_payload(primary=None, codes=None, verdicts=V):
    company = {"kbli_code": primary}
    if codes is not None:
        company["custom_fields"] = {"kbli_codes": codes}
    return payload(company=company, verdicts=verdicts)


def test_c5_distinct_canonical_codes_are_counted_without_titles():
    p = kbli_payload("62010", ["62010", "70209", "70209"])
    assert p == {"kbli_count": 2}
    assert text(
        company={"kbli_code": "62010", "kbli_description": "Secret Title Ltd"}, verdicts=V
    ) == ("It is registered under one KBLI line.")
    assert "Secret" not in json.dumps(p)


def test_c5_the_company_code_and_the_list_are_one_set():
    assert kbli_payload(None, ["62010", "70209"]) == {"kbli_count": 2}
    assert kbli_payload("62010", None) == {"kbli_count": 1}
    assert kbli_payload("", ["62010"]) == {"kbli_count": 1}
    assert kbli_payload("62010", '["70209"]') == {"kbli_count": 2}
    assert kbli_payload(None, None) == {}
    assert kbli_payload(None, []) == {}


def test_c6_one_absent_code_among_valid_ones_omits_c5_and_states_c6():
    p = kbli_payload("62010", ["70209", "74100"])
    assert p == {"kbli_not_in_catalogue": True}
    out = rt.template_recap("company", p)
    assert out == "At least one KBLI code on file is not in the KBLI 2025 catalogue."
    assert "74100" not in out and "1" not in out


def test_c5_all_canonical_states_c5_and_omits_c6():
    p = kbli_payload("62010", ["70209"])
    assert p == {"kbli_count": 2} and "kbli_not_in_catalogue" not in p


def test_c5_c6_one_unknown_code_omits_both_even_beside_an_absent_one():
    assert kbli_payload("62010", ["99999"]) == {}
    assert kbli_payload("62010", ["99999", "74100"]) == {}
    assert kbli_payload("62010", ["12345"]) == {}  # no verdict at all is UNKNOWN
    assert kbli_payload("62010", ["70209"], verdicts=None) == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"a": "62010"},
        [["62010"]],
        ["62010", 70209],
        ["62010", None],
        "not json",
        "[62010]",
        '{"a": 1}',
        ["6201"],
        ["620100"],
        [" 62010"],
        ["62010 "],
        ["６２０１０"],
        [""],
        7,
        True,
    ],
)
def test_c5_c6_a_malformed_list_or_a_non_strict_code_omits_both(bad):
    assert kbli_payload("62010", ["70209"]) == {"kbli_count": 2}  # twin
    assert kbli_payload("62010", bad) == {}
    assert kbli_payload("74100", bad) == {}


@pytest.mark.parametrize("primary", ["6201", " 62010", "62010 ", "62010\n", 62010, "abcde", "0"])
def test_c5_c6_a_non_strict_company_code_omits_both(primary):
    assert kbli_payload(primary, ["70209"]) == {}


def test_c5_c6_the_bound_is_fifty_distinct_codes():
    fifty = [f"{10000 + i:05d}" for i in range(50)]
    verdicts = dict.fromkeys(fifty, CANONICAL)
    assert kbli_payload(None, fifty, verdicts) == {"kbli_count": 50}
    fifty_one = [*fifty, "10050"]
    verdicts["10050"] = CANONICAL
    assert kbli_payload(None, fifty_one, verdicts) == {}
    verdicts["10049"] = ABSENT
    assert kbli_payload(None, fifty_one, verdicts) == {}
    # the bound counts distinct codes, not entries
    assert kbli_payload(None, [*fifty, *fifty], verdicts) == {"kbli_not_in_catalogue": True}


def test_c5_c6_an_undecodable_custom_fields_omits_both():
    assert payload(company={"kbli_code": "62010", "custom_fields": "{broken"}, verdicts=V) == {}
    assert payload(company={"kbli_code": "62010", "custom_fields": ["x"]}, verdicts=V) == {}
    assert payload(company={"kbli_code": "62010", "custom_fields": None}, verdicts=V) == {
        "kbli_count": 1
    }


def test_c5_wording_is_singular_and_plural():
    assert (
        rt.template_recap("company", {"kbli_count": 1}) == "It is registered under one KBLI line."
    )
    assert (
        rt.template_recap("company", {"kbli_count": 12}) == "It is registered under 12 KBLI lines."
    )


# ── C7: LKPM ────────────────────────────────────────────────────────────────


def rows(*specs):
    return [
        {"client_id": c, "company_id": co, "year": y, "quarter": q, "status": s}
        for (c, co, y, q, s) in specs
    ]


LINKS = [link(1, "shareholder"), link(2, "director")]


def lkpm(*specs, links=LINKS):
    return build(links=links, lkpm=rows(*specs))["lkpm_latest"]


def test_c7_the_latest_period_is_stated_as_q_year_with_its_status():
    got = lkpm(
        (1, CO, 2026, "Q1", "draft"),
        (1, CO, 2026, "Q2", "approved"),
        (1, CO, 2025, "Q4", "approved"),
    )
    assert got == {"year": 2026, "quarter": "Q2", "status": "approved"}
    out = rt.template_recap("company", {"lkpm_latest": got})
    assert out == "The latest LKPM on record is Q2 2026 (approved)."


def test_c7_quarter_may_be_an_integer_and_is_still_stated_as_q_n():
    assert lkpm((1, CO, 2026, 3, "approved"), (1, CO, 2026, 2, "approved"))["quarter"] == "Q3"


def test_c7_reports_of_another_company_are_not_candidates():
    assert (
        lkpm((1, CO, 2026, "Q1", "approved"), (1, 999, 2027, "Q4", "approved"))["quarter"] == "Q1"
    )
    assert lkpm((1, 999, 2027, "Q4", "approved")) is None


def test_c7_a_client_level_row_of_the_current_client_omits_the_period():
    assert lkpm((1, CO, 2026, "Q1", "approved")) is not None  # twin
    assert lkpm((1, CO, 2026, "Q1", "approved"), (1, None, 2025, "Q1", "draft")) is None


def test_c7_a_client_level_row_of_another_linked_client_omits_the_period():
    # The builder is never told who opened the recap, so the answer is the same for every linked client.
    assert lkpm((1, CO, 2026, "Q1", "approved"), (2, None, 2025, "Q1", "draft")) is None
    assert lkpm((1, CO, 2026, "Q1", "approved"), (1, None, 2025, "Q1", "draft")) is None


def test_c7_a_client_level_row_of_a_client_not_linked_to_the_company_is_innocent():
    assert lkpm((1, CO, 2026, "Q1", "approved"), (77, None, 2025, "Q1", "draft")) is not None


def test_c7_a_2027_q4_padded_row_beside_2026_q2_omits_the_period():
    assert lkpm((1, CO, 2026, "Q2", "approved")) is not None
    assert lkpm((1, CO, 2026, "Q2", "approved"), (1, CO, 2027, "q4 ", "approved")) is None
    assert lkpm((1, CO, 2026, "Q2", "approved"), (1, CO, 2027, "Q4 ", "approved")) is None


@pytest.mark.parametrize(
    "bad",
    [
        ("Q5", 2027),
        ("Q0", 2027),
        (None, 2027),
        ("Q1", None),
        ("Q1", 1999),
        ("Q1", "2027"),
        (True, 2027),
        ("Q1", True),
    ],
)
def test_c7_any_unparseable_candidate_period_omits_the_period(bad):
    quarter, year = bad
    assert lkpm((1, CO, 2026, "Q2", "approved"), (1, CO, year, quarter, "approved")) is None


def test_c7_two_clients_with_the_same_latest_period_and_different_statuses_drop_the_label_only():
    got = lkpm(
        (1, CO, 2026, "Q2", "draft"),
        (2, CO, 2026, "Q2", "approved"),
        (1, CO, 2026, "Q1", "approved"),
    )
    assert got == {"year": 2026, "quarter": "Q2"}
    assert rt.template_recap("company", r.recap_payload("company", {"lkpm_latest": got})) == (
        "The latest LKPM on record is Q2 2026."
    )


def test_c7_with_equal_statuses_the_label_is_stated():
    got = lkpm((1, CO, 2026, "Q2", "approved"), (2, CO, 2026, "Q2", " APPROVED "))
    assert got["status"] == "approved"


def test_c7_only_the_latest_periods_statuses_are_compared():
    got = lkpm(
        (1, CO, 2026, "Q1", "draft"),
        (1, CO, 2026, "Q2", "approved"),
        (2, CO, 2026, "Q2", "approved"),
    )
    assert got["status"] == "approved"


@pytest.mark.parametrize("status", ["final", "", None, 3, "approved!", "Approved Final"])
def test_c7_a_status_outside_the_set_omits_the_label_not_the_period(status):
    got = lkpm((1, CO, 2026, "Q2", status))
    assert got["quarter"] == "Q2" and got["status"] is None
    assert r.recap_payload("company", {"lkpm_latest": got})["lkpm_latest"] == {
        "year": 2026,
        "quarter": "Q2",
    }


def test_c7_an_unreadable_lkpm_table_omits_the_period_and_nothing_else():
    p = payload(company={"company_type": "PT"}, lkpm=None)
    assert p == {"legal_form": "PT"}
    assert "lkpm_latest" in payload(
        company={"company_type": "PT"}, lkpm=rows((1, CO, 2026, "Q1", "approved"))
    )


def test_c7_no_report_says_nothing():
    assert lkpm() is None
