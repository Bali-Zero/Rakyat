"""E33: seven distinct Law-derived leaves, conditions and exact stay boundaries."""

import json

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

DATA = guide.load_family("E33")
LAW_DIR = guide.HERE / "law" / "E33"
# Written from the official cards, independently of the Guide configuration.
PATHS = {
    "second_home": ("E33", ["second_home_commitment", "stay_months"]),
    "government_invited_expert": ("E33A", ["central_government_sponsor", "stay_months"]),
    "government_invited_collaboration": ("E33B", ["collaboration_commitment", "stay_months"]),
    "government_invited_world_figure": ("E33C", ["central_government_sponsor", "stay_months"]),
    "senior_five_years": ("E33E", ["age_55", "senior_deposit_commitment", "monthly_income"]),
    "senior_one_year": ("E33F", ["age_55", "monthly_income", "sponsor", "stay_months"]),
    "remote_foreign_company": ("E33G", ["annual_income", "stay_months"]),
}
YEARS = {
    "E33": [5],
    "E33A": [5, 10],
    "E33B": [5, 10],
    "E33C": [5, 10],
    "E33E": [5],
    "E33F": [1],
    "E33G": [1],
}
GOOD = {
    "second_home_commitment": "own_name_state_bank_130000",
    "collaboration_commitment": "yes",
    "central_government_sponsor": "central_government",
    "age_55": "yes",
    "senior_deposit_commitment": "yes",
    "monthly_income": "yes",
    "annual_income": "yes",
    "sponsor": "yes",
    "stay_months": 12,
}


def law(code: str) -> dict:
    return json.loads((LAW_DIR / f"{code}.json").read_text())


def answers(route: str, **extra: object) -> dict:
    return {"activity": route, **GOOD, **extra}


def test_only_seven_source_verified_leaves() -> None:
    assert DATA["questions"]["activity"]["options"] == {a: c for a, (c, _) in PATHS.items()}
    assert set(DATA["visas"]) == set(YEARS) | {"E33D"}
    assert law("E33D")["status"] == "NOT_FOUND"
    assert law("E33D")["unavailable_verbatim"] == "Data Belum Tersedia"
    assert DATA["visas"]["E33D"]["stay_options"] is None


@pytest.mark.parametrize("activity,expected", PATHS.items())
def test_every_leaf_in_order_with_at_most_four_questions(activity: str, expected: tuple) -> None:
    code, questions = expected
    assert resolve("E33", {}) == {"ask": "activity"}
    a = {"activity": activity}
    for question in questions:
        assert resolve("E33", a) == {"ask": question, "code": code}
        a[question] = GOOD[question]
    result = resolve("E33", a)
    assert result == {
        "code": code,
        "verdict": OK,
        "stay_option": {"value": YEARS[code][0], "unit": "YEAR"},
    }
    # E33F gained the owner-ruled age question on 2026-10-19 (five questions).
    assert len(questions) + 1 <= (5 if code == "E33F" else 4)


@pytest.mark.parametrize("activity,expected", PATHS.items())
def test_all_initial_and_extension_months_and_first_excess(activity: str, expected: tuple) -> None:
    code, _ = expected
    total = 72 if code in {"E33F", "E33G"} else 120
    for months in range(1, total + 1):
        result = resolve("E33", answers(activity, stay_months=months))
        if months <= YEARS[code][-1] * 12:
            year = next(y for y in YEARS[code] if months <= y * 12)
            assert result == {
                "code": code,
                "verdict": OK,
                "stay_option": {"value": year, "unit": "YEAR"},
            }
        else:
            assert result == {
                "code": code,
                "verdict": OK_WITH_EXTENSION,
                "stay_option": {"value": YEARS[code][-1], "unit": "YEAR"},
                "total_max_known": True,
            }
    for months in (total + 1, 1200):
        assert resolve("E33", answers(activity, stay_months=months)) == {
            "code": code,
            "verdict": STAY_TOO_LONG,
        }


