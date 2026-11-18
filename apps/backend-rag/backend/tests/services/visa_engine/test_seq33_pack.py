"""Tie the public seq-33 signature to the frozen, reviewed source and seq-32 parent."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from backend.scripts.visa_engine.gold_replay_driver import (
    PACKS_DIR,
    _offline_identity_provider,
    _repository_trust_store,
    build_persona_request,
)
from backend.scripts.visa_engine.reachability_report import build_report
from backend.services.visa_engine import compiler, evaluator
from backend.services.visa_engine.bundle import canonicalize_json, verify_rule_pack
from backend.services.visa_engine.compiler import DEFAULT_FACT_REGISTRY, compile_rule_pack
from backend.services.visa_engine.errors import RulePackVerificationError
from backend.services.visa_engine.models import DecisionState
from backend.tests.services.visa_engine.test_evaluator_gold import Persona

WALKS_DIR = Path(__file__).resolve().parent / "gold_coverage/fixtures/walks"
SOURCE = PACKS_DIR / "rulepack-prod-033.source.json"
SIGNED = PACKS_DIR / "rulepack-prod-033.signed.json"
SOURCE_SHA256 = "2730e3bf9194b953e4f7c27a6a2234dfe67e8ea35900a2e9e5d8896ce543b182"
PAYLOAD_SHA256 = "24f2bb305dfa3007760323051f33e70d69ae2cbe07dc95550169822c9def8a74"


@pytest.fixture(scope="module")
def signed():
    return json.loads(SIGNED.read_text(encoding="utf-8"))


def test_source_bytes_and_jcs_are_the_frozen_candidate():
    raw = SOURCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_SHA256
    assert hashlib.sha256(canonicalize_json(json.loads(raw))).hexdigest() == PAYLOAD_SHA256


def test_signed_payload_is_exactly_the_reviewed_source(signed):
    assert canonicalize_json(signed["payload"]) == canonicalize_json(
        json.loads(SOURCE.read_bytes())
    )
    assert signed["payload_sha256"] == PAYLOAD_SHA256


def test_signature_verifies_and_compiles_under_the_repository_public_key(signed):
    verified = verify_rule_pack(
        signed,
        trust_store=_repository_trust_store(),
        observed_at=datetime.fromisoformat(signed["protected"]["signed_at"]),
    )
    assert verified.pack.protected.kid == "prod-2026-07-1"
    assert verified.pack.payload.sequence == 33
    assert verified.pack.payload.environment.value == "PRODUCTION"
    assert verified.payload_sha256.hex() == PAYLOAD_SHA256
    assert len(verified.pack.payload.rules) == 125
    assert len(verified.pack.payload.products) == 38
    assert len(verified.pack.payload.source_records) == 28
    report = compile_rule_pack(verified.pack)
    assert report.ok, [error.code for error in report.errors]


def test_parent_is_the_actual_signed_seq32(signed):
    parent = json.loads((PACKS_DIR / "rulepack-prod-032.signed.json").read_bytes())
    verified = verify_rule_pack(
        parent,
        trust_store=_repository_trust_store(),
        observed_at=datetime.fromisoformat(parent["protected"]["signed_at"]),
    )
    assert verified.pack.payload.sequence == 32
    assert signed["payload"]["previous_payload_sha256"] == verified.payload_sha256.hex()


def test_tampering_with_the_signed_payload_is_rejected(signed):
    tampered = copy.deepcopy(signed)
    tampered["payload"]["created_by"] += "x"
    with pytest.raises(RulePackVerificationError):
        verify_rule_pack(
            tampered,
            trust_store=_repository_trust_store(),
            observed_at=datetime.fromisoformat(signed["protected"]["signed_at"]),
        )


def test_valid_shape_signature_tampering_is_rejected(signed):
    tampered = copy.deepcopy(signed)
    raw = bytearray(base64.urlsafe_b64decode(tampered["signature"] + "=="))
    raw[0] ^= 1
    tampered["signature"] = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(RulePackVerificationError):
        verify_rule_pack(
            tampered,
            trust_store=_repository_trust_store(),
            observed_at=datetime.fromisoformat(signed["protected"]["signed_at"]),
        )


def _compiled_seq33(signed_raw: dict[str, Any]):
    verified = verify_rule_pack(
        signed_raw,
        trust_store=_repository_trust_store(),
        observed_at=datetime.fromisoformat(signed_raw["protected"]["signed_at"]),
    )
    compiled = compiler.build_compiled_pack(verified.pack, fact_registry=DEFAULT_FACT_REGISTRY)
    as_of = datetime.fromisoformat(signed_raw["protected"]["signed_at"])
    return compiled, as_of


def _eval_overrides(compiled, as_of: datetime, overrides: dict[str, Any], label: str):
    persona = Persona(
        id=0,
        label=label,
        overrides=overrides,
        expected_state=DecisionState.NEEDS_INPUT,
    )
    request = build_persona_request(persona)
    return evaluator.evaluate(
        request.applicant_facts(),
        compiled,
        effective_at=as_of,
        observed_at=as_of,
        identity_provider=_offline_identity_provider,
    )


def test_reachability_report_unlocks_requested_product_code_on_seq33():
    report = build_report(SOURCE)
    assert "intent.requested_product_code" not in report.not_asked_facts
    assert "intent.requested_product_code" in report.used_fact_paths


def test_hf_visit_not_e28a_conversion_excludes_c2_and_d12(signed):
    compiled, as_of = _compiled_seq33(signed)

    walk_spec = json.loads((WALKS_DIR / "onshore_invest.json").read_text(encoding="utf-8"))
    with_e28a = _eval_overrides(compiled, as_of, walk_spec["overrides"], "seq33-onshore-invest")
    assert with_e28a.state == DecisionState.SUPPORTED_CANDIDATES
    with_e28a_codes = [c.product_code for c in with_e28a.candidates]
    assert "C2" not in with_e28a_codes
    assert "D12" not in with_e28a_codes

    # Prove C2 only disappeared because hf.visit.not-e28a-conversion fired:
    without_code_overrides = {
        **walk_spec["overrides"],
        "intent.requested_product_code": {"status": "UNKNOWN", "reason": "NOT_ASKED"},
    }
    without_code = _eval_overrides(compiled, as_of, without_code_overrides, "seq33-without-code")
    assert "C2" in [c.product_code for c in without_code.candidates]

    # When no E28 product qualifies either, hf.visit.not-e28a-conversion surfaces its own code.
    c2_only_overrides = {
        **walk_spec["overrides"],
        "investment.pt_pma_committed": {"status": "KNOWN", "value": False},
        "investment.establishes_indonesian_company": {"status": "KNOWN", "value": False},
        "investment.foreign_branch_or_subsidiary": {"status": "KNOWN", "value": False},
        "investment.capital_market_only": {"status": "KNOWN", "value": False},
        "investment.meets_published_threshold": {"status": "KNOWN", "value": False},
    }
    excluded = _eval_overrides(compiled, as_of, c2_only_overrides, "seq33-c2-excluded")
    assert excluded.state == DecisionState.NO_SUPPORTED_PATH
    assert any(
        r.code == "VISITOR_ENTRY_NOT_ONSHORE_INVESTOR_CONVERSION"
        and "hf.visit.not-e28a-conversion" in r.rule_ids
        for r in excluded.no_path_reasons
    )


def test_review_e28a_bridging_origin_requires_human_review(signed):
    compiled, as_of = _compiled_seq33(signed)
    walk_spec = json.loads((WALKS_DIR / "onshore_invest.json").read_text(encoding="utf-8"))
    overrides = {
        **walk_spec["overrides"],
        "process.application_channel": {"status": "KNOWN", "value": "STATUS_BRIDGING"},
        "immigration.current_status_code": {"status": "KNOWN", "value": "C1"},
    }
    decision = _eval_overrides(compiled, as_of, overrides, "seq33-review-bridging-origin")
    assert decision.state == DecisionState.HUMAN_REVIEW_REQUIRED
    assert any(
        r.code == "STATUS_BRIDGING_REVIEW" and "review.e28a.bridging-origin" in r.rule_ids
        for r in decision.review_reasons
    )


def test_el_bridging_four_rules_fire_with_an_explicit_destination(signed):
    compiled, as_of = _compiled_seq33(signed)
    walk_spec = json.loads((WALKS_DIR / "onshore_other.json").read_text(encoding="utf-8"))
    overrides = {
        **walk_spec["overrides"],
        "intent.purposes": {"status": "KNOWN", "value": ["OTHER"]},
        "immigration.current_status_code": {"status": "KNOWN", "value": "E23"},
        "immigration.current_status_expiry": {"status": "KNOWN", "value": "2026-12-31"},
        "process.application_channel": {"status": "KNOWN", "value": "STATUS_BRIDGING"},
        "intent.requested_product_code": {"status": "KNOWN", "value": "E28A"},
        "intent.stay_days": {"status": "KNOWN", "value": 60},
    }
    decision = _eval_overrides(compiled, as_of, overrides, "seq33-el-bridging-4-rules")
    assert decision.state == DecisionState.SUPPORTED_CANDIDATES
    bridging = next((c for c in decision.candidates if c.product_code == "BRIDGING"), None)
    assert bridging is not None
    assert {
        "el.bridging.destination-stated",
        "el.bridging.source-status-verify",
        "el.bridging.t3-window-manual",
        "el.bridging.overstay-shield-payment",
    } <= set(bridging.support_rule_ids)
