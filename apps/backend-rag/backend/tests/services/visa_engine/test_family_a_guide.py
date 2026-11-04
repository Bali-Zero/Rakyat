"""Family A: independent Law-derived leaves, eligibility and day boundaries."""

import json

import pytest

from backend.services.visa_engine.families import guide
from backend.services.visa_engine.families.guide import (
    NOT_COVERED,
    OK,
    OK_WITH_EXTENSION,
    STAY_TOO_LONG,
    TO_VERIFY,
    resolve,
)

FAMILY = "A"
DATA = guide.load_family(FAMILY)
PATHS = {
    "holiday_family_meetings_transit": "A1",
    "official_government_duties": "A4",
    "active_transport_crew": "A36",
    "crew_arriving_on_operating_vessel": "A37",
}
# Expected values read from the official cards, not from the configuration under test.
LIMITS = {"A1": (30, 30), "A4": (30, 30), "A36": (30, 30), "A37": (30, 30)}
QUESTIONS = {
    "A1": ["nationality", "travel_status", "stay_days"],  # owner ruling 2026-10-19
    "A4": ["eligible_nationality", "travel_status", "stay_days"],
    "A36": ["travel_status", "stay_days"],
    "A37": ["travel_status", "stay_days"],
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
def test_non_national_documents_are_excluded_without_sponsor_question(
    activity: str, code: str
) -> None:
    assert resolve(
        FAMILY, answers(activity, travel_status="stateless_or_non_national_document")
    ) == {"code": code, "verdict": NOT_COVERED}
    assert "sponsor" not in DATA["questions"]


def test_unconfirmed_nationality_cannot_pass() -> None:
    assert (
        resolve(FAMILY, answers("official_government_duties", eligible_nationality="no"))["verdict"]
        == NOT_COVERED
    )
    for code in ("A1", "A4"):
        assert "warga negara tertentu" in card(code)["jenis_visa_verbatim"]


# --- owner ruling 2026-10-19: A1 nationality list -------------------------------------
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


def test_a1_card_carries_the_sourced_regulation_list() -> None:
    field = card("A1")["eligible_nationalities"]
    assert field["authority_type"] == "REGULATION" and field["not_page_verbatim"] is True
    assert field["retrieved_at"] == "2026-10-19"
    assert [e["iso2"] for e in field["entries"]] == A1_LIST
    bases = [e["basis"] for e in field["entries"]]
    assert sum(b.startswith("Perpres 95/2024 Lampiran A") for b in bases) == 13
    assert sum(b.startswith("Permenimipas 10/2026") for b in bases) == 6
    assert {s["url"] for s in field["sources"]} >= {
        "https://peraturan.go.id/files/perpres-no-95-tahun-2024.pdf",
        "https://peraturan.bpk.go.id/Download/416328/Permen_Nomor10_Tahun2026.pdf",
    }
    assert field["permit_holder_route"]["basis"] == "Perpres 95/2024 Lampiran B"
    assert field["permit_holder_route"]["guide_verdict"] == "TO_VERIFY"
    assert "Kepulauan Riau" in field["permit_holder_route"]["to_verify"]
    assert guide.eligible_nationalities("law/A", "A1") == frozenset(A1_LIST)
    assert set(A1_LIST) <= guide.ISO_3166_ALPHA2 and len(guide.ISO_3166_ALPHA2) == 249


@pytest.mark.parametrize("iso2", A1_LIST)
def test_every_listed_nationality_selects_a1(iso2: str) -> None:
    for answer in (iso2, iso2.lower(), f" {iso2} "):
        result = resolve(FAMILY, answers(HOLIDAY, nationality=answer))
        assert result == {"code": "A1", "verdict": OK, "stay_option": {"value": 30, "unit": "DAY"}}


@pytest.mark.parametrize("iso2", ["US", "AU", "IT", "ID", "CN", "IN"])
def test_unlisted_nationality_is_not_a1_unless_singapore_permit_holder(iso2: str) -> None:
    a = answers(HOLIDAY, nationality=iso2)
    assert resolve(FAMILY, a) == {"ask": "singapore_permit_holder", "code": "A1"}
    assert resolve(FAMILY, {**a, "singapore_permit_holder": "no"}) == {
        "code": "A1",
        "verdict": NOT_COVERED,
    }
    # Gate 2026-10-19: the Lampiran B route is unsettled (10/2026 dropped the wording; NB-2:
    # certain Kepri entry points only), so a holder gets TO_VERIFY, never OK.
    assert resolve(FAMILY, {**a, "singapore_permit_holder": "yes"}) == {
        "code": "A1",
        "verdict": TO_VERIFY,
    }
    assert resolve(FAMILY, {**a, "singapore_permit_holder": "maybe"}) == {
        "ask": "singapore_permit_holder",
        "code": "A1",
    }


@pytest.mark.parametrize("unknown", ["XX", "ZZ", "USA", "Thailand", "", None, 1, True, "T H"])
def test_unknown_nationality_is_asked_again(unknown: object) -> None:
    assert resolve(FAMILY, answers(HOLIDAY, nationality=unknown)) == {
        "ask": "nationality",
        "code": "A1",
    }
    # The bare self-confirmation no longer decides A1.
    assert resolve(
        FAMILY, {**answers(HOLIDAY, eligible_nationality="yes"), "nationality": unknown}
    ) == {
        "ask": "nationality",
        "code": "A1",
    }


def test_operating_vessel_route_preserves_a37_specific_scope() -> None:
    assert "datang langsung dengan Alat Angkutnya" in card("A37")["definition_verbatim"]
    assert "Zona Ekonomi Eksklusif" in card("A37")["definition_verbatim"]
    assert (
        "foreign expert"
        in DATA["questions"]["activity"]["labels_en"]["crew_arriving_on_operating_vessel"]
    )