@pytest.mark.parametrize(
    "activity,gate",
    [
        ("government_invited_collaboration", "collaboration_commitment"),
        ("senior_five_years", "age_55"),
        ("senior_five_years", "senior_deposit_commitment"),
        ("senior_five_years", "monthly_income"),
        ("senior_one_year", "monthly_income"),
        ("remote_foreign_company", "annual_income"),
    ],
)
def test_failed_condition_wins_over_later_answers(activity: str, gate: str) -> None:
    code = PATHS[activity][0]
    assert resolve("E33", answers(activity, **{gate: "no"})) == {
        "code": code,
        "verdict": NOT_COVERED,
    }


def test_second_home_has_alternative_commitments_without_sponsor() -> None:
    for option in ("own_name_state_bank_130000", "apartment_1000000"):
        assert (
            resolve(
                "E33",
                {"activity": "second_home", "second_home_commitment": option, "stay_months": 60},
            )["verdict"]
            == OK
        )
    assert resolve("E33", answers("second_home", second_home_commitment="neither")) == {
        "code": "E33",
        "verdict": NOT_COVERED,
    }


@pytest.mark.parametrize(
    "activity", ["government_invited_expert", "government_invited_world_figure"]
)
@pytest.mark.parametrize("sponsor", ["local_government", "company", "individual", "none"])
def test_central_government_sponsor_cannot_be_substituted(activity: str, sponsor: str) -> None:
    assert resolve("E33", answers(activity, central_government_sponsor=sponsor)) == {
        "code": PATHS[activity][0],
        "verdict": NEEDS_SPONSOR,
    }


def test_e33f_needs_sponsor_and_owner_ruled_age_55_but_no_invented_sponsor_type() -> None:
    a = {
        "activity": "senior_one_year",
        "age_55": "yes",
        "monthly_income": "yes",
        "sponsor": "no",
        "stay_months": 12,
    }
    assert resolve("E33", a) == {"code": "E33F", "verdict": NEEDS_SPONSOR}
    a["sponsor"] = "yes"
    assert resolve("E33", a)["verdict"] == OK
    # Owner ruling 2026-10-19: senior threshold 55 also gates E33F.
    assert resolve("E33", {**a, "age_55": "no"}) == {"code": "E33F", "verdict": NOT_COVERED}
    assert resolve("E33", {k: v for k, v in a.items() if k != "age_55"}) == {
        "ask": "age_55",
        "code": "E33F",
    }
    assert "minimum_age_not_stated" not in law("E33F")["to_verify"]
    assert "sponsor_type_not_stated" in law("E33F")["to_verify"]
    assert DATA["visas"]["E33F"]["requires_questions"] == ["age_55", "monthly_income"]


@pytest.mark.parametrize(
    "activity",
    [
        "second_home",
        "government_invited_collaboration",
        "senior_five_years",
        "remote_foreign_company",
    ],
)
def test_no_sponsor_or_travel_document_question_on_sponsor_free_routes(activity: str) -> None:
    a = answers(activity)
    del a["sponsor"], a["central_government_sponsor"]
    assert resolve("E33", a)["verdict"] == OK


@pytest.mark.parametrize("value", [None, "", "maybe", True, False, 1, 0])
@pytest.mark.parametrize("activity,expected", PATHS.items())
def test_invalid_condition_answers_ask_the_same_question(
    activity: str, expected: tuple, value: object
) -> None:
    code, questions = expected
    for question in ["activity", *questions]:
        if question == "stay_months":
            continue
        expected_result = {"ask": question}
        if question != "activity":
            expected_result["code"] = code
        assert resolve("E33", answers(activity, **{question: value})) == expected_result


