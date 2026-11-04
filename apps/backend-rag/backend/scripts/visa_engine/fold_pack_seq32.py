"""Fold seq-32: C2 without a sponsor, D2 multiple-entry, D12 180 days per entry, E30 level bands.

Chained on the signed seq-31 anchor (payload
``7a5d80c670c61b5c63d18e8d07c1368173e0f954a319962d0064b57dc4efdff4``).  Like seq-26, seq-30
and seq-31 this is a mid-week content fold: all 28 ``source_records`` (and every
``OFFICIAL_PORTAL`` stamp, so the freshness boundary) stay byte-identical to signed seq-31.
Products, prices and duration options do not move, with ONE exception: the D12 extension
policy (item 3).  No RANKING rule is added (RANKING rules may not read legal facts,
``compiler.py`` ``RANKING_RULE_USES_LEGAL_FACT``).

Four content items, each backed by a Law card or an owner ruling (except the VOCATIONAL
fallback inside item 4, which keeps today's behaviour pending an owner decision):

1. C2 drops the sponsor conjunct.  Law card ``services/visa_engine/families/law/C/C2.json``
   ``sponsor_required``: ``as_stated_boolean: false``, verbatim "Anda tidak membutuhkan
   penjamin/sponsor untuk mengajukan visa ini." (the card lists exceptions: stateless,
   non-national-passport travel document, listed countries; a national-passport holder is the
   case the rule serves).  ``el.c2.business`` therefore keeps ``intent.purposes intersects
   [BUSINESS_MEETINGS, INVESTMENT]`` and ``intent.stay_days <= 180`` and no longer requires
   ``family.sponsor_confirmed == true``; ``required_facts`` follows.  An offshore business-meetings
   traveller gets C2 next to D2 instead of D2 only.

2. D2 is multiple-entry only.  Law card ``law/D/D2.json`` and the D family are multiple-entry;
   every ``el.d1-*`` rule already carries ``intent.entry_pattern == MULTIPLE`` and the six
   ``el.d2-*`` rules did not.  They now carry it in exactly the D1 shapes.  A SINGLE-entry
   business traveller is no longer offered D2.  D12 is deliberately NOT given the same conjunct:
   owner decision pending (D12 is also a multiple-entry product by its card).

3. D12 is 180 days per entry.  Owner ruling 2026-10-19 (repository memory and ``law/D/GUIDE.md``:
   "D12 conflict resolved to 180 days per entry").  The card ``law/D/D12.json`` ``stay_days``
   reads, verbatim, "Anda dapat tinggal di Indonesia maksimal 180 hari, dihitung sejak tanggal
   kedatangan."  The card ALSO carries an extension sentence ("Izin tinggal dari visa ini bisa
   diperpanjang untuk 180 hari berikutnya ..."); the owner ruling reads the card's "another 180
   days / 12 months or 2 years" passages as visa VALIDITY, not per-entry stay.  So the six
   ``el.d12-*`` rules move ``intent.stay_days <= 360`` -> ``<= 180``, and the D12 product's
   extension policy becomes {status VERIFIED, allowed false, maximum_extensions 0} so display and
   rule agree.  The ruling, not the card, decides; the card text is untouched.

4. The generic E30 yields to the level bands.  New HARD_FILTER ``hf.e30-generic-yields-to-level-band``
   on E30 only: EXCLUDE (``E30_GENERIC_YIELDS_TO_LEVEL_BAND``) when ``intent.purposes intersects
   [STUDY]`` and ``study.level in [PRIMARY, SECONDARY, UNDERGRADUATE, POSTGRADUATE]``,
   ``on_unknown: NO_EFFECT``.  VOCATIONAL keeps today's behaviour (generic E30 only, since E30B no longer
   lists it): ``law/E30/GUIDE.md`` offers no vocational product and says generic E30 is never
   offered, so whether a vocational student should get E30 or nothing is an OWNER decision still
   open (PENDING-ARMS row opened in PR #8908, item 6); RESEARCH and OTHER stay with E30 likewise.  ``hf.e30b-level-band``
   drops VOCATIONAL from its allow list, so E30B is UNDERGRADUATE/POSTGRADUATE only, as the
   E30B card ``law/E30/E30B.json`` ``definition_verbatim`` states: "Menempuh pendidikan di
   Indonesia pada jenjang sarjana, pascasarjana hingga doktoral."  E30E and E30F have no
   interview fact that distinguishes them and their cards are NOT_FOUND, so two more HARD_FILTERs
   (``hf.e30e-route-not-assessed`` / ``hf.e30f-route-not-assessed``, reason codes
   ``E30E_ROUTE_NOT_ASSESSED`` / ``E30F_ROUTE_NOT_ASSESSED``, ``on_unknown: NO_EFFECT``) stop
   them from appearing next to every student; the products stay in the pack.

No regulation article is written anywhere.  This module never signs or activates anything.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from backend.scripts.visa_engine import fold_pack_seq28 as seq28
from backend.scripts.visa_engine import fold_pack_seq30 as seq30
from backend.scripts.visa_engine.compile_pack import compile_pack_source
from backend.scripts.visa_engine.fold_pack_seq24 import (
    assert_no_hard_filter_reads_a_synthesised_twin_basis_fact,
)
from backend.services.visa_engine.bundle import (
    StaticTrustStore,
    canonicalize_json,
    verify_rule_pack,
)
from backend.services.visa_engine.errors import RulePackVerificationError
from backend.services.visa_engine.models import RulePackPayload

SEQ31_SEQUENCE = 31
SEQ31_PAYLOAD_SHA256 = (
    "7a5d80c670c61b5c63d18e8d07c1368173e0f954a319962d0064b57dc4efdff4"  # pragma: allowlist secret
)
SEQ32_SEQUENCE = 32
FOLD_VERSION = "2026.10.22"
# A fixed instant in the past at fold time (the seq-29..31 convention), so the signer's
# chronology check (created_at <= signing time) holds whenever the owner signs.
FOLD_CREATED_AT = "2026-10-22T00:30:00Z"
FOLD_CREATED_BY = "agent.nuzantara.backend-rag.vo-seq32-paths-1022"
NEW_RULE_VALID_FROM = "2026-10-22T00:00:00Z"

C2_PRODUCT_VERSION_ID = "de433757-f7af-5762-ad65-41bc16bc2ae1"
D2_PRODUCT_VERSION_ID = "306725c1-7269-5c45-a4b8-188265fcf4d8"
D12_PRODUCT_VERSION_ID = "63f64a7e-2bff-5a48-8c3b-7ad5349e8c91"
E30_PRODUCT_VERSION_ID = "94d26bed-b3ba-5b9a-9c6c-9c137854293c"
E30B_PRODUCT_VERSION_ID = "904d99ec-d198-5eb8-9dc4-0b2047e49137"
E30E_PRODUCT_VERSION_ID = "19e3ea6c-95c6-5998-b9d7-efc77f583589"
E30F_PRODUCT_VERSION_ID = "235f5dfc-2feb-5d5b-ab09-0f4e4122819e"
# The record that already backs hf.e30a-level-band / hf.e30b-level-band; reused, never re-stamped.
SRC_E30_LEVEL_BAND = "e3572ad2-08a9-55bd-b818-353b3e9db715"

C2_RULE_ID = "el.c2.business"
D2_RULE_IDS = (
    "el.d2-multi-entry-support",
    "el.d2-passport-validity",
    "el.d2-funds-usd-2000",
    "el.d2-cv-required",
    "el.d2-itinerary-required",
    "el.d2-support-letter",
)
D12_RULE_IDS = (
    "el.d12-multi-entry-support",
    "el.d12-passport-validity",
    "el.d12-funds-usd-5000",
    "el.d12-cv-required",
    "el.d12-itinerary-required",
    "el.d12-support-letter",
)
E30B_BAND_RULE_ID = "hf.e30b-level-band"
E30_GENERIC_RULE_ID = "hf.e30-generic-yields-to-level-band"
E30E_RULE_ID = "hf.e30e-route-not-assessed"
E30F_RULE_ID = "hf.e30f-route-not-assessed"

E30_YIELD_LEVELS = ["PRIMARY", "SECONDARY", "UNDERGRADUATE", "POSTGRADUATE"]
D12_STAY_DAYS_PER_ENTRY = 180
OLD_D12_STAY_DAYS = 360
D12_EXTENSION_POLICY = {
    "status": "VERIFIED",
    "allowed": False,
    "reason_code": None,
    "days_per_extension": None,
    "maximum_extensions": 0,
}

MODIFIED_RULE_IDS = frozenset({C2_RULE_ID, *D2_RULE_IDS, *D12_RULE_IDS, E30B_BAND_RULE_ID})
INSERTED_RULE_IDS_IN_ORDER = (E30_GENERIC_RULE_ID, E30E_RULE_ID, E30F_RULE_ID)
INSERTED_RULE_IDS = frozenset(INSERTED_RULE_IDS_IN_ORDER)
MODIFIED_PRODUCT_IDS = frozenset({D12_PRODUCT_VERSION_ID})

_IDENTITY_KEYS = seq28._IDENTITY_KEYS
_PACKS = seq28._PACKS
_canon = seq28._canon
_rules_by_id = seq28._rules_by_id
_fact_paths = seq28._fact_paths

_SPONSOR_CONJUNCT = {"op": "eq", "fact": "family.sponsor_confirmed", "value": True}
_MULTIPLE = {"op": "eq", "fact": "intent.entry_pattern", "value": "MULTIPLE"}
_BUSINESS = {"op": "intersects", "fact": "intent.purposes", "values": ["BUSINESS_MEETINGS"]}
_D2_STAY = {"op": "lte", "fact": "intent.stay_days", "value": 180}


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"fold_pack_seq32: {message}")


def _c2_when(*, with_sponsor: bool) -> dict[str, Any]:
    args: list[dict[str, Any]] = [
        {
            "op": "intersects",
            "fact": "intent.purposes",
            "values": ["BUSINESS_MEETINGS", "INVESTMENT"],
        }
    ]
    if with_sponsor:
        args.append(copy.deepcopy(_SPONSOR_CONJUNCT))
    args.append({"op": "lte", "fact": "intent.stay_days", "value": 180})
    return {"op": "all", "args": args}


def build_c2_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Drop the sponsor conjunct from ``el.c2.business`` (C2 card: no sponsor needed)."""

    if rule.get("rule_id") != C2_RULE_ID:
        _fail(f"expected {C2_RULE_ID!r}, got {rule.get('rule_id')!r}")
    if rule.get("when") != _c2_when(with_sponsor=True):
        _fail(f"{C2_RULE_ID!r} no longer has the seq-31 sponsor-conjunct shape")
    out = copy.deepcopy(rule)
    out["when"] = _c2_when(with_sponsor=False)
    out["required_facts"] = _fact_paths(out["when"])
    return out


