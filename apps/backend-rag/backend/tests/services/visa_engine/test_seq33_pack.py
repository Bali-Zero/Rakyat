"""Tie the public seq-33 signature to the frozen, reviewed source and seq-32 parent."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime

import pytest

from backend.scripts.visa_engine.gold_replay_driver import PACKS_DIR, _repository_trust_store
from backend.services.visa_engine.bundle import canonicalize_json, verify_rule_pack
from backend.services.visa_engine.compiler import compile_rule_pack
from backend.services.visa_engine.errors import RulePackVerificationError

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
