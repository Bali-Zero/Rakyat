"""Family guide: answers -> visa code -> verdict. Data comes from family_<x>.json, built from law/<X>/*.json."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

HERE = Path(__file__).parent

OK = "OK"
OK_WITH_EXTENSION = "OK_WITH_EXTENSION"
NEEDS_SPONSOR = "NEEDS_SPONSOR"
STAY_TOO_LONG = "STAY_TOO_LONG"
NOT_COVERED = "NOT_COVERED"  # the activity is outside what this visa allows (e.g. commercial sport on C8A/C8B)
TO_VERIFY = "TO_VERIFY"  # a required legal assessment remains unknown

#: ISO 3166-1 alpha-2, all 249 officially assigned codes. A nationality answer outside this set
#: is unknown and is asked again; a known code outside a card's list is simply not listed.
ISO_3166_ALPHA2 = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ
    BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM
    DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS
    GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN
    KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ
    MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM
    PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV
    SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI
    VN VU WF WS YE YT ZA ZM ZW
    """.split()
)


@cache
def load_family(family: str) -> dict:
    return json.loads((HERE / f"family_{family.lower()}.json").read_text())


@cache
def eligible_nationalities(law_dir: str, code: str) -> frozenset[str]:
    """ISO alpha-2 codes listed in the card's sourced ``eligible_nationalities`` field."""
    card = json.loads((HERE / law_dir / f"{code}.json").read_text())
    return frozenset(entry["iso2"] for entry in card["eligible_nationalities"]["entries"])


def _nationality_gate(data: dict, code: str, gate: str, answers: dict) -> dict | None:
    """A ``kind: nationality`` question decides from the card's list. ``None`` means the gate passes.

    An answer that is not an ISO 3166-1 alpha-2 code is asked again. A listed code passes. A known,
    unlisted code fails, unless the question names an ``otherwise_ask`` yes/no route that applies
    regardless of nationality (A1: Singapore permit holders, Perpres 95/2024 Lampiran B); a yes there
    returns TO_VERIFY, because that route's current scope is unsettled."""
    questions = data["questions"]
    value = answers.get(gate)
    iso2 = value.strip().upper() if isinstance(value, str) else None
    if iso2 not in ISO_3166_ALPHA2:
        return {"ask": gate, "code": code}
    if iso2 in eligible_nationalities(data["law_dir"], code):
        return None
    route = questions[gate].get("otherwise_ask")
    if route:
        if answers.get(route) not in questions[route]["options"]:
            return {"ask": route, "code": code}
        if questions[route]["options"][answers[route]]:
            # The route's current scope is unsettled: never a plain OK.
            return {"code": code, "verdict": TO_VERIFY}
    return {"code": code, "verdict": NOT_COVERED}


def max_total_days(visa: dict) -> int:
    if "max_total_days" in visa:
        return visa["max_total_days"]
    return visa["max_total_months"] * 30


