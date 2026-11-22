"""An evaluation that cannot be completed is answered as unavailable and keeps nothing.

``run_evaluation`` fails closed in seven places between the pack binding and the sealed
decision: the caller gets a retryable outage, no decision is saved, and the log names the
kind of failure without its text, because the step runs over an applicant's facts. None of
that was pinned at this level: the exception classes are tested where they are raised, not
where they are caught, so narrowing an ``except``, reordering the handlers or logging the
exception itself would have left every test green.

Each test runs the real orchestration over the gold TEST pack and breaks exactly one step.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
import pytest

from backend.services.visa_engine import evaluate_path, shadow
from backend.services.visa_engine.bundle import VerifiedRulePack
from backend.services.visa_engine.errors import (
    FactsFingerprintKeyError,
    FactsFingerprintKeyUnavailableError,
    PlaceholderIdentityNotAllowedError,
    RulePackCompilationError,
    RulePackVerificationError,
)
from backend.services.visa_engine.models import RulePack
from backend.tests.services.visa_engine.gold_harness import loader as gold_loader

# What an exception raised over an applicant's facts could carry in its text.
MARKER = "FACT-VALUE-THAT-MUST-NOT-BE-LOGGED"
MODES = ["SHADOW", "ENFORCE"]


class _Stage:
    """The real orchestration over the gold TEST pack, with a recording decision writer."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.saved: list[dict[str, Any]] = []
        compiled = gold_loader.load_and_compile_rule_pack()
        raw = gold_loader.load_rule_pack_raw()
        pack = RulePack.model_validate(raw)

        async def binding(pool: object, **_: object) -> object:
            return shadow._PackBinding(
                rule_pack_id=pack.payload.rule_pack_id,
                ruleset_activation_id=uuid.uuid4(),
                environment="TEST",
                raw_envelope=raw,
            )

        def verify(raw_envelope: object, **_: object) -> VerifiedRulePack:
            return VerifiedRulePack(
                pack=pack,
                canonical_payload=b"",
                payload_sha256=bytes.fromhex(pack.payload_sha256),
                unsigned_dev=False,
            )

        async def save(pool: object, **row: Any) -> None:
            self.saved.append(row)

        async def retention_is_there(*_: object, **__: object) -> bool:
            return True

        class _Prices:
            loaded = True

            def get_service_by_key(self, key: str) -> None:
                return None

            def get_all_prices(self) -> dict[str, object]:
                return {"metadata": {"last_updated": "2026-05-06", "currency": "IDR"}}

        monkeypatch.setattr(evaluate_path, "_resolve_active_pack_binding", binding)
        monkeypatch.setattr(evaluate_path, "verify_rule_pack", verify)
        monkeypatch.setattr(evaluate_path, "build_compiled_pack", lambda *_, **__: compiled)
        monkeypatch.setattr(evaluate_path, "_save_evaluate_decision", save)
        monkeypatch.setattr(evaluate_path, "active_retention_policy_available", retention_is_there)
        monkeypatch.setattr(evaluate_path, "get_pricing_service", lambda: _Prices())
        monkeypatch.setenv("VISA_ENGINE_TRUST_STORE_KEYS_JSON", "[]")

    def breaks(self, step: str, error: BaseException) -> None:
        """Make one named step of the orchestration raise ``error``."""

        def broken(*_: object, **__: object) -> object:
            raise error

        async def broken_async(*_: object, **__: object) -> object:
            raise error

        self.monkeypatch.setattr(
            evaluate_path,
            step,
            broken_async if step == "_resolve_active_pack_binding" else broken,
        )

    async def run(self, mode: str) -> dict[str, Any]:
        self.monkeypatch.setenv(evaluate_path.EVALUATE_MODE_ENV, mode)
        return await evaluate_path.run_evaluation(
            object(),
            facts=gold_loader.load_persona(gold_loader.PERSONAS_DIR / "02_business_c2.json").facts,
            traffic_source="real",
            request_category_hint=None,
            request_trace="trace-fail-closed",
            evaluation_time=gold_loader.GOLD_EFFECTIVE_AT,
        )


@pytest.fixture
def stage(monkeypatch: pytest.MonkeyPatch) -> _Stage:
    return _Stage(monkeypatch)


@pytest.fixture
def records() -> Any:
    """Every record the module's logger emits, whatever the logging setup around it. A
    handler of this test's own: the list is the same object from setup to the last assert."""
    seen: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    handler = _Keep(level=logging.DEBUG)
    level = evaluate_path.logger.level
    evaluate_path.logger.addHandler(handler)
    evaluate_path.logger.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        evaluate_path.logger.setLevel(level)
        evaluate_path.logger.removeHandler(handler)


def _outage(body: dict[str, Any]) -> dict[str, Any] | None:
    return body["decision"].get("outage")


def _everything_logged(records: list[logging.LogRecord]) -> str:
    return "\n".join(f"{record.getMessage()} {record.exc_info!r}" for record in records)


# ---------------------------------------------------------------- the stage itself


async def test_with_nothing_broken_the_stage_answers_a_decision_and_saves_it_once(
    stage: _Stage,
) -> None:
    """The control: every outage below comes from the one step its test breaks."""
    body = await stage.run("SHADOW")

    assert _outage(body) is None
    assert len(stage.saved) == 1