def _d2_base() -> dict[str, Any]:
    return {"op": "all", "args": [copy.deepcopy(_BUSINESS), copy.deepcopy(_D2_STAY)]}


def _d2_when(rule_id: str, *, multiple: bool) -> dict[str, Any]:
    """The seq-31 shape (``multiple=False``) or the D1-shaped seq-32 one (``multiple=True``)."""

    base = _d2_base()
    if rule_id == "el.d2-multi-entry-support":
        if not multiple:
            return base
        return {"op": "all", "args": [base, copy.deepcopy(_MULTIPLE)]}
    if not multiple:
        return {"op": "all", "args": [copy.deepcopy(_BUSINESS), base]}
    return {
        "op": "all",
        "args": [
            {"op": "all", "args": [copy.deepcopy(_BUSINESS), copy.deepcopy(_MULTIPLE)]},
            {"op": "all", "args": [base, copy.deepcopy(_MULTIPLE)]},
        ],
    }


def build_d2_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Add ``intent.entry_pattern == MULTIPLE`` to a ``el.d2-*`` rule, the way ``el.d1-*`` has it."""

    rule_id = rule.get("rule_id")
    if rule_id not in D2_RULE_IDS:
        _fail(f"{rule_id!r} is not a D2 rule")
    if rule.get("when") != _d2_when(rule_id, multiple=False):
        _fail(f"{rule_id!r} no longer has the seq-31 shape")
    out = copy.deepcopy(rule)
    out["when"] = _d2_when(rule_id, multiple=True)
    out["required_facts"] = _fact_paths(out["when"])
    return out


def _d12_when(stay_days: int) -> dict[str, Any]:
    return {
        "op": "all",
        "args": [
            {"op": "intersects", "fact": "intent.purposes", "values": ["INVESTMENT"]},
            {"op": "lte", "fact": "intent.stay_days", "value": stay_days},
        ],
    }


def build_d12_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Move a ``el.d12-*`` stay bound from 360 to 180 days per entry (owner ruling 2026-10-19)."""

    rule_id = rule.get("rule_id")
    if rule_id not in D12_RULE_IDS:
        _fail(f"{rule_id!r} is not a D12 rule")
    if rule.get("when") != _d12_when(OLD_D12_STAY_DAYS):
        _fail(f"{rule_id!r} no longer has the seq-31 360-day shape")
    out = copy.deepcopy(rule)
    out["when"] = _d12_when(D12_STAY_DAYS_PER_ENTRY)
    out["required_facts"] = _fact_paths(out["when"])
    return out


