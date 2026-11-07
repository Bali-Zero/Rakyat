"""E28: every Guide leaf, Law conditions and exact calendar-month boundaries."""

import json
import re

import pytest

from backend.services.visa_engine.families import guide
from backend.services.visa_engine.families.guide import (
    NEEDS_SPONSOR,
    NOT_COVERED,
    OK,
    OK_WITH_EXTENSION,
    STAY_TOO_LONG,
    resolve,
)

DATA = guide.load_family("E28")
LAW_DIR = guide.HERE / "law" / "E28"
PATHS = {
    "shareholder_in_sponsoring_company": "E28A",
    "individual_establishing_company": "E28B",
    "capital_market_investor": "E28C",
    "foreign_branch_or_subsidiary_director_or_commissioner": "E28D",
    "ikn_foreign_branch_or_subsidiary_director_or_commissioner": "E28F",
    "parent_company_representative_in_indonesian_branch": "E28G",
}
YEARS = {"E28A": [1, 2], **{code: [5, 10] for code in ("E28B", "E28C", "E28D", "E28F", "E28G")}}


def law(code: str) -> dict:
    return json.loads((LAW_DIR / f"{code}.json").read_text())


def commitment(code: str, tier: int) -> str:
    return f"investment_commitment_{code.lower()}_{tier}y"


def answers(route: str, **extra: object) -> dict:
    gates = {
        commitment(c, t): "yes" for c in ("E28B", "E28C", "E28D", "E28F", "E28G") for t in (5, 10)
    }
    return {
        "investment_route": route,
        "own_shareholding": "yes",
        "sponsor": "yes",
        **gates,
        "stay_months": 12,
        **extra,
    }


def test_reachable_codes_and_law_questions() -> None:
    assert DATA["questions"]["investment_route"]["options"] == PATHS
    assert set(DATA["questions"]) == {
        "investment_route",
        "own_shareholding",
        "sponsor",
        "stay_months",
        *(commitment(c, t) for c in ("E28B", "E28C", "E28D", "E28F", "E28G") for t in (5, 10)),
    }
    assert set(DATA["visas"]) == {"E28", "E28A", "E28B", "E28C", "E28D", "E28E", "E28F", "E28G"}
    assert {p.stem for p in LAW_DIR.glob("E28*.json")} == set(DATA["visas"])
    for code in ("E28", "E28E"):
        assert law(code)["status"] == "NOT_FOUND"
        assert DATA["visas"][code]["stay_options"] is None
    assert "mendirikan perusahaan" in law("E28B")["definition_verbatim"]
    assert "obligasi pemerintah" in law("E28C")["definition_verbatim"]
    for code in ("E28D", "E28F"):
        assert "anggota direksi atau anggota dewan komisaris" in law(code)["definition_verbatim"]
    assert "Ibu Kota Nusantara" in law("E28F")["definition_verbatim"]
    assert "perwakilan perusahaan induk" in law("E28G")["definition_verbatim"]


@pytest.mark.parametrize("code", YEARS)
def test_initial_choices_and_sponsor_equal_law(code: str) -> None:
    card, visa = law(code), DATA["visas"][code]
    assert visa["sponsor_required"] is card["sponsor_required"]["as_stated_boolean"]
    assert visa["entry_type"] == card["entry"].upper() == "MULTIPLE"
    assert visa["extendable"] is True
    assert "dapat diperpanjang" in " ".join(card["extendable"]).lower()
    assert visa["stay_options"] == [{"value": y, "unit": "YEAR"} for y in YEARS[code]]
    assert ("1 tahun atau 2 tahun" if code == "E28A" else "5 tahun dan 10 tahun") in " ".join(
        card["stay_days"]
    )
    assert "stay_days" not in visa and "max_total_days" not in visa
    if code != "E28A":
        assert visa["sponsor"] == "NOT_REQUIRED"
        assert "tidak membutuhkan penjamin/sponsor" in card["sponsor_required"]["verbatim"][0]
    if code != "E28G":
        years = 6 if code == "E28A" else 10
        assert (
            f"tidak lebih dari {years} tahun"
            in card["supplementary_stay_evidence"]["extension_verbatim"]
        )
        assert visa["max_total_months"] == years * 12
    else:
        assert card.get("supplementary_stay_evidence") is None
        assert "max_total_months" not in visa


