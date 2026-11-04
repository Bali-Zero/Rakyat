"""Family B: independent Law-derived leaves, eligibility and day boundaries."""

import json

import pytest

from backend.services.visa_engine.families import guide
from backend.services.visa_engine.families.guide import (
    NOT_COVERED,
    OK,
    OK_WITH_EXTENSION,
    STAY_TOO_LONG,
    resolve,
)

FAMILY = "B"
DATA = guide.load_family(FAMILY)
PATHS = {"holiday_family_meetings_transit": "B1", "official_government_duties": "B4"}
# Expected values read from the official cards, not from the configuration under test.
LIMITS = {"B1": (30, 60), "B4": (30, 60)}
QUESTIONS = {
    "B1": ["nationality", "travel_status", "stay_days"],  # owner ruling 2026-10-19
    "B4": ["eligible_nationality", "travel_status", "stay_days"],
}
GOOD = {
    "eligible_nationality": "yes",
    "nationality": "TH",  # on both the A1 and the B1 list
    "travel_status": "national_passport",
    "travel_document": "not_in_exception",
    "sponsor": "yes",
    "stay_days": 1,
}
PATH_GOOD = {
    "eligible_nationality": "yes",
    "nationality": "TH",  # on both the A1 and the B1 list
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
@pytest.mark.parametrize("travel_status", ["national_passport", "temporary_or_emergency_passport"])
def test_allowed_travel_statuses(activity: str, code: str, travel_status: str) -> None:
    assert resolve(FAMILY, answers(activity, travel_status=travel_status))["verdict"] == OK
    assert "temporary passport" in " ".join(card(code)["ketentuan_lain_verbatim"])


@pytest.mark.parametrize("activity,code", PATHS.items())
def test_excluded_status_or_nationality_never_passes(activity: str, code: str) -> None:
    for extra in (
        {"eligible_nationality": "no"} if code == "B4" else {"nationality": "MO"},
        {"travel_status": "stateless_or_other_non_national_document"},
    ):
        assert resolve(FAMILY, answers(activity, **extra)) == {"code": code, "verdict": NOT_COVERED}
    assert "sponsor" not in DATA["questions"]


def test_b1_is_initially_thirty_days_with_one_extension() -> None:
    assert (
        resolve(FAMILY, answers("holiday_family_meetings_transit", stay_days=31))["verdict"]
        == OK_WITH_EXTENSION
    )
    assert "satu kali" in " ".join(card("B1")["extendable"])
    assert "90 hari" in " ".join(card("B1")["ketentuan_lain_verbatim"])
    assert DATA["visas"]["B1"]["stay_options"] == [{"value": 30, "unit": "DAY"}]


# --- owner ruling 2026-10-19: B1 nationality list -------------------------------------
A1_LIST = [
    "BN",
    "PH",
    "KH",
    "LA",
    "MY",
    "MM",
    "SG",
    "TH",
    "VN",
    "TL",
    "SR",
    "CO",
    "HK",
    "TR",
    "BR",
    "PE",
    "KZ",
    "MO",
    "BY",
]
HOLIDAY = "holiday_family_meetings_transit"


def test_b1_card_carries_the_official_portal_list() -> None:
    field = card("B1")["eligible_nationalities"]
    assert field["authority_type"] == "OFFICIAL_PORTAL"
    instrument = field["instrument"]
    assert instrument["number"] == "M.HH-02.GR.01.06 Tahun 2024"
    assert instrument["ditetapkan"] == "2024-01-09"
    assert instrument["revokes"] == "M.HH-04.GR.01.06 Tahun 2023"
    assert instrument["secondary_source"]["page_date"] == "2024-01-16"
    assert "97-entry portal list" in instrument["to_verify"]
    assert "none was identified" not in json.dumps(card("B1"))
    assert field["not_page_verbatim"] is True and field["retrieved_at"] == "2026-10-19"
    entries = [e["iso2"] for e in field["entries"]]
    assert len(entries) == len(set(entries)) == 97
    assert set(entries) <= guide.ISO_3166_ALPHA2
    assert field["sources"][0]["url"] == (
        "https://www.imigrasi.go.id/wna/daftar-negara-voa-bvk-calling-visa/daftar-negara-subjek-visa-on-arrival"
    )
    assert guide.eligible_nationalities("law/B", "B1") == frozenset(entries)


def test_b1_never_excludes_a1_eligible_nationals() -> None:
    b1 = guide.eligible_nationalities("law/B", "B1")
    overlap = sorted(set(A1_LIST) & b1)
    assert len(overlap) == 18 and set(A1_LIST) - b1 == {"MO"}
    for iso2 in overlap:
        assert resolve(FAMILY, answers(HOLIDAY, nationality=iso2))["verdict"] == OK
    assert resolve(FAMILY, answers(HOLIDAY, nationality="MO")) == {
        "code": "B1",
        "verdict": NOT_COVERED,
    }


@pytest.mark.parametrize(
    "iso2",
    sorted(
        json.loads((guide.HERE / "law" / "B" / "B1.json").read_text())["eligible_nationalities"][
            "entries"
        ][i]["iso2"]
        for i in range(97)
    ),
)
def test_every_listed_nationality_selects_b1(iso2: str) -> None:
    assert resolve(FAMILY, answers(HOLIDAY, nationality=iso2.lower())) == {
        "code": "B1",
        "verdict": OK,
        "stay_option": {"value": 30, "unit": "DAY"},
    }


@pytest.mark.parametrize("unknown", ["XX", "USA", "", None, 7])
def test_unknown_b1_nationality_is_asked_again(unknown: object) -> None:
    assert resolve(FAMILY, answers(HOLIDAY, nationality=unknown)) == {
        "ask": "nationality",
        "code": "B1",
    }
