"""Prove the proposed sponsor delta without changing any signed or canonical pack."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from backend.scripts.visa_engine import sponsor_requirements_fold
from backend.scripts.visa_engine.compile_pack import wrap_as_unsigned_pack
from backend.scripts.visa_engine.fold_pack_seq28 import _rule_pack_id
from backend.scripts.visa_engine.gold_replay_driver import (
    PACKS_DIR,
    _offline_identity_provider,
    _repository_trust_store,
)
from backend.scripts.visa_engine.sponsor_requirements_fold import apply_sponsor_requirements
from backend.services.visa_engine import compiler, evaluate_path, evaluator
from backend.services.visa_engine.api_models import VisaOracleEvaluateRequest
from backend.services.visa_engine.bundle import verify_rule_pack
from backend.services.visa_engine.models import RulePackPayload
from backend.tests.services.visa_engine.gold_replay import _decision_actual


@pytest.fixture(scope="module")
def anchor():
    signed = json.loads((PACKS_DIR / "rulepack-prod-032.signed.json").read_text())
    return verify_rule_pack(
        signed,
        trust_store=_repository_trust_store(),
        observed_at=datetime.fromisoformat(signed["protected"]["signed_at"]),
    )


@pytest.fixture(scope="module", autouse=True)
def isolated_pack_directory(tmp_path_factory):
    # Future production signatures must not change a pinned regression test.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            sponsor_requirements_fold, "_PACKS", tmp_path_factory.mktemp("signed-inventory")
        )
        yield


@pytest.fixture(scope="module")
def successor(anchor):
    candidate = anchor.pack.payload.model_dump(mode="json", by_alias=True, exclude_unset=True)
    # A throwaway successor identity, built only in RAM. No production candidate is written.
    candidate.update(
        sequence=33,
        previous_payload_sha256=anchor.payload_sha256.hex(),
        rule_pack_id=str(_rule_pack_id(33)),
        created_at="2026-11-03T08:00:00Z",
    )
    return candidate


def test_delta_adds_only_two_per_product_filters_without_mutating_input(successor, anchor):
    original = copy.deepcopy(successor)
    out = apply_sponsor_requirements(successor, anchor=anchor)
    assert successor == original
    assert out["rules"][:-2] == original["rules"]
    assert {k: v for k, v in out.items() if k != "rules"} == {
        k: v for k, v in original.items() if k != "rules"
    }
    products = {p["product_code"]: p["product_version_id"] for p in original["products"]}
    assert [rule["product_version_ids"] for rule in out["rules"][-2:]] == [
        [products["E31A"]],
        [products["E28A"]],
    ]
    assert all(
        rule["stage"] == "HARD_FILTER" and rule["on_unknown"] == "NEEDS_INPUT"
        for rule in out["rules"][-2:]
    )
    with pytest.raises(ValueError):
        apply_sponsor_requirements(out, anchor=anchor)


def test_reject_issued_or_unchained_identity(successor, anchor):
    for changes in (
        {"sequence": 32},
        {"rule_pack_id": str(anchor.pack.payload.rule_pack_id)},
        {"previous_payload_sha256": "0" * 64},
        {"rollback_of_payload_sha256": "0" * 64},
    ):
        with pytest.raises(ValueError):
            apply_sponsor_requirements({**successor, **changes}, anchor=anchor)
    with pytest.raises(ValueError):
        apply_sponsor_requirements(
            {"protected": {}, "payload": successor, "signature": "test"}, anchor=anchor
        )


def test_reject_already_issued_successor_and_missing_authority(
    successor, anchor, tmp_path, monkeypatch
):
    monkeypatch.setattr(sponsor_requirements_fold, "_PACKS", tmp_path)
    (tmp_path / "rulepack-prod-033.signed.json").touch()
    with pytest.raises(ValueError, match="unissued"):
        apply_sponsor_requirements(successor, anchor=anchor)
    (tmp_path / "rulepack-prod-033.signed.json").unlink()
    bad = copy.deepcopy(successor)
    for source in bad["source_records"]:
        source["locators"] = []
    with pytest.raises(ValueError, match="E28A lacks a verified source"):
        apply_sponsor_requirements(bad, anchor=anchor)


@pytest.fixture(scope="module")
def compiled(successor, anchor):
    raw = apply_sponsor_requirements(successor, anchor=anchor)
    unsigned = wrap_as_unsigned_pack(RulePackPayload.model_validate(raw))
    report = compiler.compile_rule_pack(unsigned)
    assert report.ok, [f"{error.code}: {error.message}" for error in report.errors]
    return compiler.build_compiled_pack(unsigned)


def known(value):
    return {"status": "KNOWN", "value": value}


UNVERIFIED = {"status": "UNKNOWN", "reason": "UNVERIFIED"}
AS_OF = datetime.fromisoformat("2026-11-03T08:01:00+00:00")
WALKS = Path(__file__).parent / "gold_coverage/fixtures/walks"
COMPANY = "investment.sponsoring_company_confirmed"
FAMILY = "family.sponsor_confirmed"
SPOUSE = "family.spouse_application_support_confirmed"


def _decision(compiled, walk: str, updates):
    wire = json.loads((WALKS / f"{walk}.json").read_text())
    wire["overrides"].update(updates)
    if wire["overrides"].get(SPOUSE) is None:
        wire["overrides"].pop(SPOUSE, None)
    request = VisaOracleEvaluateRequest.model_validate(
        {
            "schema_version": "1.0.0",
            "assessment_id": "00000000-0000-4000-8000-000000000001",
            "collected_at": AS_OF,
            "facts": wire["overrides"],
            "disclosed_review_flags": wire.get("disclosed_review_flags", []),
        }
    )
    facts = request.applicant_facts()
    decision = evaluator.evaluate(
        facts,
        compiled,
        effective_at=AS_OF,
        observed_at=AS_OF,
        identity_provider=_offline_identity_provider,
    )
    return _decision_actual(
        evaluate_path.apply_public_policy_adapters(
            decision, facts, compiled, disclosed_review_flags=request.effective_review_flags()
        )
    )


def _investor(compiled, **overrides):
    return _decision(
        compiled,
        "offshore_invest_pt_pma",
        {
            "intent.stay_days": known(400),
            "investment.pt_pma_committed": known(True),
            "investment.paid_up_capital_idr": known(3000000000),
            "investment.establishes_indonesian_company": known(False),
            "investment.foreign_branch_or_subsidiary": known(False),
            "investment.ikn_subsidiary": known(False),
            "investment.capital_market_only": known(False),
            "sponsor.type": known("INVESTMENT"),
            **overrides,
        },
    )


def test_e31a_requires_the_spouse_letter_answer_not_a_generic_family_answer(compiled):
    def family(value, **extra):
        return _decision(
            compiled,
            "offshore_family_SPOUSE_spNat_ID",
            {FAMILY: known(False), SPOUSE: value, "intent.stay_days": known(400), **extra},
        )

    assert family(known(True))["candidates"] == ["E31A"]
    denied = family(
        known(False),
        **{FAMILY: known(True), "process.wants_onshore_conversion": known(True)},
    )
    assert "E31A" not in denied["candidates"]
    assert "E31A_SPOUSE_APPLICATION_SUPPORT_DECLINED" in denied["no_path_reason_codes"]
    # A legacy "no", or even a generic "yes", says nothing about the spouse's letter.
    for legacy in (known(True), known(False)):
        for value in [None, UNVERIFIED, {"status": "UNKNOWN", "reason": "NOT_ASKED"}]:
            pending = family(value, **{FAMILY: legacy})
            assert pending["state"] == "NEEDS_INPUT"
            assert pending["missing_facts"] == [SPOUSE]
    assert "C1" in family(known(False), **{"intent.stay_days": known(120)})["candidates"]


def test_company_confirmation_is_distinct_from_family_support_and_category(compiled):
    accepted = _investor(compiled, **{COMPANY: known(True), FAMILY: known(False)})
    assert accepted["candidates"] == ["E28A"]
    assert _investor(compiled, **{COMPANY: known(True), "sponsor.type": UNVERIFIED})[
        "candidates"
    ] == ["E28A"]
    denied = _investor(compiled, **{COMPANY: known(False), FAMILY: known(True)})
    assert "E28A" not in denied["candidates"]
    assert "E28A_SPONSORING_COMPANY_DECLINED" in denied["no_path_reason_codes"]
    for value in [known(True), {"status": "UNKNOWN", "reason": "CONFLICTING"}, UNVERIFIED]:
        pending = _investor(compiled, **{COMPANY: value, "sponsor.type": known("NONE")})
        assert pending["state"] == "NEEDS_INPUT"
        assert pending["missing_facts"] == [COMPANY]
        assert not pending["no_path_reason_codes"]
    for reason in ["NOT_ASKED", "UNVERIFIED"]:
        pending = _investor(compiled, **{COMPANY: {"status": "UNKNOWN", "reason": reason}})
        assert pending["state"] == "NEEDS_INPUT" and COMPANY in pending["missing_facts"]


@pytest.mark.parametrize(
    ("code", "route"),
    [
        ("E28B", {"investment.establishes_indonesian_company": True}),
        ("E28C", {"investment.capital_market_only": True}),
        ("E28D", {"investment.foreign_branch_or_subsidiary": True}),
        (
            "E28F",
            {
                "investment.establishes_indonesian_company": True,
                "investment.foreign_branch_or_subsidiary": True,
                "investment.ikn_subsidiary": True,
            },
        ),
    ],
)
def test_golden_visas_still_support_without_a_company_sponsor(compiled, code, route):
    outcome = _investor(
        compiled,
        **{
            "sponsor.type": known("NONE"),
            "investment.own_shareholding_idr": known(5000000000),
            "investment.meets_published_threshold": known(True),
            **{key: known(value) for key, value in route.items()},
        },
    )
    assert code in outcome["candidates"] and COMPANY not in outcome["missing_facts"]


def test_no_collateral_filter_on_retirement(compiled):
    for walk, code in [
        ("offshore_retirement_bank_deposit_age64_deposit_passing", "E33E"),
        ("offshore_retirement_property_age64_sponsor_no", "E33F"),
    ]:
        result = _decision(compiled, walk, {})
        assert code in result["candidates"]
        assert COMPANY not in result["missing_facts"]


def test_an_already_ineligible_route_does_not_blame_an_unrelated_sponsor(compiled, anchor):
    unchanged = compiler.build_compiled_pack(anchor.pack)
    insufficient_capital = {"investment.paid_up_capital_idr": known(1000000000)}
    before = _investor(unchanged, **insufficient_capital)
    assert before["state"] == "NO_SUPPORTED_PATH"
    assert "E28A_PAID_CAPITAL_BELOW_MIN" in before["no_path_reason_codes"]
    assert _investor(compiled, **insufficient_capital) == before
    unmarried = {
        "family.marriage_registered": known(False),
        FAMILY: known(False),
        "intent.stay_days": known(400),
    }
    old = _decision(unchanged, "offshore_family_SPOUSE_spNat_ID", unmarried)
    assert old["state"] == "NO_SUPPORTED_PATH"
    assert _decision(compiled, "offshore_family_SPOUSE_spNat_ID", unmarried) == old