def _e30b_band_when(levels: list[str]) -> dict[str, Any]:
    return {
        "op": "all",
        "args": [
            {"op": "intersects", "fact": "intent.purposes", "values": ["STUDY"]},
            {"op": "not_in", "fact": "study.level", "values": levels},
        ],
    }


def build_e30b_band_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """E30B admits UNDERGRADUATE/POSTGRADUATE only; VOCATIONAL leaves its allow list."""

    if rule.get("rule_id") != E30B_BAND_RULE_ID:
        _fail(f"expected {E30B_BAND_RULE_ID!r}, got {rule.get('rule_id')!r}")
    if rule.get("when") != _e30b_band_when(["VOCATIONAL", "UNDERGRADUATE", "POSTGRADUATE"]):
        _fail(f"{E30B_BAND_RULE_ID!r} no longer has the seq-31 allow list")
    out = copy.deepcopy(rule)
    out["when"] = _e30b_band_when(["UNDERGRADUATE", "POSTGRADUATE"])
    out["required_facts"] = _fact_paths(out["when"])
    return out


def _route_filter(
    rule_id: str, product_version_id: str, when: dict[str, Any], reason_code: str
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "stage": "HARD_FILTER",
        "scope": "PRODUCTS",
        "product_version_ids": [product_version_id],
        "when": when,
        "effect": {"type": "EXCLUDE", "reason_code": reason_code},
        "on_unknown": "NO_EFFECT",
        "priority": 100,
        "required_facts": _fact_paths(when),
        "source_refs": [SRC_E30_LEVEL_BAND],
        "valid_period": {"from": NEW_RULE_VALID_FROM, "to": None},
        "explanation_key": f"explain.{rule_id}",
        "safety_critical": False,
    }


