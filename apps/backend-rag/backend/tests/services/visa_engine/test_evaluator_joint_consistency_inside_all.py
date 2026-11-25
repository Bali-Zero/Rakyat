"""Regression coverage for equality extraction inside conjunctions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.services.visa_engine import models as M
from backend.services.visa_engine.compiler import build_compiled_pack
from backend.services.visa_engine.enums import FactPath
from backend.services.visa_engine.evaluator import ProductProofStatus, evaluate_product
from backend.services.visa_engine.fact_registry import DEFAULT_FACT_REGISTRY
from backend.tests.services.visa_engine import _builders as B
from backend.tests.services.visa_engine.conftest import make_applicant_facts

_EFFECTIVE_AT = datetime(2026, 7, 18, tzinfo=timezone.utc)


def _known(value: object) -> dict[str, object]:
    return {"status": "KNOWN", "value": value}


def _unknown() -> dict[str, str]:
    return {"status": "UNKNOWN", "reason": "NOT_PROVIDED"}


def _facts(overrides: dict[str, object]) -> M.ApplicantFacts:
    base = make_applicant_facts()
    data = base.facts.model_dump(by_alias=True, mode="json")
    data.update(overrides)
    return M.ApplicantFacts(
        schema_version="1.0.0",
        assessment_id=base.assessment_id,
        collected_at=base.collected_at,
        facts=data,
    )


def _proof_for_unknown_marital_status(
    tourism_when: dict[str, object],
    study_when: dict[str, object],
    required_facts: list[str],
):
    product_id = B.new_uuid()
    source_id = B.new_uuid()
    product = B.product(
        product_id=product_id,
        source_id=source_id,
        covered_purposes=["TOURISM", "STUDY"],
    )
    rules = [
        B.rule(
            rule_id="el.tourism",
            stage="ELIGIBILITY",
            scope="PRODUCTS",
            product_version_ids=[product_id],
            when=tourism_when,
            effect={"type": "SUPPORT", "reason_code": "TOURISM", "covered_purposes": ["TOURISM"]},
            source_id=source_id,
            required_facts=required_facts,
        ),
        B.rule(
            rule_id="el.study",
            stage="ELIGIBILITY",
            scope="PRODUCTS",
            product_version_ids=[product_id],
            when=study_when,
            effect={"type": "SUPPORT", "reason_code": "STUDY", "covered_purposes": ["STUDY"]},
            source_id=source_id,
            required_facts=required_facts,
        ),
    ]
    payload = B.rule_pack_payload(
        rules=rules,
        products=[product],
        source_records=[B.source_record(source_id=source_id)],
    )
    compiled = build_compiled_pack(M.RulePack.model_validate(B.rule_pack_envelope(payload)))
    (compiled_product,) = compiled.products
    snapshot = DEFAULT_FACT_REGISTRY.derive(
        _facts(
            {
                "person.marital_status": _unknown(),
                "intent.stay_days": _known(30),
                "study.admission_confirmed": _known(True),
            }
        ),
        effective_at=_EFFECTIVE_AT,
    )
    return evaluate_product(
        product=compiled_product,
        rules=compiled.rules_for(compiled_product, effective_at=_EFFECTIVE_AT),
        facts=snapshot,
        purposes=frozenset({"TOURISM", "STUDY"}),
    )


def _eq(value: str) -> dict[str, object]:
    return {"op": "eq", "fact": "person.marital_status", "value": value}


class TestJointConsistencyInsideAll:
    def test_conflicting_equalities_inside_all_are_unsupported(self) -> None:
        tourism_when = {
            "op": "all",
            "args": [_eq("MARRIED"), {"op": "eq", "fact": "intent.stay_days", "value": 30}],
        }
        study_when = {
            "op": "all",
            "args": [_eq("SINGLE"), {"op": "eq", "fact": "intent.stay_days", "value": 30}],
        }

        proof = _proof_for_unknown_marital_status(
            tourism_when, study_when, ["person.marital_status", "intent.stay_days"]
        )

        assert proof.status is ProductProofStatus.UNSUPPORTED
        assert proof.missing_purposes == frozenset({"TOURISM", "STUDY"})

    def test_conflicting_equalities_inside_nested_all_are_unsupported(self) -> None:
        tourism_when = {
            "op": "all",
            "args": [
                {
                    "op": "all",
                    "args": [_eq("MARRIED"), {"op": "eq", "fact": "intent.stay_days", "value": 30}],
                },
                {"op": "eq", "fact": "study.admission_confirmed", "value": True},
            ],
        }
        study_when = {
            "op": "all",
            "args": [
                {
                    "op": "all",
                    "args": [_eq("SINGLE"), {"op": "eq", "fact": "intent.stay_days", "value": 30}],
                },
                {"op": "eq", "fact": "study.admission_confirmed", "value": True},
            ],
        }

        proof = _proof_for_unknown_marital_status(
            tourism_when,
            study_when,
            ["person.marital_status", "intent.stay_days", "study.admission_confirmed"],
        )

        assert proof.status is ProductProofStatus.UNSUPPORTED
        assert proof.missing_purposes == frozenset({"TOURISM", "STUDY"})

    @pytest.mark.parametrize(
        ("tourism_when", "study_when"),
        [
            (
                {
                    "op": "any",
                    "args": [_eq("MARRIED"), {"op": "eq", "fact": "intent.stay_days", "value": 31}],
                },
                {
                    "op": "any",
                    "args": [_eq("SINGLE"), {"op": "eq", "fact": "intent.stay_days", "value": 31}],
                },
            ),
            ({"op": "not", "arg": _eq("MARRIED")}, {"op": "not", "arg": _eq("SINGLE")}),
            (
                {"op": "neq", "fact": "person.marital_status", "value": "MARRIED"},
                {"op": "neq", "fact": "person.marital_status", "value": "SINGLE"},
            ),
            (
                {"op": "in", "fact": "person.marital_status", "values": ["MARRIED"]},
                {"op": "in", "fact": "person.marital_status", "values": ["SINGLE"]},
            ),
        ],
        ids=["any", "not", "neq", "in"],
    )
    def test_unanalysable_wrappers_leave_unknown_support_paths_blocked(
        self, tourism_when: dict[str, object], study_when: dict[str, object]
    ) -> None:
        required_facts = ["person.marital_status"]
        if tourism_when["op"] == "any":
            required_facts.append("intent.stay_days")

        proof = _proof_for_unknown_marital_status(tourism_when, study_when, required_facts)

        assert proof.status is ProductProofStatus.BLOCKED_UNKNOWN
        assert proof.missing_facts == frozenset({FactPath.PERSON_MARITAL_STATUS})