@pytest.mark.parametrize("value", [None, "", 0, "0", -1, True, False, 1.5, "1.5", "１２", " 12 "])
@pytest.mark.parametrize("activity", PATHS)
def test_invalid_months_are_never_approved(activity: str, value: object) -> None:
    assert resolve("E33", answers(activity, stay_months=value)) == {
        "ask": "stay_months",
        "code": PATHS[activity][0],
    }


def test_single_e33e_choice_and_explicit_duration_override() -> None:
    a = answers("senior_five_years")
    del a["stay_months"]
    assert resolve("E33", a) == {
        "code": "E33E",
        "verdict": OK,
        "stay_option": {"value": 5, "unit": "YEAR"},
    }
    assert resolve("E33", {**a, "stay_months": "61"})["verdict"] == OK_WITH_EXTENSION
    assert resolve("E33", {**a, "stay_months": "121"})["verdict"] == STAY_TOO_LONG


@pytest.mark.parametrize("code", YEARS)
def test_typed_data_matches_code_page_and_separate_total_evidence(code: str) -> None:
    card, visa = law(code), DATA["visas"][code]
    assert card["status"] == "VERIFIED_FROM_SOURCE"
    assert visa["stay_options"] == [{"value": y, "unit": "YEAR"} for y in YEARS[code]]
    assert visa["sponsor_required"] is card["sponsor_required"]["as_stated_boolean"]
    assert visa["sponsor"] == ("REQUIRED" if visa["sponsor_required"] else "NOT_REQUIRED")
    assert visa["entry_type"] == card["entry"].upper() == "MULTIPLE"
    assert visa["extendable"] is ("dapat diperpanjang" in " ".join(card["extendable"]))
    if code in {"E33F", "E33G"}:
        assert card["total_max_stay_verbatim"] is None
        evidence = card["supplementary_stay_evidence"]
        assert f"({code})" in evidence["code_name_verbatim"]
        assert "tidak lebih dari 6 tahun" in evidence["extension_verbatim"]
        assert visa["max_total_months"] == 72
    else:
        assert "10 (sepuluh) tahun" in " ".join(card["total_max_stay_verbatim"])
        assert visa["max_total_months"] == 120


def test_thresholds_and_checklist_alternatives_stay_faithful() -> None:
    assert "berusia 55 tahun atau lebih" in law("E33E")["definition_verbatim"]
    assert "at least 55" in DATA["questions"]["age_55"]["text_en"]
    for code in ("E33E", "E33F"):
        text = " ".join(r["text"] for r in law(code)["requirements_verbatim"])
        assert "penghasilan atau tunjangan" in text and "US$3.000" in text
    assert (
        "income or an allowance of at least USD 3,000 per month"
        in DATA["questions"]["monthly_income"]["text_en"]
    )
    assert "US$50.000" in law("E33E")["requirements_verbatim"][7]["text"]
    assert "USD 50,000" in DATA["questions"]["senior_deposit_commitment"]["text_en"]
    assert "US$60.000 per tahun" in law("E33G")["requirements_verbatim"][5]["text"]
    assert "USD 60,000 per year" in DATA["questions"]["annual_income"]["text_en"]
    text = " ".join(r["text"] for r in law("E33B")["requirements_verbatim"])
    assert "sertifikat" in text and "atau" in text
    assert "3 (tiga) tahun terakhir" in text and "3,5 (tiga koma lima)" in text
    assert "Perjanjian kerja dengan perusahaan" in law("E33G")["requirements_verbatim"][6]["text"]
    assert (
        "employment agreement"
        in DATA["questions"]["activity"]["labels_en"]["remote_foreign_company"]
    )


# Owner ruling 2026-10-18 "segui la legge": Permenkumham 22/2023 Pasal 173 huruf f (+ 11/2024).
ITAP_BY_LAW = {
    "E33": (True, 1),
    "E33A": (True, 2),
    "E33B": (True, 2),
    "E33C": (True, 3),
    "E33D": (None, None),
    "E33E": (True, 4),
    "E33F": (True, 4),
    "E33G": (False, None),
}


