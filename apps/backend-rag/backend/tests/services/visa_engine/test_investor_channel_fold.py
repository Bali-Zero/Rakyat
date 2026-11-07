"""Verify route scope, legal alternatives and fixed gold expectations for seq33."""

import json
from datetime import datetime

import pytest

from backend.scripts.visa_engine.compile_pack import wrap_as_unsigned_pack
from backend.scripts.visa_engine.gold_replay_driver import (
    PACKS_DIR,
    PERSONAS,
    _offline_identity_provider,
    build_persona_request,
)
from backend.services.visa_engine import compiler, evaluate_path, evaluator
from backend.services.visa_engine.models import RulePackPayload
from backend.tests.services.visa_engine.gold_replay import _decision_actual

NOW = datetime.fromisoformat("2026-11-04T00:00:00+00:00")


@pytest.fixture(scope="module")
def packed():
    payload = json.loads((PACKS_DIR / "rulepack-prod-033.source.json").read_text())
    return compiler.build_compiled_pack(
        wrap_as_unsigned_pack(RulePackPayload.model_validate(payload))
    )


def decision(packed, persona, updates=None):
    request = build_persona_request(next(p for p in PERSONAS if p.id == persona))
    data = request.model_dump(mode="json", by_alias=True)
    data["facts"].update(updates or {})
    request = type(request).model_validate(data)
    facts = request.applicant_facts()
    d = evaluator.evaluate(
        facts,
        packed,
        effective_at=NOW,
        observed_at=NOW,
        identity_provider=_offline_identity_provider,
    )
    return _decision_actual(
        evaluate_path.apply_public_policy_adapters(
            d, facts, packed, disclosed_review_flags=request.effective_review_flags()
        )
    )


def known(v):
    return {"status": "KNOWN", "value": v}


def test_gold_conversion_clarifies_instead_of_offering_visitor_entry(packed):
    outcome = decision(packed, 9)
    assert outcome["state"] == "NEEDS_INPUT"
    assert outcome["missing_facts"] == ["sponsor.type"]
    assert not outcome["candidates"]
    # C1 is not a blanket bar: a qualified shareholder can obtain the requested route.
    qualified = decision(
        packed,
        9,
        {
            "investment.paid_up_capital_idr": known(3000000000),
            "investment.own_shareholding_idr": known(12000000000),
            "investment.sponsoring_company_confirmed": known(True),
            "sponsor.type": known("INVESTMENT"),
        },
    )
    assert "E28A" in qualified["candidates"]
    assert "C2" not in qualified["candidates"] and "D12" not in qualified["candidates"]


def test_bridging_gold_has_specific_review(packed):
    outcome = decision(packed, 10)
    assert outcome["state"] == "HUMAN_REVIEW_REQUIRED"
    assert outcome["review_reason_codes"] == ["STATUS_BRIDGING_REVIEW"]
    assert not outcome["candidates"]


def test_unknown_target_or_offshore_is_outside_the_conversion_filter(packed):
    # An ordinary offshore investor still has the existing entry alternatives.
    outcome = decision(packed, 9, {"immigration.currently_in_indonesia": known(False)})
    assert "C2" in outcome["candidates"] and "D12" in outcome["candidates"]


def test_unknown_target_keeps_entry_alternatives(packed):
    outcome = decision(
        packed, 9, {"intent.requested_product_code": {"status": "UNKNOWN", "reason": "NOT_ASKED"}}
    )
    assert "C2" in outcome["candidates"] and "D12" in outcome["candidates"]


def test_low_capital_does_not_become_an_investor_path_when_other_routes_are_refused(packed):
    outcome = decision(
        packed,
        9,
        {
            "sponsor.type": known("NONE"),
            "investment.establishes_indonesian_company": known(False),
            "investment.foreign_branch_or_subsidiary": known(False),
            "investment.capital_market_only": known(False),
            "investment.ikn_subsidiary": known(False),
        },
    )
    assert outcome["state"] == "NO_SUPPORTED_PATH"
    assert "E28A_PAID_CAPITAL_BELOW_MIN" in outcome["no_path_reason_codes"]
    assert "VISITOR_ENTRY_NOT_ONSHORE_INVESTOR_CONVERSION" in outcome["no_path_reason_codes"]


@pytest.mark.parametrize("origin", [known("E28A"), {"status": "UNKNOWN", "reason": "UNVERIFIED"}])
def test_bridging_review_does_not_fire_for_eligible_or_unverified_origins(packed, origin):
    outcome = decision(packed, 10, {"immigration.current_status_code": origin})
    assert "STATUS_BRIDGING_REVIEW" not in outcome["review_reason_codes"]


def test_direct_conversion_does_not_fire_the_bridging_review(packed):
    assert "STATUS_BRIDGING_REVIEW" not in decision(packed, 9)["review_reason_codes"]
