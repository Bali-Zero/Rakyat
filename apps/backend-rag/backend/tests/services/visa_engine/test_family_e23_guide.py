"""E23: all initial-stay leaves, source conditions and fail-closed secondary codes."""

import json

import pytest

from backend.services.visa_engine.families import guide
from backend.services.visa_engine.families.guide import NEEDS_SPONSOR, OK, resolve

DATA = guide.load_family("E23")
LAW_DIR = guide.HERE / "law" / "E23"
STAYS = {
    "180_days": {"value": 180, "unit": "DAY"},
    "1_year": {"value": 1, "unit": "YEAR"},
    "2_years": {"value": 2, "unit": "YEAR"},
}
SECONDARY = {"E23A", "E23U", "E23V", "E23X", "E23Y"}


def answers(**extra: object) -> dict:
    return {"activity": "employment", "sponsor": "yes", "initial_stay": "180_days", **extra}


@pytest.mark.parametrize("choice,option", STAYS.items())
def test_each_leaf_requires_work_sponsor_then_exact_initial_stay(choice: str, option: dict) -> None:
    current = {}
    for question, answer in (
        ("activity", "employment"),
        ("sponsor", "yes"),
        ("initial_stay", choice),
    ):
        result = resolve("E23", current)
        assert result["ask"] == question
        assert set(result) <= {"ask", "code"}
        current[question] = answer
    assert resolve("E23", current) == {"code": "E23", "verdict": OK, "stay_option": option}


@pytest.mark.parametrize("choice", STAYS)
def test_no_sponsor_cannot_be_overridden_by_stay_or_document_answers(choice: str) -> None:
    assert resolve(
        "E23",
        answers(sponsor="no", initial_stay=choice, work_permit="yes", travel_document="passport"),
    ) == {"code": "E23", "verdict": NEEDS_SPONSOR}


@pytest.mark.parametrize("question", ["activity", "sponsor", "initial_stay"])
@pytest.mark.parametrize("value", [None, "", "invalid", True, False, 0, 1, "YES"])
def test_invalid_choices_ask_again(question: str, value: object) -> None:
    expected = {"ask": question}
    if question != "activity":
        expected["code"] = "E23"
    assert resolve("E23", answers(**{question: value})) == expected


@pytest.mark.parametrize(
    "value",
    [
        180,
        "180",
        "6_months",
        "12_months",
        "365_days",
        "730_days",
        [],
        {},
        {"value": 180, "unit": "DAY"},
    ],
)
def test_initial_choice_never_converts_or_accepts_an_arbitrary_duration(value: object) -> None:
    assert resolve("E23", answers(initial_stay=value)) == {"ask": "initial_stay", "code": "E23"}


@pytest.mark.parametrize(
    "extra", [{"stay_days": 180}, {"stay_months": 6}, {"stay_months": 24}, {"stay_months": 1200}]
)
def test_numeric_stay_cannot_bypass_discrete_choice(extra: dict) -> None:
    assert resolve("E23", {"activity": "employment", "sponsor": "yes", **extra}) == {
        "ask": "initial_stay",
        "code": "E23",
    }


@pytest.mark.parametrize(
    "activity", [*sorted(SECONDARY), "E23-FREELANCE", "freelance", "remote_work", "holiday"]
)
def test_missing_pages_and_unsupported_activities_have_no_fallback(activity: str) -> None:
    assert resolve("E23", answers(activity=activity)) == {"ask": "activity"}


def test_guide_conditions_and_stay_units_come_from_the_populated_card() -> None:
    card = json.loads((LAW_DIR / "E23.json").read_text())
    visa = DATA["visas"]["E23"]
    assert set(DATA["questions"]) == {"activity", "sponsor", "initial_stay"}
    assert DATA["questions"]["activity"]["options"] == {"employment": "E23"}
    assert "bekerja dengan penjamin di Indonesia" in card["definition_verbatim"]
    assert visa["sponsor_required"] is card["sponsor_required"]["as_stated_boolean"] is True
    assert "penjamin yang memberikan kerja" in card["requirements_verbatim"][0]["text"]
    assert "180 hari, 1 tahun, atau 2 tahun" in card["stay_days"][0]
    assert visa["stay_options"] == list(STAYS.values())
    assert DATA["questions"]["initial_stay"]["options"] == STAYS
    assert visa["extendable"] is True
    assert "dapat diperpanjang" in " ".join(card["extendable"])
    assert visa.get("max_total_days") is visa.get("max_total_months") is None
    assert card["total_max_verbatim"] is None
    # All seven requirements survive as a checklist, including the work permission.
    assert len(card["requirements_verbatim"]) == 7
    assert card["requirements_verbatim"][6]["text"] == "Izin bekerja dari Kementerian Tenaga Kerja."
    assert (
        "kecuali diperlukan dalam pekerjaan tersebut"
        in card["prohibited_or_conditions_verbatim"][1]
    )


def test_question_option_cannot_approve_a_stay_absent_from_the_law(monkeypatch) -> None:
    monkeypatch.setitem(
        DATA["questions"]["initial_stay"]["options"], "3_years", {"value": 3, "unit": "YEAR"}
    )
    assert resolve("E23", answers(initial_stay="3_years")) == {"ask": "initial_stay", "code": "E23"}


def test_itap_assessments_remain_exactly_unknown_per_code() -> None:
    evidence = json.loads((LAW_DIR / "ITAP.json").read_text())
    assert evidence["eligible_codes_as_stated"] is None
    assert {row["code"] for row in evidence["codes"]} == set(DATA["visas"])
    for row in evidence["codes"]:
        card = json.loads((LAW_DIR / f"{row['code']}.json").read_text())
        assert card["itap_eligibility_as_stated_boolean"] is row["eligible_as_stated"] is None
        assert card["itap_status"] == row["status"] == "TO_VERIFY"
        assert card["itap_verbatim"] == row["visa_page_itap_verbatim"]
