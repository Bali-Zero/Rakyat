"""Direct state coverage for disclosed dead-end and holding flags."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.services.visa_engine import evaluate_path
from backend.services.visa_engine.api_models import DisclosedReviewFlag, VisaOracleEvaluateResponse
from backend.services.visa_engine.enums import DecisionState, FactPath
from backend.services.visa_engine.models import Decision, Reason
from backend.tests.services.visa_engine.test_evaluate_endpoint import _supported_baseline

_BASE_NO_PATH_CODE = "PACK_BASE_NO_PATH"
_BASE_REVIEW_CODE = "PACK_BASE_REVIEW"
_DEAD_END_CODE = "DISCLOSED_ACTIVITY_BOUNDARY_NO_PATH"
_ACTIVITY_REVIEW_CODE = "DISCLOSED_ACTIVITY_BOUNDARY_REVIEW"
_CRIMINAL_REVIEW_CODE = "DISCLOSED_CRIMINAL_RECORD_REVIEW"


def _reason(code: str) -> Reason:
    return Reason(code=code, rule_ids=(f"system.test.{code.lower()}",), source_refs=())


def _decision_in_state(state: DecisionState) -> Decision:
    supported = _supported_baseline()
    if state is DecisionState.SUPPORTED_CANDIDATES:
        return supported
    if state is DecisionState.TEMPORARILY_UNAVAILABLE:
        body = evaluate_path.build_temp_unavailable_body(
            now=datetime(2026, 7, 23, tzinfo=timezone.utc),
            code="RULE_PACK_UNAVAILABLE",
        )
        return VisaOracleEvaluateResponse.model_validate(body).decision

    payload = supported.model_dump(mode="python")
    payload.update(
        {
            "state": state,
            "candidates": (),
            "quotes": (),
            "missing_facts": (),
            "review_reasons": (),
            "no_path_reasons": (),
            "outage": None,
        }
    )
    if state is DecisionState.NEEDS_INPUT:
        payload["missing_facts"] = (FactPath.INTENT_STAY_DAYS,)
    elif state is DecisionState.NO_SUPPORTED_PATH:
        payload["no_path_reasons"] = (_reason(_BASE_NO_PATH_CODE),)
    elif state is DecisionState.HUMAN_REVIEW_REQUIRED:
        payload["review_reasons"] = (_reason(_BASE_REVIEW_CODE),)
    return Decision.model_validate(payload)


def _assert_no_path_from_dead_end(decision: Decision, expected_codes: list[str]) -> None:
    assert decision.state is DecisionState.NO_SUPPORTED_PATH
    assert [reason.code for reason in decision.no_path_reasons] == expected_codes
    assert decision.missing_facts == ()
    assert decision.candidates == ()


def test_activity_boundary_replaces_needs_input_with_only_its_dead_end_reason() -> None:
    reviewed = evaluate_path._apply_disclosed_review_flags(
        _decision_in_state(DecisionState.NEEDS_INPUT),
        (DisclosedReviewFlag.ACTIVITY_BOUNDARY,),
    )

    _assert_no_path_from_dead_end(reviewed, [_DEAD_END_CODE])


def test_activity_boundary_replaces_supported_candidates_with_only_its_dead_end_reason() -> None:
    reviewed = evaluate_path._apply_disclosed_review_flags(
        _decision_in_state(DecisionState.SUPPORTED_CANDIDATES),
        (DisclosedReviewFlag.ACTIVITY_BOUNDARY,),
    )

    _assert_no_path_from_dead_end(reviewed, [_DEAD_END_CODE])


def test_activity_boundary_appends_after_the_pack_no_path_reason() -> None:
    reviewed = evaluate_path._apply_disclosed_review_flags(
        _decision_in_state(DecisionState.NO_SUPPORTED_PATH),
        (DisclosedReviewFlag.ACTIVITY_BOUNDARY,),
    )

    _assert_no_path_from_dead_end(reviewed, [_BASE_NO_PATH_CODE, _DEAD_END_CODE])


def test_activity_boundary_joins_an_existing_human_review_hold() -> None:
    reviewed = evaluate_path._apply_disclosed_review_flags(
        _decision_in_state(DecisionState.HUMAN_REVIEW_REQUIRED),
        (DisclosedReviewFlag.ACTIVITY_BOUNDARY,),
    )

    assert reviewed.state is DecisionState.HUMAN_REVIEW_REQUIRED
    assert [reason.code for reason in reviewed.review_reasons] == [
        _BASE_REVIEW_CODE,
        _ACTIVITY_REVIEW_CODE,
    ]
    assert reviewed.no_path_reasons == ()
    assert reviewed.missing_facts == ()
    assert reviewed.candidates == ()


def test_activity_boundary_leaves_a_temporarily_unavailable_pack_unchanged() -> None:
    unavailable = _decision_in_state(DecisionState.TEMPORARILY_UNAVAILABLE)

    reviewed = evaluate_path._apply_disclosed_review_flags(
        unavailable,
        (DisclosedReviewFlag.ACTIVITY_BOUNDARY,),
    )

    assert reviewed == unavailable
    assert reviewed.state is DecisionState.TEMPORARILY_UNAVAILABLE
    assert reviewed.review_reasons == ()
    assert reviewed.no_path_reasons == ()
    assert reviewed.missing_facts == ()
    assert reviewed.candidates == ()


@pytest.mark.parametrize(
    "state",
    (
        DecisionState.NEEDS_INPUT,
        DecisionState.SUPPORTED_CANDIDATES,
        DecisionState.NO_SUPPORTED_PATH,
        DecisionState.HUMAN_REVIEW_REQUIRED,
    ),
)
def test_criminal_record_overrides_activity_boundary_on_every_evaluated_state(
    state: DecisionState,
) -> None:
    reviewed = evaluate_path._apply_disclosed_review_flags(
        _decision_in_state(state),
        (DisclosedReviewFlag.ACTIVITY_BOUNDARY, DisclosedReviewFlag.CRIMINAL_RECORD),
    )

    assert reviewed.state is DecisionState.HUMAN_REVIEW_REQUIRED
    expected_codes = [_CRIMINAL_REVIEW_CODE, _ACTIVITY_REVIEW_CODE]
    if state is DecisionState.HUMAN_REVIEW_REQUIRED:
        expected_codes.insert(0, _BASE_REVIEW_CODE)
    assert [reason.code for reason in reviewed.review_reasons] == expected_codes
    assert reviewed.no_path_reasons == ()
    assert reviewed.missing_facts == ()
    assert reviewed.candidates == ()


def test_an_outage_stays_an_outage_whatever_is_disclosed() -> None:
    # An outage evaluated nothing and carries no decision id, so there is no decision to hold.
    unavailable = _decision_in_state(DecisionState.TEMPORARILY_UNAVAILABLE)

    reviewed = evaluate_path._apply_disclosed_review_flags(
        unavailable,
        (DisclosedReviewFlag.ACTIVITY_BOUNDARY, DisclosedReviewFlag.CRIMINAL_RECORD),
    )

    assert reviewed == unavailable
    assert reviewed.decision_id is None
