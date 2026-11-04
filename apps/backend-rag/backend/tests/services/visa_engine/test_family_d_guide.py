"""Family D: independent Law-derived leaves, eligibility and day boundaries."""

import json

import pytest

from backend.services.visa_engine.families import guide
from backend.services.visa_engine.families.guide import (
    NEEDS_SPONSOR,
    OK,
    OK_WITH_EXTENSION,
    STAY_TOO_LONG,
    resolve,
)

FAMILY = "D"
DATA = guide.load_family(FAMILY)
PATHS = {
    "holiday_family_events_transit": "D1",
    "business_meetings_negotiations_purchases": "D2",
    "medical_treatment": "D3",
    "arts_cultural_performance": "D7",
    "pre_investment_surveys": "D12",
    "film_production": "D14",
    "company_branch_audit_inspection": "D17",
}
# Expected values read from the official cards, not from the configuration under test.
# D12's total is the owner ruling of 2026-10-19: 180 days per entry (the card's passages conflict).
LIMITS = {
    "D1": (60, 180),
    "D2": (60, 180),
    "D3": (60, 180),
    "D7": (30, 30),
    "D12": (180, 180),
    "D14": (60, 180),
    "D17": (60, 180),
}
# Owner ruling 2026-10-19: Bali Zero is an agency and always sponsors; no sponsor question.
QUESTIONS = {code: ["stay_days"] for code in PATHS.values()}
GOOD = {
    "eligible_nationality": "yes",
    "travel_status": "national_passport",
    "travel_document": "not_in_exception",
    "sponsor": "yes",
    "stay_days": 1,
}
PATH_GOOD = {
    "eligible_nationality": "yes",
    "travel_status": "national_passport",
    "travel_document": "listed_nationality",
    "sponsor": "yes",
    "stay_days": 1,
}


def answers(activity: str, **extra: object) -> dict:
    return {"activity": activity, **GOOD, **extra}


def card(code: str) -> dict:
    return json.loads((guide.HERE / "law" / FAMILY / f"{code}.json").read_text())


def test_only_substantive_law_cards_are_reachable() -> None:
    assert DATA["questions"]["activity"]["options"] == PATHS
    assert {c for c in DATA["visas"] if card(c)["status"] == "VERIFIED_FROM_SOURCE"} == set(
        PATHS.values()
    )
    assert {p.stem for p in (guide.HERE / "law" / FAMILY).glob(f"{FAMILY}*.json")} == set(
        DATA["visas"]
    )


@pytest.mark.parametrize("activity,code", PATHS.items())
def test_each_path_in_order_at_most_four_questions(activity: str, code: str) -> None:
    assert resolve(FAMILY, {}) == {"ask": "activity"}
    a = {"activity": activity}
    for question in QUESTIONS[code]:
        assert resolve(FAMILY, a) == {"ask": question, "code": code}
        a[question] = PATH_GOOD[question]
    initial, _ = LIMITS[code]
    assert resolve(FAMILY, a) == {
        "code": code,
        "verdict": OK,
        "stay_option": {"value": initial, "unit": "DAY"},
    }
    assert len(QUESTIONS[code]) + 1 <= 4


@pytest.mark.parametrize("activity,code", PATHS.items())
def test_all_day_boundaries_and_numeric_strings(activity: str, code: str) -> None:
    initial, total = LIMITS[code]
    for days in range(1, (total or initial + 1) + 1):
        result = resolve(FAMILY, answers(activity, stay_days=days))
        expected = {
            "code": code,
            "verdict": OK if days <= initial else OK_WITH_EXTENSION,
            "stay_option": {"value": initial, "unit": "DAY"},
        }
        if days > initial:
            expected["total_max_known"] = total is not None
        assert result == expected
    if total is not None:
        for days in (total + 1, 10000):
            assert resolve(FAMILY, answers(activity, stay_days=days)) == {
                "code": code,
                "verdict": STAY_TOO_LONG,
            }
    assert resolve(FAMILY, answers(activity, stay_days=str(initial)))["verdict"] == OK


@pytest.mark.parametrize("activity,code", PATHS.items())
@pytest.mark.parametrize(
    "invalid", [None, "", 0, -1, True, False, 1.5, "1.5", "0", "１２", " 30 ", "-1"]
)
def test_invalid_day_values_ask_again(activity: str, code: str, invalid: object) -> None:
    assert resolve(FAMILY, answers(activity, stay_days=invalid)) == {
        "ask": "stay_days",
        "code": code,
    }


@pytest.mark.parametrize("invalid", [None, "", "unknown", "D4", True, 1])
def test_invalid_activity_never_selects_a_code(invalid: object) -> None:
    assert resolve(FAMILY, {"activity": invalid}) == {"ask": "activity"}