def test_itap_stage_follows_the_law_per_code() -> None:
    evidence = json.loads((LAW_DIR / "ITAP.json").read_text())
    assert evidence["explicitly_convertible_codes"] == [
        "E33",
        "E33A",
        "E33B",
        "E33C",
        "E33E",
        "E33F",
    ]
    assert (
        {a["code"] for a in evidence["code_assessments"]} == set(DATA["visas"]) == set(ITAP_BY_LAW)
    )
    for assessment in evidence["code_assessments"]:
        code = assessment["code"]
        eligible, angka = ITAP_BY_LAW[code]
        card = law(code)
        assert assessment["as_stated_boolean"] is eligible
        assert card["itap_eligibility_as_stated_boolean"] is eligible
        assert assessment["status"] == card["itap_status"]
        expected = (
            "TO_VERIFY"
            if eligible is None
            else "LAW_MAPPED_BY_TITLE"
            if code == "E33F"
            else "LAW_MAPPED_BY_DEFINITION"
        )
        assert card["itap_status"] == expected
        assert (
            "itap_eligibility_as_stated_boolean" in card["to_verify"]
        ) is False or eligible is None
        if eligible:
            assert f"Pasal 173 huruf f angka {angka}" in assessment["reason"]
            sections = [e["section"] for e in card["itap_verbatim"]]
            assert any(f"Pasal 173 huruf f angka {angka}" in s for s in sections)
            assert any("Pasal 178" in s and "Pasal 179" in s for s in sections)
    # E33G verbatim entries are real quotes (Indonesian), commentary lives in notes.
    for entry in law("E33G")["itap_verbatim"]:
        assert not entry["text"].startswith("Pasal 173 huruf f lists only")
    assert any(
        "remote worker" in e["text"] and "1 (satu) tahun" in e["text"]
        for e in law("E33G")["itap_verbatim"]
    )
    assert any("Commentary on itap_verbatim" in n for n in law("E33G")["notes"])
    assert law("E33D")["itap_verbatim"] is None
    assert all("itap" not in q.lower() for q in DATA["questions"])


def test_no_card_or_doc_still_claims_e33_has_no_kitap() -> None:
    for path in LAW_DIR.glob("*"):
        if path.suffix in {".json", ".md"}:
            assert "base E33 has no KITAP stage" not in path.read_text(), path.name
            assert "owner_instruction_vs_generic_itap_text" not in path.read_text(), path.name


def test_senior_age_conflict_is_history_resolved_by_owner_ruling() -> None:
    for code in ("E33E", "E33F"):
        card = law(code)
        assert any("55-vs-60 conflict" in n for n in card["notes"])
        assert "senior_age_threshold_55_vs_60" not in card["to_verify"]
        (ruling,) = card["owner_rulings"]
        assert ruling["date"] == "2026-10-19"
        assert ruling["topic"] == "senior_age_threshold_55_vs_60"
        assert (
            ruling["ruling"] == "55 per owner ruling + Pasal 173 f angka 4 (Permenkumham 11/2024)."
        )
        assert "Pasal 105 ayat (10) huruf d" in ruling["history"]
        assert {
            "index": len(card["to_verify"]) + 1 if code == "E33F" else 2,
            "item": "senior_age_threshold_55_vs_60",
        } in ruling["resolves_to_verify"]
        assert "age_55" in DATA["visas"][code]["requires_questions"]
        assert DATA["questions"]["age_55"]["text_en"] == "Are you at least 55 years old?"
    evidence = json.loads((LAW_DIR / "ITAP.json").read_text())
    assert any("55-vs-60 conflict" in n for n in evidence["notes"])
    guide = (LAW_DIR / "GUIDE.md").read_text()
    assert "Pasal 105 ayat (10) huruf d" in guide
    assert "OWNER RULING, 2026-10-19" in guide and "senior age threshold 55" in guide
