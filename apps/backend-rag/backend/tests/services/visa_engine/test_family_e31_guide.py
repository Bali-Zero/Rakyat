"""E31: source-derived relationships, Golden Visa exclusions and calendar stays."""

import json

import pytest

from backend.services.visa_engine.families import guide
from backend.services.visa_engine.families.guide import (
    NEEDS_SPONSOR,
    NOT_COVERED,
    OK,
    OK_WITH_EXTENSION,
    resolve,
)

DATA = guide.load_family("E31")
LAW_DIR = guide.HERE / "law" / "E31"
# Independently transcribed from the titles and special requirements, not legacy packs.
PATHS = {
    "spouse_indonesian_citizen": "E31A",
    "spouse_itas_itap_holder": "E31B",
    "child_of_lawful_foreign_indonesian_marriage": "E31C",
    "foreign_stepchild_of_lawful_foreign_indonesian_marriage": "E31D",
    "child_of_itas_itap_or_valid_limited_stay_visa_holder": "E31E",
    "child_indonesian_parent_court_established_relationship": "E31F",
    "parent_of_indonesian_child": "E31G",
    "parent_of_child_with_itas_itap_or_valid_limited_stay_visa": "E31H",
    "child_joining_sibling_with_itas_itap_or_valid_limited_stay_visa": "E31J",
}
GATED = {"E31E", "E31H"}


def law(code: str) -> dict:
    return json.loads((LAW_DIR / f"{code}.json").read_text())


def answers(relationship: str, **extra: object) -> dict:
    return {
        "family_relationship": relationship,
        "not_joining_golden_visa": "yes",
        "sponsor": "yes",
        "stay_months": 12,
        **extra,
    }


def test_only_substantive_codes_are_reachable() -> None:
    assert DATA["questions"]["family_relationship"]["options"] == PATHS
    assert set(DATA["visas"]) == {"E31", *PATHS.values()}
    assert law("E31")["status"] == "NOT_FOUND"
    assert law("E31")["not_found_evidence_verbatim"] == "Data Belum Tersedia"
    assert DATA["visas"]["E31"]["stay_options"] is None
    assert len(DATA["questions"]) == 4


@pytest.mark.parametrize("code", PATHS.values())
def test_initial_stays_entry_and_sponsor_match_law(code: str) -> None:
    card = law(code)
    visa = DATA["visas"][code]
    assert card["status"] == "VERIFIED_FROM_SOURCE"
    assert visa["sponsor_required"] is card["sponsor_required"]["as_stated_boolean"] is True
    assert visa["entry_type"] == card["entry"].upper() == "MULTIPLE"
    assert "1 tahun atau 2 tahun" in " ".join(card["stay_days"])
    assert visa["stay_options"] == [{"value": 1, "unit": "YEAR"}, {"value": 2, "unit": "YEAR"}]
    assert visa["extendable"] is True
    assert "dapat diperpanjang" in " ".join(card["extendable"])
    assert card["total_max_verbatim"] is None
    assert all(k not in visa for k in ("stay_days", "max_total_days", "max_total_months"))


@pytest.mark.parametrize("relationship,code", PATHS.items())
def test_every_path_asks_at_most_four_questions(relationship: str, code: str) -> None:
    assert resolve("E31", {}) == {"ask": "family_relationship"}
    a = {"family_relationship": relationship}
    if code in GATED:
        assert resolve("E31", a) == {"ask": "not_joining_golden_visa", "code": code}
        a["not_joining_golden_visa"] = "yes"
    assert resolve("E31", a) == {"ask": "sponsor", "code": code}
    a["sponsor"] = "yes"
    assert resolve("E31", a) == {"ask": "stay_months", "code": code}
    a["stay_months"] = 12
    assert resolve("E31", a) == {
        "code": code,
        "verdict": OK,
        "stay_option": {"value": 1, "unit": "YEAR"},
    }


@pytest.mark.parametrize("relationship,code", PATHS.items())
@pytest.mark.parametrize("months,year", [(1, 1), (12, 1), (13, 2), (24, 2), ("13", 2)])
def test_initial_month_boundaries(relationship: str, code: str, months: object, year: int) -> None:
    assert resolve("E31", answers(relationship, stay_months=months)) == {
        "code": code,
        "verdict": OK,
        "stay_option": {"value": year, "unit": "YEAR"},
    }


