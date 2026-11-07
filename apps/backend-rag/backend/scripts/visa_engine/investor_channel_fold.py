"""Add channel constraints for an expressly requested onshore E28A route.

A visit visa for entering Indonesia is not the requested in-country investor
status. A C1 stay is not itself a general bar to conversion. The product and
capital conditions come from the owner's existing signed rules; this ingredient
only addresses an explicitly selected E28A/conversion or bridging workflow.
"""

from __future__ import annotations

import copy
from typing import Any

from backend.scripts.visa_engine.fold_pack_seq28 import _fact_paths, _rule_pack_id
from backend.services.visa_engine.bundle import VerifiedRulePack

_ALIH = "https://www.imigrasi.go.id/wna/izin-tinggal-keimigrasian/izin-tinggal-kunjungan-menjadi-izin-tinggal-terbatas"


def investor_channel_rules(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    rules = {rule["rule_id"]: rule for rule in candidate["rules"]}
    ids = {
        product["product_code"]: product["product_version_id"] for product in candidate["products"]
    }
    bridge_origin = rules["hf.bridging.from-visit-itk"]
    (alih_ref,) = [
        source["source_record_id"]
        for source in candidate["source_records"]
        if source["canonical_url"] == _ALIH and source["status"] == "VERIFIED"
    ]
    selected = [
        {"op": "eq", "fact": "immigration.currently_in_indonesia", "value": True},
        {"op": "intersects", "fact": "intent.purposes", "values": ["INVESTMENT"]},
        {"op": "eq", "fact": "intent.requested_product_code", "value": "E28A"},
    ]
    conversion = [
        *selected,
        {"op": "eq", "fact": "process.application_channel", "value": "ONSHORE_CONVERSION"},
    ]

    def make(
        rule_id: str,
        when: list[dict[str, Any]],
        refs: list[str],
        effect: str,
        reason: str,
        products: list[str] | None = None,
    ) -> dict[str, Any]:
        predicate = {"op": "all", "args": when}
        return {
            "rule_id": rule_id,
            "stage": "HUMAN_REVIEW" if effect == "REQUIRE_REVIEW" else "HARD_FILTER",
            "scope": "GLOBAL" if products is None else "PRODUCTS",
            "product_version_ids": products,
            "when": predicate,
            "effect": {"type": effect, "reason_code": reason},
            "required_facts": _fact_paths(predicate),
            "source_refs": refs,
            "on_unknown": "NO_EFFECT",
            "priority": 100,
            "valid_period": {"from": candidate["created_at"], "to": None},
            "explanation_key": "explain." + rule_id,
            "safety_critical": False,
        }

    return [
        # Otherwise continue assessing E28A and its own unknowns. Do not use
        # entry products as a fallback for this in-country conversion.
        make(
            "hf.visit.not-e28a-conversion",
            conversion,
            [alih_ref],
            "EXCLUDE",
            "VISITOR_ENTRY_NOT_ONSHORE_INVESTOR_CONVERSION",
            [ids["C2"], ids["D12"]],
        ),
        # An expressly selected bridging route from an origin already excluded
        # by the pack calls for advice about alternatives, not a fresh visit visa.
        make(
            "review.e28a.bridging-origin",
            [
                *selected,
                {"op": "eq", "fact": "process.application_channel", "value": "STATUS_BRIDGING"},
                copy.deepcopy(bridge_origin["when"]),
            ],
            list(bridge_origin["source_refs"]),
            "REQUIRE_REVIEW",
            "STATUS_BRIDGING_REVIEW",
        ),
    ]


def apply_investor_channel_requirements(
    candidate: dict[str, Any], *, anchor: VerifiedRulePack
) -> dict[str, Any]:
    """Return an isolated unsigned seq-33 overlay on the verified seq-32 parent."""
    if (
        anchor.unsigned_dev
        or anchor.pack.payload.environment.value != "PRODUCTION"
        or anchor.pack.payload.sequence != 32
        or candidate.get("sequence") != 33
        or candidate.get("rule_pack_id") != str(_rule_pack_id(33))
        or candidate.get("previous_payload_sha256") != anchor.payload_sha256.hex()
        or candidate.get("environment") != "PRODUCTION"
        or candidate.get("rollback_of_payload_sha256") is not None
        or any(key in candidate for key in ("signature", "protected", "payload"))
    ):
        raise ValueError("Expected an unsigned production seq-33 successor to verified seq-32")
    out = copy.deepcopy(candidate)
    new = investor_channel_rules(out)
    if {row["rule_id"] for row in new} & {row["rule_id"] for row in out["rules"]}:
        raise ValueError("The investor channel rules already exist")
    out["rules"].extend(new)
    return out