def build_e30_generic_filter() -> dict[str, Any]:
    when = {
        "op": "all",
        "args": [
            {"op": "intersects", "fact": "intent.purposes", "values": ["STUDY"]},
            {"op": "in", "fact": "study.level", "values": list(E30_YIELD_LEVELS)},
        ],
    }
    return _route_filter(
        E30_GENERIC_RULE_ID, E30_PRODUCT_VERSION_ID, when, "E30_GENERIC_YIELDS_TO_LEVEL_BAND"
    )


def _study_only_when() -> dict[str, Any]:
    return {"op": "intersects", "fact": "intent.purposes", "values": ["STUDY"]}


def build_e30e_filter() -> dict[str, Any]:
    return _route_filter(
        E30E_RULE_ID, E30E_PRODUCT_VERSION_ID, _study_only_when(), "E30E_ROUTE_NOT_ASSESSED"
    )


def build_e30f_filter() -> dict[str, Any]:
    return _route_filter(
        E30F_RULE_ID, E30F_PRODUCT_VERSION_ID, _study_only_when(), "E30F_ROUTE_NOT_ASSESSED"
    )


_INSERTED_RULE_BUILDERS = {
    E30_GENERIC_RULE_ID: build_e30_generic_filter,
    E30E_RULE_ID: build_e30e_filter,
    E30F_RULE_ID: build_e30f_filter,
}


def _modified_rule_builder(rule_id: str) -> Any:
    if rule_id == C2_RULE_ID:
        return build_c2_rule
    if rule_id in D2_RULE_IDS:
        return build_d2_rule
    if rule_id in D12_RULE_IDS:
        return build_d12_rule
    if rule_id == E30B_BAND_RULE_ID:
        return build_e30b_band_rule
    _fail(f"no builder for {rule_id!r}")


def build_d12_product(product: dict[str, Any]) -> dict[str, Any]:
    """Display agrees with the rule: D12 is 180 days per entry and not extendable."""

    if product.get("product_version_id") != D12_PRODUCT_VERSION_ID:
        _fail("not the D12 product")
    if product.get("extension_policy") != {
        "status": "VERIFIED",
        "allowed": True,
        "reason_code": None,
        "days_per_extension": 180,
        "maximum_extensions": 1,
    }:
        _fail("D12 extension policy is not the seq-31 one")
    out = copy.deepcopy(product)
    out["extension_policy"] = copy.deepcopy(D12_EXTENSION_POLICY)
    return out