@pytest.mark.parametrize("route,code", PATHS.items())
def test_question_order_and_maximum_path_length(route: str, code: str) -> None:
    assert resolve("E28", {}) == {"ask": "investment_route"}
    a = {"investment_route": route}
    if code == "E28A":
        assert resolve("E28", a) == {"ask": "own_shareholding", "code": code}
        a["own_shareholding"] = "yes"
        assert resolve("E28", a) == {"ask": "sponsor", "code": code}
        a["sponsor"] = "yes"
    assert resolve("E28", a) == {"ask": "stay_months", "code": code}
    a["stay_months"] = 12
    if code != "E28A":
        assert resolve("E28", a) == {"ask": commitment(code, 5), "code": code}
        a[commitment(code, 5)] = "yes"
    assert resolve("E28", a)["verdict"] == OK
    assert len(a) == (4 if code == "E28A" else 3)


@pytest.mark.parametrize("route,code", PATHS.items())
def test_every_initial_month_returns_smallest_fitting_year_option(route: str, code: str) -> None:
    for months in range(1, YEARS[code][-1] * 12 + 1):
        year = next(y for y in YEARS[code] if months <= y * 12)
        assert resolve("E28", answers(route, stay_months=months)) == {
            "code": code,
            "verdict": OK,
            "stay_option": {"value": year, "unit": "YEAR"},
        }
    assert resolve("E28", answers(route, stay_months=str(YEARS[code][-1] * 12)))["verdict"] == OK


def test_e28a_extension_reaches_six_years_but_never_exceeds_it() -> None:
    route = "shareholder_in_sponsoring_company"
    for months in range(25, 73):
        assert resolve("E28", answers(route, stay_months=months)) == {
            "code": "E28A",
            "verdict": OK_WITH_EXTENSION,
            "stay_option": {"value": 2, "unit": "YEAR"},
            "total_max_known": True,
        }
    for months in (73, 1200):
        assert resolve("E28", answers(route, stay_months=months)) == {
            "code": "E28A",
            "verdict": STAY_TOO_LONG,
        }


@pytest.mark.parametrize("route,code", PATHS.items())
def test_over_largest_golden_option_honors_known_or_unknown_total(route: str, code: str) -> None:
    if code == "E28A":
        return
    for months in (121, 1200):
        result = resolve("E28", answers(route, stay_months=months))
        if code == "E28G":
            assert result == {
                "code": code,
                "verdict": OK_WITH_EXTENSION,
                "stay_option": {"value": 10, "unit": "YEAR"},
                "total_max_known": False,
            }
        else:
            assert result == {"code": code, "verdict": STAY_TOO_LONG}


def test_own_shareholding_and_company_sponsor_are_independent_gates() -> None:
    route = "shareholder_in_sponsoring_company"
    text = law("E28A")["requirements_verbatim"][6]["text"]
    assert "Rp10.000.000.000,00" in text and "perusahaan penjamin" in text
    assert "visa bekerja sesuai dengan jabatan" in text
    assert DATA["visas"]["E28A"]["requires_question"] == "own_shareholding"
    assert resolve("E28", answers(route, own_shareholding="no", sponsor="no")) == {
        "code": "E28A",
        "verdict": NOT_COVERED,
    }
    assert resolve("E28", answers(route, sponsor="no")) == {
        "code": "E28A",
        "verdict": NEEDS_SPONSOR,
    }


@pytest.mark.parametrize("route,code", list(PATHS.items())[1:])
def test_no_sponsor_routes_ignore_irrelevant_answers(route: str, code: str) -> None:
    assert resolve(
        "E28", {"investment_route": route, commitment(code, 5): "yes", "stay_months": 60}
    ) == {
        "code": code,
        "verdict": OK,
        "stay_option": {"value": 5, "unit": "YEAR"},
    }
    assert resolve("E28", answers(route, sponsor="no", own_shareholding="no"))["verdict"] == OK


@pytest.mark.parametrize("question", ["investment_route", "own_shareholding", "sponsor"])
@pytest.mark.parametrize("value", [None, "", "invalid", True, False, 0, 1])
def test_invalid_choices_ask_again(question: str, value: object) -> None:
    assert (
        resolve("E28", answers("shareholder_in_sponsoring_company", **{question: value}))["ask"]
        == question
    )