@pytest.mark.parametrize("relationship,code", PATHS.items())
@pytest.mark.parametrize("months", [25, 1200])
def test_extension_does_not_invent_a_total_maximum(
    relationship: str, code: str, months: int
) -> None:
    assert resolve("E31", answers(relationship, stay_months=months)) == {
        "code": code,
        "verdict": OK_WITH_EXTENSION,
        "stay_option": {"value": 2, "unit": "YEAR"},
        "total_max_known": False,
    }


@pytest.mark.parametrize("relationship,code", PATHS.items())
def test_sponsor_is_required_on_every_path(relationship: str, code: str) -> None:
    assert resolve("E31", answers(relationship, sponsor="no")) == {
        "code": code,
        "verdict": NEEDS_SPONSOR,
    }


@pytest.mark.parametrize("relationship,code", PATHS.items())
def test_golden_visa_exclusion_is_per_code(relationship: str, code: str) -> None:
    prohibited = " ".join(law(code)["prohibited_or_conditions_verbatim"])
    assert ("golden visa" in prohibited) is (code in GATED)
    assert (DATA["visas"][code].get("requires_question") == "not_joining_golden_visa") is (
        code in GATED
    )
    result = resolve("E31", answers(relationship, not_joining_golden_visa="no"))
    assert result["verdict"] == (NOT_COVERED if code in GATED else OK)
    if code in GATED:
        assert resolve(
            "E31", answers(relationship, not_joining_golden_visa="no", sponsor="no")
        ) == {
            "code": code,
            "verdict": NOT_COVERED,
        }


@pytest.mark.parametrize("question", ["family_relationship", "not_joining_golden_visa", "sponsor"])
@pytest.mark.parametrize("value", [None, "", "invalid", True, False, 1, 0])
def test_invalid_choices_repeat_question(question: str, value: object) -> None:
    a = answers("child_of_itas_itap_or_valid_limited_stay_visa_holder", **{question: value})
    assert resolve("E31", a)["ask"] == question


@pytest.mark.parametrize("value", [None, "", "0", 0, -1, True, False, 1.5, "1.5", "１２", " 12 "])
def test_invalid_months_repeat_question(value: object) -> None:
    assert resolve("E31", answers("spouse_indonesian_citizen", stay_months=value)) == {
        "ask": "stay_months",
        "code": "E31A",
    }


def test_relationships_are_not_the_old_generic_child_gate() -> None:
    assert "Perkawinan Sah WNA-WNI" in law("E31C")["official_name_id"]
    assert "Anak Bawaan WNA" in law("E31D")["official_name_id"]
    assert "ayah/ibu Warga Negara Indonesia" in law("E31D")["requirements_verbatim"][0]["text"]
    assert "Putusan pengadilan Indonesia" in law("E31F")["persyaratan_khusus_verbatim"]
    assert resolve("E31", {"family_relationship": "child_of_wni"}) == {"ask": "family_relationship"}
    assert (
        not {"age", "marital_status", "birth_certificate", "court_order"} & DATA["questions"].keys()
    )


@pytest.mark.parametrize("code", ["E31E", "E31H", "E31J"])
def test_current_relative_visa_is_a_documented_alternative(code: str) -> None:
    assert "Visa Tinggal Terbatas" in law(code)["persyaratan_khusus_verbatim"]
    relationship = next(path for path, target in PATHS.items() if target == code)
    assert "valid_limited_stay_visa" in relationship


def test_itap_stage_preserves_unknown_per_code_and_separate_direct_context() -> None:
    evidence = json.loads((LAW_DIR / "ITAP.json").read_text())
    assert {m["code"] for m in evidence["code_mapping"]} == set(DATA["visas"])
    assert evidence["itap_direct_verbatim"][0]["codes_stated"] == ["E31A", "E31B", "E31C"]
    for mapping in evidence["code_mapping"]:
        assert mapping["itas_to_itap_eligible_as_stated"] is None
        assert mapping["status"] == "TO_VERIFY"
        assessment = law(mapping["code"])["itap_assessment"]
        assert assessment["itas_to_itap_eligible_as_stated"] is None
        assert assessment["direct_itap_context_present"] is (
            mapping["code"] in {"E31A", "E31B", "E31C"}
        )