def assert_only_expected_changes(restamped: dict[str, Any], out: dict[str, Any]) -> None:
    """Seq-31 may differ from seq-32 in the scheduled rules and the D12 product only."""

    if set(restamped) != set(out):
        _fail("top-level key set changed")
    for key in set(restamped) - _IDENTITY_KEYS - {"rules", "products"}:
        if _canon(restamped[key]) != _canon(out[key]):
            _fail(f"{key} changed; source_records and all other content must be identical")

    if [p["product_version_id"] for p in restamped["products"]] != [
        p["product_version_id"] for p in out["products"]
    ]:
        _fail("product order or membership changed")
    for before, after in zip(restamped["products"], out["products"], strict=True):
        pid = before["product_version_id"]
        if pid in MODIFIED_PRODUCT_IDS:
            if _canon(after) != _canon(build_d12_product(before)):
                _fail(f"modified product {pid!r} does not equal its builder output")
        elif _canon(before) != _canon(after):
            _fail(f"unscheduled product {pid!r} changed")

    before_rules = _rules_by_id(restamped)
    after_rules = _rules_by_id(out)
    expected_order = [
        *(r["rule_id"] for r in restamped["rules"]),
        *INSERTED_RULE_IDS_IN_ORDER,
    ]
    if [r["rule_id"] for r in out["rules"]] != expected_order:
        _fail("rule order or membership changed outside the scheduled inserts")
    for rule_id, before in before_rules.items():
        after = after_rules[rule_id]
        if rule_id in MODIFIED_RULE_IDS:
            if _canon(after) != _canon(_modified_rule_builder(rule_id)(before)):
                _fail(f"modified rule {rule_id!r} does not equal its builder output")
        elif _canon(before) != _canon(after):
            _fail(f"unscheduled rule {rule_id!r} changed")
    for rule_id, builder in _INSERTED_RULE_BUILDERS.items():
        if _canon(after_rules[rule_id]) != _canon(builder()):
            _fail(f"inserted rule {rule_id!r} does not equal its builder output")


def assert_anchor_relative_changes(anchor: dict[str, Any], out: dict[str, Any]) -> None:
    """Compare the seq-32 output directly with the seq-31 anchor."""

    assert_only_expected_changes(
        _with_identity(anchor, str(out["created_at"]), str(out["created_by"])), out
    )


def _with_identity(payload: dict[str, Any], created_at: str, created_by: str) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    out.update(
        {
            "sequence": SEQ32_SEQUENCE,
            "rule_pack_id": str(seq28._rule_pack_id(SEQ32_SEQUENCE)),
            "version": FOLD_VERSION,
            "created_at": created_at,
            "created_by": created_by,
            "previous_payload_sha256": SEQ31_PAYLOAD_SHA256,
            "rollback_of_payload_sha256": None,
        }
    )
    return out


def _assert_compile_gate(out: dict[str, Any], created_at: str) -> None:
    with tempfile.TemporaryDirectory(prefix="seq32-compile-") as temp_dir:
        path = Path(temp_dir) / "rulepack-prod-032.source.json"
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        _, report = compile_pack_source(
            path, observed_at=datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        )
    if not report.ok:
        detail = "; ".join(f"{error.code}:{error.message}" for error in report.errors)
        _fail(f"compile_pack gate failed: {detail}")


def apply_content_edits(
    restamped: dict[str, Any], *, created_at: str = FOLD_CREATED_AT
) -> dict[str, Any]:
    if restamped.get("sequence") != SEQ32_SEQUENCE:
        _fail(f"restamped base carries sequence {restamped.get('sequence')!r}, expected 32")
    rules = _rules_by_id(restamped)
    for rule_id in MODIFIED_RULE_IDS:
        if rule_id not in rules:
            _fail(f"rule {rule_id!r} is absent")
    for rule_id in INSERTED_RULE_IDS:
        if rule_id in rules:
            _fail(f"rule {rule_id!r} already exists")

    out = copy.deepcopy(restamped)
    out["rules"] = [
        _modified_rule_builder(rule["rule_id"])(rule)
        if rule["rule_id"] in MODIFIED_RULE_IDS
        else rule
        for rule in out["rules"]
    ]
    out["rules"].extend(_INSERTED_RULE_BUILDERS[rid]() for rid in INSERTED_RULE_IDS_IN_ORDER)
    out["products"] = [
        build_d12_product(p) if p["product_version_id"] in MODIFIED_PRODUCT_IDS else p
        for p in out["products"]
    ]

    assert_only_expected_changes(restamped, out)
    after = _rules_by_id(out)
    for rule_id in (*sorted(MODIFIED_RULE_IDS), *INSERTED_RULE_IDS_IN_ORDER):
        seq30._assert_required_facts(after[rule_id])
    seq30._assert_candidate_review_inventory(out)
    assert_no_hard_filter_reads_a_synthesised_twin_basis_fact(out)
    RulePackPayload.model_validate(out)
    _assert_compile_gate(out, created_at)
    return out


