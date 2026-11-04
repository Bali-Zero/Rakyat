"""Seq-32 fold gates: C2 without a sponsor, D2 multiple-entry, D12 180 days per entry, E30 bands.

Chained on verified signed ``seq-31`` (``rulepack-prod-031.signed.json``, payload SHA-256
``7a5d80c670c61b5c63d18e8d07c1368173e0f954a319962d0064b57dc4efdff4``).

The tests in ``TestSignedBundleTiesToSource`` need ``rulepack-prod-032.signed.json``; until the
owner signs the candidate they skip, and they run unchanged once the bundle exists.  Every other
test grades the unsigned source.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from backend.scripts.visa_engine import fold_pack_seq28 as seq28_mod
from backend.scripts.visa_engine import fold_pack_seq32 as fold_mod
from backend.scripts.visa_engine.compile_pack import compile_pack_source
from backend.services.visa_engine.bundle import (
    StaticTrustStore,
    canonicalize_json,
    verify_rule_pack,
)
from backend.services.visa_engine.errors import RulePackVerificationError
from backend.services.visa_engine.models import RulePackPayload
from backend.tests.services.visa_engine.test_seq28_pack import (
    PROD_TRUST_STORE_JSON,
    _portals,
    _read_json,
    _rules,
)

_REPO_ROOT = Path(__file__).resolve().parents[6]
_PACKS = _REPO_ROOT / "apps/backend-rag/backend/services/visa_engine/contracts/packs"
_SEQ31_SOURCE = _PACKS / "rulepack-prod-031.source.json"
_SEQ31_SIGNED = _PACKS / "rulepack-prod-031.signed.json"
_SEQ32_SOURCE = _PACKS / "rulepack-prod-032.source.json"
_SEQ32_SIGNED = _PACKS / "rulepack-prod-032.signed.json"
_SEQ31_SHA = "7a5d80c670c61b5c63d18e8d07c1368173e0f954a319962d0064b57dc4efdff4"
_SPONSOR = "family.sponsor_confirmed"
_ENTRY = "intent.entry_pattern"
_STAY = "intent.stay_days"
_INSERTED = {
    "hf.e30-generic-yields-to-level-band": "E30_GENERIC_YIELDS_TO_LEVEL_BAND",
    "hf.e30e-route-not-assessed": "E30E_ROUTE_NOT_ASSESSED",
    "hf.e30f-route-not-assessed": "E30F_ROUTE_NOT_ASSESSED",
}

requires_signed = pytest.mark.skipif(
    not _SEQ32_SIGNED.is_file(),
    reason="rulepack-prod-032.signed.json is signed by the owner after the gates",
)


@pytest.fixture(scope="module", autouse=True)
def prod_trust_store_env() -> None:
    import os

    old = os.environ.get("VISA_ENGINE_TRUST_STORE_KEYS_JSON")
    os.environ["VISA_ENGINE_TRUST_STORE_KEYS_JSON"] = PROD_TRUST_STORE_JSON
    yield
    if old is None:
        os.environ.pop("VISA_ENGINE_TRUST_STORE_KEYS_JSON", None)
    else:
        os.environ["VISA_ENGINE_TRUST_STORE_KEYS_JSON"] = old


@pytest.fixture(scope="module")
def anchor() -> dict[str, Any]:
    return _read_json(_SEQ31_SOURCE)


@pytest.fixture(scope="module")
def anchor_signed() -> dict[str, Any]:
    return _read_json(_SEQ31_SIGNED)


@pytest.fixture(scope="module")
def seq32() -> dict[str, Any]:
    return _read_json(_SEQ32_SOURCE)


@pytest.fixture(scope="module")
def folded(
    anchor: dict[str, Any], anchor_signed: dict[str, Any], seq32: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    return fold_mod.fold_from_signed_seq31(
        anchor,
        anchor_signed,
        trust_store=StaticTrustStore.from_env(),
        created_at=str(seq32["created_at"]),
        created_by=str(seq32["created_by"]),
    )


@pytest.fixture(scope="module")
def restamped(folded: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    return folded[0]


def _flat_args(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Every leaf condition under nested ``all`` nodes."""
    if node["op"] == "all":
        return [leaf for arg in node["args"] for leaf in _flat_args(arg)]
    return [node]


