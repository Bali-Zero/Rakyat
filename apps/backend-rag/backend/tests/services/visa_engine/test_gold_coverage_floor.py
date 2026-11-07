"""Gold-coverage floor: every synthetic persona in the repository corpus must
still be SUPPORTED for its product by the highest signed PRODUCTION pack.

This is the armed twin of ``gold_coverage_replay.py``: the corpus under
``gold_coverage/personas/`` is data, and data without a reader is W81-dead.
The test runs the replay in-process and fails on the first persona whose
product drops out of the candidates — a pack sequence that silently
un-supports a product it used to support goes red here, before it is signed
into production.  An EMPTY corpus fails too (a floor that passes on zero
personas is the W84 green-but-dead shape).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.scripts.visa_engine import gold_coverage_replay as replay
from backend.scripts.visa_engine.gold_coverage_eval import _evaluate, _verified_compiled_pack
from backend.scripts.visa_engine.gold_replay_driver import (
    PACKS_DIR,
    _parse_utc,
    build_persona_request,
    select_highest_repository_pack,
)
from backend.services.visa_engine import evaluator
from backend.services.visa_engine.enums import DecisionState
from backend.services.visa_engine.fact_registry import DEFAULT_FACT_REGISTRY
from backend.tests.services.visa_engine.test_evaluator_gold import Persona
from backend.tests.services.visa_engine.test_interview_walk_census import (
    _route_not_assessed_codes,
)

CORPUS_DIR = Path(__file__).resolve().parent / "gold_coverage" / "personas"

# PINNED to the highest signed pack's own `signed_at`, never the wall clock:
# the selected pack's source_records carry a freshness_policy
# (MAX_AGE_SINCE_VERIFIED_AT) with as little as a 604800s (7-day) window, so
# calling `replay.main` without `--as-of` evaluates at `datetime.now(UTC)` —
# a clock bomb guaranteed to go stale exactly 7 days after the newest
# source's verified_at, with zero code change. It took the ENTIRE merge
# queue down on 2026-08-30 (verified_at 2026-08-23T10:44:48Z + 604800s =
# 2026-08-30T10:44:48Z); at that instant the engine correctly started
# returning HUMAN_REVIEW_REQUIRED for every persona in this floor — the
# engine was right, the wall-clock evaluation was the bug. `signed_at` (not
# `payload.created_at`) because `--as-of` also drives `verify_rule_pack`'s
# `observed_at`, which rejects a signature dated AFTER the observation
# instant.
_, _HIGHEST_SIGNED_PACK = select_highest_repository_pack(PACKS_DIR)
_AS_OF = _HIGHEST_SIGNED_PACK["protected"]["signed_at"]


def _run_corpus(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    rc = replay.main(["--corpus", str(CORPUS_DIR), "--as-of", _AS_OF])
    out = capsys.readouterr().out
    start = out.find("{")
    report = json.loads(out[start:]) if start >= 0 else {}
    return rc, report


def test_corpus_is_not_empty() -> None:
    files = sorted(CORPUS_DIR.glob("*.json"))
    assert files, f"gold-coverage corpus is empty: {CORPUS_DIR}"


def test_every_corpus_persona_is_supported_for_its_product(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, report = _run_corpus(capsys)
    # Products the signed pack PARKS on purpose (`*_ROUTE_NOT_ASSESSED` HARD_FILTER) cannot be
    # SUPPORTED; they are carved out here, derived from the pack, and pinned as an EXCLUDED
    # negative witness by `test_parked_products_end_excluded_with_their_route_not_assessed_code`.
    parked = set(_route_not_assessed_codes())
    failed = [
        p
        for p in report.get("personas", [])
        if not p.get("pass") and p.get("product_code") not in parked
    ]
    assert not failed, "gold-coverage floor broken: " + "; ".join(
        f"{p.get('file')} ({p.get('product_code')}): state={p.get('actual', {}).get('state')} "
        f"missing={p.get('candidates_missing')}"
        for p in failed
    )
    assert report["summary"]["total"] == len(list(CORPUS_DIR.glob("*.json")))
    # A carve-out must stay honest: a parked product that DID pass would mean the pack no longer
    # parks it and the derived set is stale.
    for p in report.get("personas", []):
        if p.get("product_code") in parked:
            assert not p.get("pass"), p.get("file")


def _persona_spec(code: str) -> dict:
    return json.loads((CORPUS_DIR / f"{code}.json").read_text(encoding="utf-8"))


def test_the_parked_products_are_exactly_e30e_and_e30f_on_seq_33() -> None:
    """The carve-out set is derived from the pack and pinned: it may not grow silently."""
    assert _HIGHEST_SIGNED_PACK["payload"]["sequence"] == 33
    assert set(_route_not_assessed_codes()) == {"E30E", "E30F"}


@pytest.mark.parametrize("code", ["E30E", "E30F"])
def test_parked_products_end_excluded_with_their_route_not_assessed_code(code: str) -> None:
    """Negative witness, not a skip: the persona's product is EXCLUDED by exactly the
    pack's `<CODE>_ROUTE_NOT_ASSESSED` reason, and it is not among the candidates."""
    expected_reason = _route_not_assessed_codes()[code]
    assert expected_reason == f"{code}_ROUTE_NOT_ASSESSED"
    spec = _persona_spec(code)
    as_of = _parse_utc(_AS_OF)
    out = _evaluate(spec["overrides"], spec["label"], as_of=as_of)
    assert code not in (out["actual"]["candidates"] or []), code

    _, compiled = _verified_compiled_pack(as_of)
    persona = Persona(
        id=0,
        label=spec["label"],
        overrides=spec["overrides"],
        expected_state=DecisionState.NEEDS_INPUT,
    )
    facts = build_persona_request(persona).applicant_facts()
    snapshot = DEFAULT_FACT_REGISTRY.derive(facts, effective_at=as_of)
    product = next(p for p in compiled.products if p.product_code == code)
    proof = evaluator.evaluate_product(
        product=product,
        rules=compiled.rules_for(product, effective_at=as_of),
        facts=snapshot,
        purposes=frozenset({"STUDY"}),
    )
    assert proof.status is evaluator.ProductProofStatus.EXCLUDED, (code, proof.status)
    assert [reason.code for reason in proof.reasons] == [expected_reason], code