@pytest.mark.parametrize("activity,code", PATHS.items())
def test_structured_fields_match_verbatim_law(activity: str, code: str) -> None:
    visa, source = DATA["visas"][code], card(code)
    initial, total = LIMITS[code]
    assert f"{initial} hari" in " ".join(source["stay_days"])
    assert visa["stay_options"] == [{"value": initial, "unit": "DAY"}]
    assert visa["stay_days"] == initial
    assert visa["max_total_days"] == total
    assert visa["sponsor_required"] is source["sponsor_required"]["as_stated_boolean"]
    if code == "D12":
        assert visa["max_total_days"] == visa["stay_days"] == 180  # owner ruling, per entry
    assert source["entry"].upper() == DATA["entry_type"]
    assert visa["extendable"] is (FAMILY != "A" and code != "D7")
    assert "max_total_months" not in visa


@pytest.mark.parametrize("activity,code", PATHS.items())
def test_invalid_condition_values_ask_again(activity: str, code: str) -> None:
    for question in QUESTIONS[code]:
        if question == "stay_days":
            continue
        for value in (None, "", "unknown", True, 1):
            assert resolve(FAMILY, {"activity": activity, **PATH_GOOD, question: value}) == {
                "ask": question,
                "code": code,
            }


@pytest.mark.parametrize("activity,code", PATHS.items())
@pytest.mark.parametrize(
    "extra",
    [
        {"sponsor": "no"},
        {"travel_document": "listed_nationality", "sponsor": "no"},
        {"travel_document": "not_in_exception"},
        {},
    ],
)
def test_agency_sponsor_ruling_never_asks_or_returns_needs_sponsor(
    activity: str, code: str, extra: dict
) -> None:
    # Owner ruling 2026-10-19: Bali Zero is an agency and can always sponsor.
    result = resolve(FAMILY, {"activity": activity, "stay_days": 1, **extra})
    assert result["verdict"] == OK
    assert result["verdict"] != NEEDS_SPONSOR
    for days in range(1, 400):
        result = resolve(FAMILY, {"activity": activity, "stay_days": days, **extra})
        assert result.get("verdict") != NEEDS_SPONSOR
        assert result.get("ask") not in {"sponsor", "travel_document"}


def test_agency_sponsor_ruling_is_config_and_keeps_the_law_sponsor_column() -> None:
    from backend.services.visa_engine.families import importer

    assert DATA["agency_sponsors"] is True
    assert "sponsor" not in DATA["questions"] and "travel_document" not in DATA["questions"]
    assert any(n.startswith("OWNER RULING 2026-10-19 (agency_sponsors") for n in DATA["notes"])
    sponsor = {row["code"]: row["sponsor"] for row in importer.build_rows(FAMILY)}
    assert sponsor["D1"] == sponsor["D2"] == "CONDITIONAL"
    assert sponsor["D3"] == sponsor["D7"] == sponsor["D14"] == sponsor["D17"] == "REQUIRED"
    assert sponsor["D12"] == "NOT_REQUIRED"
    assert "Kecuali" in " ".join(card("D1")["sponsor_required"]["verbatim"])


def test_d12_owner_ruling_caps_each_entry_at_180_days() -> None:
    base = {"activity": "pre_investment_surveys"}
    assert resolve(FAMILY, {**base, "stay_days": 180}) == {
        "code": "D12",
        "verdict": OK,
        "stay_option": {"value": 180, "unit": "DAY"},
    }
    for days in (181, 200, 365):
        assert resolve(FAMILY, {**base, "stay_days": days}) == {
            "code": "D12",
            "verdict": STAY_TOO_LONG,
        }
    # The page's conflicting passages stay quoted verbatim in the card.
    assert "180 hari berikutnya" in card("D12")["jenis_visa_verbatim"]
    assert "12 bulan (1 tahun) atau 2 tahun" in " ".join(card("D12")["total_max_verbatim"])
    assert len(card("D12")["conflicts"]) == 2
    assert DATA["visas"]["D12"]["max_total_days"] == 180
    assert any("maximum stay is 180 days per entry" in n for n in DATA["notes"])
    (ruling,) = card("D12")["owner_rulings"]
    assert ruling["date"] == "2026-10-19" and ruling["topic"] == "max_stay_per_entry"
    assert "180 days per entry" in ruling["ruling"]
    assert "Pasal 95 ayat (4)" in ruling["history"] and "PLAUSIBLE" in ruling["history"]


def test_d7_allows_activity_remuneration_but_not_indonesian_employment() -> None:
    conditions = " ".join(card("D7")["ketentuan_lain_verbatim"])
    assert "diperkenankan menerima imbalan" in conditions
    assert "dilarang bekerja dalam hubungan kerja" in conditions
    assert DATA["visas"]["D7"]["extendable"] is False


def test_secondary_pages_are_never_reachable_and_fields_stay_unknown() -> None:
    for code in ("D4", "D8"):
        assert card(code)["status"] == "NOT_FOUND"
        assert "Data Belum Tersedia" in card(code)["not_found_reason"]
        assert code not in PATHS.values()
        assert all(
            DATA["visas"][code][field] is None
            for field in (
                "sponsor",
                "entry_type",
                "stay_days",
                "stay_options",
                "extendable",
                "is_sold",
            )
        )