def _facts_in(node: dict[str, Any]) -> list[str]:
    return sorted({leaf["fact"] for leaf in _flat_args(node)})


def _by_prefix(payload: dict[str, Any], prefix: str) -> dict[str, dict[str, Any]]:
    return {rid: r for rid, r in _rules(payload).items() if rid.startswith(prefix)}


def _d12_product(payload: dict[str, Any]) -> dict[str, Any]:
    return next(
        p for p in payload["products"] if p["product_version_id"] == fold_mod.D12_PRODUCT_VERSION_ID
    )


class TestCommittedSourceMatchesFold:
    def test_committed_seq32_source_is_byte_for_byte_the_fold_output(
        self, folded: tuple[dict[str, Any], dict[str, Any]], seq32: dict[str, Any]
    ) -> None:
        _, out = folded
        assert canonicalize_json(out) == canonicalize_json(seq32), (
            "the committed seq-32 source is not what fold_pack_seq32 produces; "
            "rerun the fold, never edit it by hand"
        )

    def test_cli_check_passes_on_the_committed_source(self) -> None:
        assert fold_mod.main(["--check"]) == 0

    def test_compiler_accepts_the_pack(self, seq32: dict[str, Any], tmp_path: Path) -> None:
        path = tmp_path / "rulepack-prod-032.source.json"
        path.write_text(json.dumps(seq32, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _, report = compile_pack_source(
            path, observed_at=datetime.fromisoformat(seq32["created_at"].replace("Z", "+00:00"))
        )
        assert report.ok, [f"{e.code}:{e.message}" for e in report.errors]


class TestPackIntegrity:
    def test_chain_sequence_and_uuid5(self, seq32: dict[str, Any], anchor: dict[str, Any]) -> None:
        assert seq32["sequence"] == 32
        assert anchor["sequence"] == 31
        assert seq32["version"] == fold_mod.FOLD_VERSION
        assert seq32["previous_payload_sha256"] == fold_mod.SEQ31_PAYLOAD_SHA256 == _SEQ31_SHA
        assert hashlib.sha256(canonicalize_json(anchor)).hexdigest() == _SEQ31_SHA
        assert seq32["rollback_of_payload_sha256"] is None
        assert seq32["rule_pack_id"] == str(seq28_mod._rule_pack_id(32))

    def test_created_at_is_a_past_instant_so_the_signer_chronology_check_holds(
        self, seq32: dict[str, Any], anchor: dict[str, Any]
    ) -> None:
        created = datetime.fromisoformat(seq32["created_at"].replace("Z", "+00:00"))
        assert created <= datetime.now(timezone.utc)
        assert created > datetime.fromisoformat(anchor["created_at"].replace("Z", "+00:00"))
        assert seq32["created_at"] == fold_mod.FOLD_CREATED_AT

    def test_only_expected_paths_move(
        self, restamped: dict[str, Any], seq32: dict[str, Any], anchor: dict[str, Any]
    ) -> None:
        fold_mod.assert_only_expected_changes(restamped, seq32)
        fold_mod.assert_anchor_relative_changes(anchor, seq32)

    def test_source_records_and_portal_stamps_are_byte_identical_to_seq31(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        assert canonicalize_json(anchor["source_records"]) == canonicalize_json(
            seq32["source_records"]
        )
        assert canonicalize_json(_portals(anchor)) == canonicalize_json(_portals(seq32))
        assert len(_portals(seq32)) == 18
        assert len(seq32["source_records"]) == 28

    def test_every_key_other_than_identity_rules_and_products_is_identical(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        moved = {
            key for key in anchor if canonicalize_json(anchor[key]) != canonicalize_json(seq32[key])
        }
        assert moved == {
            "version",
            "sequence",
            "created_at",
            "created_by",
            "rule_pack_id",
            "previous_payload_sha256",
            "rules",
            "products",
        }

    def test_products_and_prices_are_identical_except_the_d12_extension_policy(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        assert len(seq32["products"]) == len(anchor["products"]) == 38
        changed = []
        for before, after in zip(anchor["products"], seq32["products"], strict=True):
            assert before["product_version_id"] == after["product_version_id"]
            if canonicalize_json(before) != canonicalize_json(after):
                changed.append(after["product_version_id"])
                assert {k: v for k, v in before.items() if k != "extension_policy"} == {
                    k: v for k, v in after.items() if k != "extension_policy"
                }
        assert changed == [fold_mod.D12_PRODUCT_VERSION_ID]

    def test_model_validates(self, seq32: dict[str, Any]) -> None:
        assert RulePackPayload.model_validate(seq32).sequence == 32

    def test_every_rule_source_ref_and_product_id_resolves(self, seq32: dict[str, Any]) -> None:
        source_ids = {r["source_record_id"] for r in seq32["source_records"]}
        product_ids = {p["product_version_id"] for p in seq32["products"]}
        for rule_id, rule in _rules(seq32).items():
            assert set(rule["source_refs"]) <= source_ids, rule_id
            if rule["product_version_ids"] is not None:
                assert set(rule["product_version_ids"]) <= product_ids, rule_id


class TestExactRuleDiff:
    def test_fourteen_modified_three_inserted_and_nothing_retired(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        before, after = _rules(anchor), _rules(seq32)
        assert set(before) - set(after) == set()
        assert set(after) - set(before) == set(_INSERTED)
        changed = {rid for rid in set(before) & set(after) if before[rid] != after[rid]}
        assert changed == {
            "el.c2.business",
            *fold_mod.D2_RULE_IDS,
            *fold_mod.D12_RULE_IDS,
            "hf.e30b-level-band",
        }
        assert len(changed) == 14
        assert len(before) == 118 and len(after) == 121

    def test_rule_order_keeps_every_existing_slot_and_appends_the_three_filters(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        before = [r["rule_id"] for r in anchor["rules"]]
        after = [r["rule_id"] for r in seq32["rules"]]
        assert after == [*before, *_INSERTED]

    def test_each_changed_rule_keeps_every_key_but_when_and_required_facts(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        before, after = _rules(anchor), _rules(seq32)
        for rid in fold_mod.MODIFIED_RULE_IDS:
            for key in set(before[rid]) - {"when", "required_facts"}:
                assert after[rid][key] == before[rid][key], (rid, key)
            assert after[rid]["required_facts"] == _facts_in(after[rid]["when"]), rid


class TestC2WithoutSponsor:
    def test_sponsor_conjunct_is_gone_and_required_facts_follows(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        before = _rules(anchor)["el.c2.business"]
        after = _rules(seq32)["el.c2.business"]
        assert _SPONSOR in _facts_in(before["when"])
        assert _SPONSOR not in json.dumps(after["when"])
        assert _SPONSOR not in after["required_facts"]
        assert after["when"] == {
            "op": "all",
            "args": [
                {
                    "op": "intersects",
                    "fact": "intent.purposes",
                    "values": ["BUSINESS_MEETINGS", "INVESTMENT"],
                },
                {"op": "lte", "fact": _STAY, "value": 180},
            ],
        }
        assert after["required_facts"] == ["intent.purposes", _STAY]

    def test_no_other_rule_gained_or_lost_a_sponsor_read(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        def readers(payload: dict[str, Any]) -> set[str]:
            return {rid for rid, r in _rules(payload).items() if _SPONSOR in json.dumps(r)}

        assert readers(anchor) - readers(seq32) == {"el.c2.business"}
        assert readers(seq32) - readers(anchor) == set()


class TestD2MultipleEntry:
    def test_all_six_d2_rules_carry_multiple_entry(self, seq32: dict[str, Any]) -> None:
        d2 = _by_prefix(seq32, "el.d2-")
        assert set(d2) == set(fold_mod.D2_RULE_IDS) and len(d2) == 6
        for rid, rule in d2.items():
            leaves = [x for x in _flat_args(rule["when"]) if x["fact"] == _ENTRY]
            assert leaves, rid
            assert all(x == {"op": "eq", "fact": _ENTRY, "value": "MULTIPLE"} for x in leaves), rid
            assert _ENTRY in rule["required_facts"], rid

    def test_d2_has_the_same_shape_as_d1_with_the_business_purpose(
        self, seq32: dict[str, Any]
    ) -> None:
        d1, d2 = _by_prefix(seq32, "el.d1-"), _by_prefix(seq32, "el.d2-")
        assert len(d1) == len(d2) == 6
        for d1_id, d1_rule in d1.items():
            d2_rule = d2[d1_id.replace("el.d1-", "el.d2-")]
            narrowed = json.loads(
                json.dumps(d1_rule["when"]).replace(
                    '["TOURISM", "FAMILY", "TRANSIT", "BUSINESS_MEETINGS"]',
                    '["BUSINESS_MEETINGS"]',
                )
            )
            assert d2_rule["when"] == narrowed, d1_id

    def test_the_d1_rules_did_not_move(self, anchor: dict[str, Any], seq32: dict[str, Any]) -> None:
        for rid, rule in _by_prefix(anchor, "el.d1-").items():
            assert _rules(seq32)[rid] == rule, rid


class TestD12PerEntryStay:
    def test_all_six_d12_rules_use_180_days_and_no_entry_pattern(
        self, seq32: dict[str, Any]
    ) -> None:
        d12 = _by_prefix(seq32, "el.d12-")
        assert set(d12) == set(fold_mod.D12_RULE_IDS) and len(d12) == 6
        for rid, rule in d12.items():
            assert {"op": "lte", "fact": _STAY, "value": 180} in _flat_args(rule["when"]), rid
            assert "360" not in json.dumps(rule["when"]), rid
            assert _ENTRY not in json.dumps(rule), rid

    def test_d12_extension_policy_is_verified_and_not_extendable(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        assert _d12_product(anchor)["extension_policy"]["allowed"] is True
        after = _d12_product(seq32)["extension_policy"]
        assert after["status"] == "VERIFIED"
        assert after["allowed"] is False
        assert after["maximum_extensions"] == 0


class TestE30LevelBands:
    @pytest.mark.parametrize(("rule_id", "reason"), sorted(_INSERTED.items()))
    def test_inserted_hard_filter_shape(
        self, seq32: dict[str, Any], rule_id: str, reason: str
    ) -> None:
        rule = _rules(seq32)[rule_id]
        assert rule["stage"] == "HARD_FILTER"
        assert rule["effect"] == {"type": "EXCLUDE", "reason_code": reason}
        assert rule["on_unknown"] == "NO_EFFECT"
        assert rule["safety_critical"] is False
        assert len(rule["product_version_ids"]) == 1
        assert rule["source_refs"] == [fold_mod.SRC_E30_LEVEL_BAND]

    def test_generic_e30_yields_to_the_four_bands_and_keeps_vocational(
        self, seq32: dict[str, Any]
    ) -> None:
        rule = _rules(seq32)["hf.e30-generic-yields-to-level-band"]
        assert rule["product_version_ids"] == [fold_mod.E30_PRODUCT_VERSION_ID]
        assert rule["when"]["args"][1] == {
            "op": "in",
            "fact": "study.level",
            "values": ["PRIMARY", "SECONDARY", "UNDERGRADUATE", "POSTGRADUATE"],
        }
        assert "VOCATIONAL" not in json.dumps(rule["when"])

    def test_e30e_and_e30f_filters_target_their_own_product_for_study(
        self, seq32: dict[str, Any]
    ) -> None:
        for rid, pid in (
            ("hf.e30e-route-not-assessed", fold_mod.E30E_PRODUCT_VERSION_ID),
            ("hf.e30f-route-not-assessed", fold_mod.E30F_PRODUCT_VERSION_ID),
        ):
            rule = _rules(seq32)[rid]
            assert rule["product_version_ids"] == [pid]
            assert rule["when"] == {
                "op": "intersects",
                "fact": "intent.purposes",
                "values": ["STUDY"],
            }

    def test_e30b_band_no_longer_lists_vocational(
        self, anchor: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        before = _rules(anchor)["hf.e30b-level-band"]["when"]["args"][1]["values"]
        after = _rules(seq32)["hf.e30b-level-band"]["when"]["args"][1]["values"]
        assert "VOCATIONAL" in before
        assert after == ["UNDERGRADUATE", "POSTGRADUATE"]
        assert "VOCATIONAL" not in json.dumps(_rules(seq32)["hf.e30b-level-band"])


class TestBuilderGuards:
    def test_guilt_builders_refuse_an_already_folded_rule(self, seq32: dict[str, Any]) -> None:
        rules = _rules(seq32)
        with pytest.raises(SystemExit):
            fold_mod.build_c2_rule(rules["el.c2.business"])
        with pytest.raises(SystemExit):
            fold_mod.build_d2_rule(rules["el.d2-cv-required"])
        with pytest.raises(SystemExit):
            fold_mod.build_d12_rule(rules["el.d12-cv-required"])
        with pytest.raises(SystemExit):
            fold_mod.build_e30b_band_rule(rules["hf.e30b-level-band"])
        with pytest.raises(SystemExit):
            fold_mod.build_d12_product(_d12_product(seq32))

    def test_guilt_a_tampered_inserted_filter_is_refused(
        self, restamped: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        tampered = copy.deepcopy(seq32)
        _rules(tampered)["hf.e30e-route-not-assessed"]["on_unknown"] = "NEEDS_INPUT"
        with pytest.raises(SystemExit):
            fold_mod.assert_only_expected_changes(restamped, tampered)

    def test_guilt_a_changed_source_record_or_product_is_refused(
        self, restamped: dict[str, Any], seq32: dict[str, Any]
    ) -> None:
        tampered = copy.deepcopy(seq32)
        tampered["source_records"][0]["title"] += "x"
        with pytest.raises(SystemExit):
            fold_mod.assert_only_expected_changes(restamped, tampered)
        tampered = copy.deepcopy(seq32)
        tampered["products"][0]["product_code"] += "x"
        with pytest.raises(SystemExit):
            fold_mod.assert_only_expected_changes(restamped, tampered)

    def test_guilt_a_wrong_anchor_digest_is_refused(
        self, anchor: dict[str, Any], anchor_signed: dict[str, Any]
    ) -> None:
        drifted = copy.deepcopy(anchor)
        drifted["created_by"] += "x"
        with pytest.raises(SystemExit):
            fold_mod.fold_from_signed_seq31(
                drifted, anchor_signed, trust_store=StaticTrustStore.from_env()
            )


@requires_signed
class TestSignedBundleTiesToSource:
    def test_jcs_payload_equals_source(self, seq32: dict[str, Any]) -> None:
        signed = _read_json(_SEQ32_SIGNED)
        assert canonicalize_json(signed["payload"]) == canonicalize_json(seq32)

    def test_verifies_under_the_pinned_prod_key(self) -> None:
        signed = _read_json(_SEQ32_SIGNED)
        verified = verify_rule_pack(
            signed,
            trust_store=StaticTrustStore.from_env(),
            observed_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        assert verified.pack.protected.kid == "prod-2026-07-1"
        assert verified.pack.payload.sequence == 32

    def test_anchor_is_the_seq31_digest(self) -> None:
        signed = _read_json(_SEQ32_SIGNED)
        assert signed["payload"]["previous_payload_sha256"] == _SEQ31_SHA

    def test_guilt_one_flipped_byte_breaks_signature(self) -> None:
        signed = copy.deepcopy(_read_json(_SEQ32_SIGNED))
        signed["payload"]["created_by"] += "x"
        with pytest.raises(RulePackVerificationError):
            verify_rule_pack(
                signed,
                trust_store=StaticTrustStore.from_env(),
                observed_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