def fold_from_signed_seq31(
    anchor_source: dict[str, Any],
    anchor_signed: dict[str, Any],
    *,
    trust_store: StaticTrustStore,
    observed_at: datetime | None = None,
    created_at: str = FOLD_CREATED_AT,
    created_by: str = FOLD_CREATED_BY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify signed seq-31, check its payload equals rulepack-prod-031.source.json, and fold."""

    if anchor_source.get("sequence") != SEQ31_SEQUENCE:
        _fail(f"anchor carries sequence {anchor_source.get('sequence')!r}, expected 31")
    if hashlib.sha256(_canon(anchor_source)).hexdigest() != SEQ31_PAYLOAD_SHA256:
        _fail("anchor source is not the pinned signed seq-31 payload")

    signed_at_raw = anchor_signed.get("protected", {}).get("signed_at")
    if not isinstance(signed_at_raw, str):
        _fail("anchor signed bundle is missing protected.signed_at")
    verify_at = observed_at or datetime.fromisoformat(signed_at_raw.replace("Z", "+00:00"))
    try:
        verified = verify_rule_pack(anchor_signed, trust_store=trust_store, observed_at=verify_at)
    except RulePackVerificationError as exc:
        _fail(f"signed seq-31 anchor failed verification: {exc}")
    if verified.unsigned_dev is not False or verified.pack.payload.sequence != SEQ31_SEQUENCE:
        _fail("signed seq-31 anchor did not verify as production sequence 31")
    if verified.payload_sha256.hex() != SEQ31_PAYLOAD_SHA256:
        _fail(
            f"signed seq-31 payload_sha256 {verified.payload_sha256.hex()} != {SEQ31_PAYLOAD_SHA256}"
        )
    if _canon(anchor_signed.get("payload")) != _canon(anchor_source):
        _fail("signed seq-31 payload does not equal rulepack-prod-031.source.json")

    restamped = _with_identity(anchor_source, created_at, created_by)
    out = apply_content_edits(restamped, created_at=created_at)
    assert_anchor_relative_changes(anchor_source, out)
    return restamped, out


def main(argv: list[str] | None = None, *, observed_at: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--anchor-source", type=Path, default=_PACKS / "rulepack-prod-031.source.json"
    )
    parser.add_argument(
        "--anchor-signed", type=Path, default=_PACKS / "rulepack-prod-031.signed.json"
    )
    parser.add_argument("--created-at", default=FOLD_CREATED_AT)
    parser.add_argument("--created-by", default=FOLD_CREATED_BY)
    parser.add_argument("--output", type=Path, default=_PACKS / "rulepack-prod-032.source.json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that the existing --output file matches the fold output.",
    )
    args = parser.parse_args(argv)

    try:
        trust_store = StaticTrustStore.from_env()
    except RulePackVerificationError as exc:
        _fail(f"cannot load production trust store: {exc}")

    anchor_source = seq28._read_json(args.anchor_source)
    anchor_signed = seq28._read_json(args.anchor_signed)
    _, out = fold_from_signed_seq31(
        anchor_source,
        anchor_signed,
        trust_store=trust_store,
        observed_at=observed_at,
        created_at=args.created_at,
        created_by=args.created_by,
    )
    digest = hashlib.sha256(canonicalize_json(out)).hexdigest()

    if args.check:
        if not args.output.exists():
            _fail(f"{args.output} does not exist")
        if _canon(seq28._read_json(args.output)) != _canon(out):
            _fail(f"{args.output} differs from fold_pack_seq32 output")
        print(f"fold_pack_seq32: OK ({args.output.name}, payload_sha256={digest})")
        return 0

    seq28._assert_output_path(args.output, [args.anchor_source, args.anchor_signed])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"fold_pack_seq32: wrote {args.output}")
    print(f"fold_pack_seq32: seq-32 payload_sha256 = {digest}")
    print(f"fold_pack_seq32: modified rules = {sorted(MODIFIED_RULE_IDS)}")
    print(f"fold_pack_seq32: inserted rules = {list(INSERTED_RULE_IDS_IN_ORDER)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