async def test_a_failed_price_projection_is_not_an_outage(
    stage: _Stage, records: list[logging.LogRecord]
) -> None:
    """A price is not the legal answer: the decision is still made and saved."""
    stage.breaks("_attach_price_quotes", ValueError(MARKER))

    body = await stage.run("SHADOW")

    assert _outage(body) is None
    assert len(stage.saved) == 1
    assert "ValueError" in _everything_logged(records)
    assert MARKER not in _everything_logged(records)


# ---------------------------------------------------------------- the rule pack

PACK_FAILURES = {
    "the binding query fails in the server": (
        "_resolve_active_pack_binding",
        asyncpg.PostgresError("binding query failed"),
    ),
    "the binding query fails in the driver": (
        "_resolve_active_pack_binding",
        asyncpg.InterfaceError("connection is closed"),
    ),
    "the pack does not verify": ("verify_rule_pack", RulePackVerificationError("bad signature")),
    "the pack does not compile": ("build_compiled_pack", RulePackCompilationError("bad registry")),
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("step", "error"), PACK_FAILURES.values(), ids=PACK_FAILURES.keys())
async def test_a_pack_that_cannot_be_used_is_answered_as_unavailable(
    stage: _Stage, mode: str, step: str, error: BaseException
) -> None:
    stage.breaks(step, error)

    body = await stage.run(mode)

    assert _outage(body) == {"code": "RULE_PACK_UNAVAILABLE", "retryable": True}
    assert stage.saved == []


@pytest.mark.parametrize("mode", MODES)
async def test_no_active_pack_is_answered_as_unavailable(
    stage: _Stage, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    async def no_binding(pool: object, **_: object) -> None:
        return None

    monkeypatch.setattr(evaluate_path, "_resolve_active_pack_binding", no_binding)

    body = await stage.run(mode)

    assert _outage(body) == {"code": "RULE_PACK_UNAVAILABLE", "retryable": True}
    assert stage.saved == []


# ---------------------------------------------------------------- the evaluation

NAMED_FAILURES = {
    "a pack that needs a real identity provider": (
        "evaluate_with_trace",
        PlaceholderIdentityNotAllowedError(MARKER),
    ),
    "no facts-fingerprint key in its window": (
        "evaluate_with_trace",
        FactsFingerprintKeyUnavailableError(MARKER),
    ),
    "a malformed facts-fingerprint key": (
        "resolve_identity_provider",
        FactsFingerprintKeyError(MARKER),
    ),
}
STEPS_OF_THE_EVALUATION = [
    "resolve_identity_provider",
    "evaluate_with_trace",
    "apply_public_policy_adapters",
    "resolve_engine_hmac_keyring",
    "seal_decision",
]
UNEXPECTED = [ValueError, KeyError, TypeError, RuntimeError, AssertionError]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("step", "error"), NAMED_FAILURES.values(), ids=NAMED_FAILURES.keys())
async def test_a_missing_identity_or_key_is_answered_as_unavailable(
    stage: _Stage, records: list[logging.LogRecord], mode: str, step: str, error: BaseException
) -> None:
    stage.breaks(step, error)

    body = await stage.run(mode)

    assert _outage(body) == {"code": "EVALUATION_UNAVAILABLE", "retryable": True}
    assert stage.saved == []
    assert [record.levelno for record in records] == [logging.WARNING]
    assert MARKER not in _everything_logged(records)
    assert all(record.exc_info is None for record in records)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", UNEXPECTED, ids=[kind.__name__ for kind in UNEXPECTED])
@pytest.mark.parametrize("step", STEPS_OF_THE_EVALUATION)
async def test_any_other_failure_of_the_evaluation_is_answered_as_unavailable(
    stage: _Stage,
    records: list[logging.LogRecord],
    mode: str,
    step: str,
    kind: type[BaseException],
) -> None:
    """Whatever is raised over the applicant's facts: an outage, nothing saved, and the log
    names the class of the failure without its text and without its traceback."""
    stage.breaks(step, kind(MARKER))

    body = await stage.run(mode)

    assert _outage(body) == {"code": "EVALUATION_UNAVAILABLE", "retryable": True}
    assert stage.saved == []
    assert MARKER not in _everything_logged(records)
    assert kind.__name__ in _everything_logged(records)
    assert all(record.exc_info is None for record in records)


async def test_the_log_tells_the_three_causes_apart(
    monkeypatch: pytest.MonkeyPatch, records: list[logging.LogRecord]
) -> None:
    """An operator reading the log knows which of the three to cure: provision an identity
    or a key, repair the key configuration, or look for a defect."""
    told: dict[str, str] = {}
    causes = {
        "a real identity is needed": ("evaluate_with_trace", PlaceholderIdentityNotAllowedError()),
        "no key in its window": ("evaluate_with_trace", FactsFingerprintKeyUnavailableError()),
        "key configuration malformed": ("resolve_identity_provider", FactsFingerprintKeyError()),
        "anything else": ("evaluate_with_trace", RuntimeError()),
    }
    for cause, (step, error) in causes.items():
        records.clear()
        with monkeypatch.context() as only_this_cause:
            stage = _Stage(only_this_cause)
            stage.breaks(step, error)
            await stage.run("SHADOW")
        warnings = [record.msg for record in records if record.levelno >= logging.WARNING]
        assert len(warnings) == 1, (cause, warnings)
        told[cause] = str(warnings[0])

    assert told["a real identity is needed"] == told["no key in its window"]
    assert len(set(told.values())) == 3, told