def _positive_int(value: object) -> int | None:
    """A positive integer, from an int or a string of digits; anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value) or None
    return None


def _tier_gated(
    visa: dict, code: str, option: dict, answers: dict, questions: dict, result: dict
) -> dict:
    """Optional per-stay-tier gate: `requires_question_by_stay` maps the selected option's value to a
    yes/no question about THAT tier. Absent -> the result is returned unchanged."""
    gate = (visa.get("requires_question_by_stay") or {}).get(str(option["value"]))
    if gate:
        if answers.get(gate) not in questions[gate]["options"]:
            return {"ask": gate, "code": code}
        if not questions[gate]["options"][answers[gate]]:
            return {"code": code, "verdict": NOT_COVERED, "stay_option": option}
    return {"code": code, **result, "stay_option": option}


def resolve(family: str, answers: dict) -> dict:
    """Return {"ask": question_id} while an answer is missing, else {"code", "verdict"}."""
    data = load_family(family)
    questions = data["questions"]
    start = data.get("start_question", "activity")
    choice = questions[start]["options"].get(answers.get(start))
    if choice is None:
        return {"ask": start}
    while isinstance(choice, dict):
        sub = choice["ask"]
        if sub not in answers:
            return {"ask": sub}
        choice = questions[sub]["options"].get(answers[sub])
        if choice is None:
            return {"ask": sub}
    code = choice
    visa = data["visas"][code]
    gates = visa.get("requires_questions", [])
    if visa.get("requires_question"):
        gates = [visa["requires_question"], *gates]
    for gate in gates:
        if questions[gate].get("kind") == "nationality":
            outcome = _nationality_gate(data, code, gate, answers)
            if outcome is not None:
                return outcome
            continue
        if answers.get(gate) not in questions[gate]["options"]:
            return {"ask": gate, "code": code}
        if not questions[gate]["options"][answers[gate]]:
            return {"code": code, "verdict": NOT_COVERED}
    if visa.get("sponsor") == "NOT_REQUIRED" or data.get("agency_sponsors"):
        # agency_sponsors: owner ruling (family D, 2026-10-19) - Bali Zero is an agency and can
        # always act as sponsor, so no sponsor question is asked and NEEDS_SPONSOR is never returned.
        needs_sponsor_question = False
    elif visa["sponsor_required"] is None:
        # An unstated sponsor rule is neither an exemption nor C's conditional rule.
        return {"code": code, "verdict": TO_VERIFY}
    elif visa["sponsor_required"]:
        needs_sponsor_question = True
    else:
        # Sponsor is conditional (stateless / non-national travel document / listed countries).
        if answers.get("travel_document") not in questions["travel_document"]["options"]:
            return {"ask": "travel_document", "code": code}
        needs_sponsor_question = questions["travel_document"]["options"][answers["travel_document"]]
    if needs_sponsor_question:
        sponsor = visa.get("sponsor_question", "sponsor")
        if answers.get(sponsor) not in questions[sponsor]["options"]:
            return {"ask": sponsor, "code": code}
        if not questions[sponsor]["options"][answers[sponsor]]:
            return {"code": code, "verdict": NEEDS_SPONSOR}
    if visa.get("stay_options"):
        # Opt-in exact initial choice for mixed DAY/YEAR options; never convert units.
        if stay_question := visa.get("stay_question"):
            selected = answers.get(stay_question)
            option = (
                questions[stay_question]["options"].get(selected)
                if isinstance(selected, str)
                else None
            )
            if option not in visa["stay_options"]:
                return {"ask": stay_question, "code": code}
            return _tier_gated(visa, code, option, answers, questions, {"verdict": OK})
        if all(option["unit"] == "DAY" for option in visa["stay_options"]):
            days = _positive_int(answers.get("stay_days"))
            if days is None:
                return {"ask": "stay_days", "code": code}
            total_days = visa.get("max_total_days")
            if total_days is not None and days > total_days:
                return {"code": code, "verdict": STAY_TOO_LONG}
            options = sorted(visa["stay_options"], key=lambda option: option["value"])
            for option in options:
                if days <= option["value"]:
                    return {"code": code, "verdict": OK, "stay_option": option}
            if visa["extendable"]:
                return {
                    "code": code,
                    "verdict": OK_WITH_EXTENSION,
                    "stay_option": options[-1],
                    "total_max_known": total_days is not None,
                }
            return {"code": code, "verdict": STAY_TOO_LONG}
        # One published choice needs no duration question when the family opts in.
        if visa.get("auto_select_stay") and "stay_months" not in answers:
            (option,) = visa["stay_options"]
            return {"code": code, "verdict": OK, "stay_option": option}
        months = _positive_int(answers.get("stay_months"))
        if months is None:
            return {"ask": "stay_months", "code": code}
        total_months = visa.get("max_total_months")
        if total_months is not None and months > total_months:
            return {"code": code, "verdict": STAY_TOO_LONG}
        # Compare only exact calendar-month units, never approximate a day option.
        options = sorted(
            (o for o in visa["stay_options"] if o["unit"] in {"MONTH", "YEAR"}),
            key=lambda o: o["value"] * (12 if o["unit"] == "YEAR" else 1),
        )
        for option in options:
            if months <= option["value"] * (12 if option["unit"] == "YEAR" else 1):
                return _tier_gated(visa, code, option, answers, questions, {"verdict": OK})
        if visa["extendable"]:
            # An absent cumulative maximum stays explicitly unknown.
            return _tier_gated(
                visa,
                code,
                options[-1],
                answers,
                questions,
                {"verdict": OK_WITH_EXTENSION, "total_max_known": total_months is not None},
            )
        return {"code": code, "verdict": STAY_TOO_LONG}
    days = _positive_int(answers.get("stay_days"))
    if days is None:
        return {"ask": "stay_days", "code": code}
    if days <= visa["stay_days"]:
        return {"code": code, "verdict": OK}
    if visa["extendable"] and days <= max_total_days(visa):
        return {"code": code, "verdict": OK_WITH_EXTENSION}
    return {"code": code, "verdict": STAY_TOO_LONG}