def test_corpus_file_names_match_their_product_code() -> None:
    """``<CODE>[__CASE].json`` must declare ``product_code == CODE``.

    A named suffix permits multiple legally distinct personas for one product;
    the prefix still prevents a renamed fixture from silently claiming another
    product's expectations.
    """
    for path in sorted(CORPUS_DIR.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        product_code = path.stem.split("__", 1)[0]
        assert spec.get("product_code") == product_code, path.name
        assert product_code in (spec.get("expected_candidates") or []), path.name


#: Named-case positive variants whose delta from the base persona is PINNED, so a
#: variant that drifts by an extra fact cannot silently move the goalposts.  A
#: minor needs two facts (age plus the guardian consent that is the only other
#: fact a minor application carries) — the same exception as E30A__minor.
_VARIANT_DELTAS: dict[str, dict[str, dict]] = {
    "E31F__minor": {
        "person.birth_date": {"status": "KNOWN", "value": "2013-05-10"},
        "person.guardian_consent": {"status": "KNOWN", "value": True},
    },
    "E31F__two_year": {
        "intent.stay_days": {"status": "KNOWN", "value": 730},
    },
}


@pytest.mark.parametrize("case", sorted(_VARIANT_DELTAS))
def test_e31f_variants_differ_from_the_base_in_exactly_the_pinned_facts(case: str) -> None:
    variant = json.loads((CORPUS_DIR / f"{case}.json").read_text(encoding="utf-8"))
    base = json.loads((CORPUS_DIR / "E31F.json").read_text(encoding="utf-8"))
    v, b = variant["overrides"], base["overrides"]
    changed = {k for k in set(v) | set(b) if v.get(k) != b.get(k)}
    assert changed == set(_VARIANT_DELTAS[case]), (case, sorted(changed))
    for path, fact in _VARIANT_DELTAS[case].items():
        assert v[path] == fact, (case, path)


@pytest.mark.parametrize("case", sorted(_VARIANT_DELTAS))
def test_e31f_variants_decide_exactly_e31f_on_the_highest_signed_pack(case: str) -> None:
    """The floor only needs E31F to be PRESENT; these variants are pinned as the
    EXACT candidate list [E31F], so a spurious extra product turns this red."""
    spec = json.loads((CORPUS_DIR / f"{case}.json").read_text(encoding="utf-8"))
    out = _evaluate(spec["overrides"], spec["label"], as_of=_parse_utc(_AS_OF))
    assert out["actual"]["state"] == "SUPPORTED_CANDIDATES", case
    assert sorted(out["actual"]["candidates"] or []) == ["E31F"], case


@pytest.mark.parametrize(
    ("code", "specific"),
    [
        ("E28A", "investment.sponsoring_company_confirmed"),
        ("E31A", "family.spouse_application_support_confirmed"),
    ],
)
@pytest.mark.parametrize(
    "answer", [{"status": "KNOWN", "value": False}, {"status": "UNKNOWN", "reason": "UNVERIFIED"}]
)
def test_positive_personas_need_specific_sponsor_confirmation(
    code: str, specific: str, answer: dict
) -> None:
    spec = _persona_spec(code)
    spec["overrides"][specific] = answer
    result = _evaluate(spec["overrides"], spec["label"], as_of=_parse_utc(_AS_OF))
    assert code not in result["actual"]["candidates"]
