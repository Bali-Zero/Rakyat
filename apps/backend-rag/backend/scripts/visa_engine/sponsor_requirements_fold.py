"""Content ingredient for the owner of an unsigned Visa Oracle fold.

Call ``apply_sponsor_requirements(candidate_payload, anchor=verified_anchor)``
after the owner's other content edits, then compile the returned payload. The caller supplies
the successor identity and the verified current anchor. This function only
returns data. It does not assign a sequence, write a file, sign or activate.

The E31A input is the Indonesian spouse's own decision on the visa request
letter. An omitted input from an older client stays unknown. For E28A the
evaluation input normalizes a company confirmation that contradicts a NONE
sponsor category to CONFLICTING. Only an explicit refusal triggers a denial.

Legal grounding is the official E31A spouse request letter and the E28A
requirement for shares in the sponsoring company. The corresponding verified
pack records are selected by detail URL or explicit PAGE locator below.
"""

from __future__ import annotations

import copy
from typing import Any

from backend.scripts.visa_engine.fold_pack_seq28 import _PACKS, _fact_paths, _rule_pack_id
from backend.services.visa_engine.bundle import VerifiedRulePack


def _sponsor_filter(
    candidate: dict[str, Any], code: str, denial: dict[str, Any], reason: str
) -> dict[str, Any]:
    (product_id,) = (
        p["product_version_id"] for p in candidate["products"] if p["product_code"] == code
    )
    supports = [
        rule
        for rule in candidate["rules"]
        if rule["effect"]["type"] == "SUPPORT"
        and (rule["scope"] == "GLOBAL" or product_id in (rule["product_version_ids"] or []))
    ]
    if not supports:
        raise ValueError(f"{code} has no support routes to guard")
    # Do not demand a sponsor on a route the applicant cannot otherwise qualify for.
    routes = [rule["when"] for rule in supports]
    # E31A's supplementary SUPPORT rules all require its base spouse route.
    # Omitting these redundant arms keeps unrelated facts off the clarification.
    applicable = next(
        (
            route
            for route in routes
            if all(
                other == route or (other["op"] == "all" and route in other["args"])
                for other in routes
            )
        ),
        {"op": "any", "args": routes},
    )
    when = {"op": "all", "args": [copy.deepcopy(applicable), denial]}
    rule_id = f"hf.{code.lower()}.sponsor-not-confirmed"
    detail_path = f"/wna/daftar-visa-indonesia/{code}"
    catalogue_url = "https://www.imigrasi.go.id/wna/daftar-visa-indonesia"
    support_refs = {ref for rule in supports for ref in rule["source_refs"]}
    refs = [
        source["source_record_id"]
        for source in candidate["source_records"]
        if source["source_record_id"] in support_refs
        and source["status"] == "VERIFIED"
        and (
            source["canonical_url"] == f"{catalogue_url}/{code}"
            or (
                source["canonical_url"] == catalogue_url
                and {"kind": "PAGE", "value": detail_path} in source["locators"]
            )
        )
    ]
    if not refs:
        raise ValueError(f"{code} lacks a verified source for its specific official visa page")
    return {
        "rule_id": rule_id,
        "stage": "HARD_FILTER",
        "scope": "PRODUCTS",
        "product_version_ids": [product_id],
        "when": when,
        "effect": {"type": "EXCLUDE", "reason_code": reason},
        "on_unknown": "NEEDS_INPUT",
        "priority": 100,
        "required_facts": _fact_paths(when),
        "source_refs": refs,
        "valid_period": {"from": candidate["created_at"], "to": None},
        "explanation_key": f"explain.{rule_id}",
        "safety_critical": False,
    }


def apply_sponsor_requirements(
    candidate: dict[str, Any], *, anchor: VerifiedRulePack
) -> dict[str, Any]:
    """Return a new unsigned successor with two route-scoped hard filters."""
    sequence = anchor.pack.payload.sequence + 1
    if (
        anchor.unsigned_dev
        or anchor.pack.payload.environment.value != "PRODUCTION"
        or sequence <= 32
        or candidate.get("sequence") != sequence
        or candidate.get("environment") != "PRODUCTION"
        or candidate.get("rollback_of_payload_sha256") is not None
        or candidate.get("previous_payload_sha256") != anchor.payload_sha256.hex()
        or candidate.get("rule_pack_id") != str(_rule_pack_id(sequence))
        or any(key in candidate for key in ("signature", "protected", "payload"))
        or (_PACKS / f"rulepack-prod-{sequence:03}.signed.json").exists()
    ):
        raise ValueError(
            "Expected an unissued unsigned production successor of the verified anchor"
        )
    out = copy.deepcopy(candidate)
    filters = [
        _sponsor_filter(
            out,
            "E31A",
            {"op": "eq", "fact": "family.spouse_application_support_confirmed", "value": False},
            "E31A_SPOUSE_APPLICATION_SUPPORT_DECLINED",
        ),
        _sponsor_filter(
            out,
            "E28A",
            {"op": "eq", "fact": "investment.sponsoring_company_confirmed", "value": False},
            "E28A_SPONSORING_COMPANY_DECLINED",
        ),
    ]
    existing = {rule["rule_id"] for rule in out["rules"]}
    if any(rule["rule_id"] in existing for rule in filters):
        raise ValueError("A sponsor filter with this identity is already present")
    out["rules"].extend(filters)
    return out