@pytest.mark.parametrize("value", [None, "", 0, -1, True, False, 1.5, "0", "1.5", "１２", " 12 "])
@pytest.mark.parametrize("route", PATHS)
def test_invalid_months_ask_again(route: str, value: object) -> None:
    assert resolve("E28", answers(route, stay_months=value)) == {
        "ask": "stay_months",
        "code": PATHS[route],
    }


def test_itap_unknown_for_every_code() -> None:
    evidence = json.loads((LAW_DIR / "ITAP.json").read_text())
    assert evidence["explicitly_convertible_codes"] is None
    assert {item["code"] for item in evidence["code_assessments"]} == set(DATA["visas"])
    for item in evidence["code_assessments"]:
        assert item["as_stated_boolean"] is None and item["status"] == "TO_VERIFY"
        assert law(item["code"])["itap_eligibility_as_stated_boolean"] is None


GOLDEN = list(PATHS.items())[1:]


def tier_section(code: str, tier: int) -> str:
    card = law(code)["persyaratan_khusus_verbatim"]
    return re.search(
        rf"Visa {tier} tahun\n(.*?)(?=\nVisa \d+ tahun|\nDaftar |\Z)", card, re.S
    ).group(1)


@pytest.mark.parametrize("route,code", GOLDEN)
def test_commitment_is_asked_for_the_selected_tier_with_that_tier_amount(
    route: str, code: str
) -> None:
    visa = DATA["visas"][code]
    assert "requires_question" not in visa
    assert visa["requires_question_by_stay"] == {
        "5": commitment(code, 5),
        "10": commitment(code, 10),
    }
    for tier, months in ((5, 60), (10, 61)):
        gate = commitment(code, tier)
        base = {"investment_route": route, "stay_months": months}
        assert resolve("E28", base) == {"ask": gate, "code": code}
        for bad in (None, "", "invalid", True, 0):
            assert resolve("E28", {**base, gate: bad}) == {"ask": gate, "code": code}
        option = {"value": tier, "unit": "YEAR"}
        assert resolve("E28", {**base, gate: "yes"}) == {
            "code": code,
            "verdict": OK,
            "stay_option": option,
        }
        assert resolve("E28", {**base, gate: "no"}) == {
            "code": code,
            "verdict": NOT_COVERED,
            "stay_option": option,
        }
        # The question quotes exactly the amounts of that tier's section of the card.
        text = DATA["questions"][gate]["text_en"]
        asked = {
            int(float(n.replace(",", "")) * (10**6 if m else 1))
            for n, m in re.findall(r"USD ([\d,]+(?:\.\d+)?)( million)?", text)
        }
        stated = {
            int(n.replace(".", "")) for n in re.findall(r"US\$([\d.]+\d)", tier_section(code, tier))
        }
        assert asked == stated, (code, tier, asked, stated)
        assert f"{tier}-year" in text


@pytest.mark.parametrize("route,code", GOLDEN)
def test_other_tier_answer_never_satisfies_the_selected_tier(route: str, code: str) -> None:
    # 120 months needs the 10-year amount: meeting only the 5-year amount is NOT_COVERED.
    five_only = {commitment(code, 5): "yes", commitment(code, 10): "no"}
    ten = {"investment_route": route, "stay_months": 120, **five_only}
    assert resolve("E28", ten) == {
        "code": code,
        "verdict": NOT_COVERED,
        "stay_option": {"value": 10, "unit": "YEAR"},
    }
    # 60 months with the 5-year amount is OK 5 YEAR, whatever the 10-year answer.
    five = {"investment_route": route, "stay_months": 60, **five_only}
    assert resolve("E28", five) == {
        "code": code,
        "verdict": OK,
        "stay_option": {"value": 5, "unit": "YEAR"},
    }
    # 61 months with the 10-year amount is OK 10 YEAR.
    ten_ok = {
        "investment_route": route,
        "stay_months": 61,
        commitment(code, 5): "no",
        commitment(code, 10): "yes",
    }
    assert resolve("E28", ten_ok) == {
        "code": code,
        "verdict": OK,
        "stay_option": {"value": 10, "unit": "YEAR"},
    }
    # Over the longest stay is decided before any commitment is asked.
    if code != "E28G":
        assert resolve("E28", {"investment_route": route, "stay_months": 121}) == {
            "code": code,
            "verdict": STAY_TOO_LONG,
        }
