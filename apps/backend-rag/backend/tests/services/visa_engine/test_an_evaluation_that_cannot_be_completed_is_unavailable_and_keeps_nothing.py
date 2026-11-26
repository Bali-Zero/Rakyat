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

import json
import logging
import os
import uuid
from base64 import urlsafe_b64encode
from typing import Any

import asyncpg
import pytest

from backend.services.visa_engine import evaluate_path, shadow
from backend.services.visa_engine.bundle import ALLOW_UNSIGNED_ENV_VAR, VerifiedRulePack
from backend.services.visa_engine.crypto import FACTS_FINGERPRINT_KEYS_ENV_VAR
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


# Every environment variable the engine reads carries one of these two prefixes.
ENGINE_VARIABLES = ("VISA_ENGINE_", "VISA_ORACLE_")


class _Stage:
    """The real orchestration over the gold TEST pack, with a recording decision writer.

    Nothing of the machine's own configuration takes part. Every variable the engine reads
    is removed first: the facts-fingerprint keys that the identity provider and the sealing
    keyring read, the evaluate environment, the law overlay, the holding flags, the trust
    store and the unsigned-pack switch. With no key material the identity and the seal are
    the TEST-only placeholder, so no real secret is ever used here and the run is the same
    on every machine. The stage then sets the two variables it needs.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.saved: list[dict[str, Any]] = []
        for name in list(os.environ):
            if name.startswith(ENGINE_VARIABLES):
                monkeypatch.delenv(name, raising=False)
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


def _a_key_for(environment: str) -> str:
    """A well-formed key configuration. Its secret is thirty-two letters k, built here."""
    secret = urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    return json.dumps(
        [
            {
                "kid": "a-key-of-the-machine",
                "environment": environment,
                "secret": secret,
                "valid_from": "2020-01-01T00:00:00+00:00",
            }
        ]
    )


CONFIGURATIONS_OF_THE_MACHINE = {
    "a malformed key configuration": {FACTS_FINGERPRINT_KEYS_ENV_VAR: "not json"},
    "an empty key array": {FACTS_FINGERPRINT_KEYS_ENV_VAR: "[]"},
    "a key for PRODUCTION only": {FACTS_FINGERPRINT_KEYS_ENV_VAR: _a_key_for("PRODUCTION")},
    "a key for TEST": {FACTS_FINGERPRINT_KEYS_ENV_VAR: _a_key_for("TEST")},
    "everything else the engine reads": {
        evaluate_path.EVALUATE_ENVIRONMENT_ENV: "STAGING",
        evaluate_path.LAW_OVERLAY_ENV: "on",
        evaluate_path._HOLDING_FLAGS_ENV_VAR: "not a list of flags",
        ALLOW_UNSIGNED_ENV_VAR: "1",
    },
}


async def _what_a_clean_machine_saves(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    with monkeypatch.context() as clean:
        stage = _Stage(clean)
        body = await stage.run("SHADOW")
    assert _outage(body) is None
    assert len(stage.saved) == 1
    return stage.saved[0]


@pytest.mark.parametrize(
    "configuration",
    CONFIGURATIONS_OF_THE_MACHINE.values(),
    ids=CONFIGURATIONS_OF_THE_MACHINE.keys(),
)
async def test_the_stage_takes_nothing_from_the_configuration_of_the_machine(
    monkeypatch: pytest.MonkeyPatch, configuration: dict[str, str]
) -> None:
    """Whatever the machine has configured, the control answers the same decision with the
    same identity: no key of the machine is used, and none can stop the run."""
    expected = await _what_a_clean_machine_saves(monkeypatch)
    for name, value in configuration.items():
        monkeypatch.setenv(name, value)
    stage = _Stage(monkeypatch)

    body = await stage.run("SHADOW")

    assert _outage(body) is None
    assert len(stage.saved) == 1
    assert stage.saved[0]["request_fingerprint"] == expected["request_fingerprint"]
    assert stage.saved[0]["decision"] == expected["decision"]
    assert stage.saved[0]["environment"] == expected["environment"]


def test_every_variable_the_stage_removes_is_one_the_engine_names() -> None:
    """The two prefixes cover each name the engine's modules define today."""
    named = [
        FACTS_FINGERPRINT_KEYS_ENV_VAR,
        ALLOW_UNSIGNED_ENV_VAR,
        evaluate_path.EVALUATE_MODE_ENV,
        evaluate_path.EVALUATE_ENVIRONMENT_ENV,
        evaluate_path.LAW_OVERLAY_ENV,
        evaluate_path.ALLOW_SYNTHETIC_SOURCES_ENV,
        evaluate_path.DRIVER_TOKEN_ENV,
        evaluate_path.CANARY_TOKEN_ENV,
        evaluate_path._HOLDING_FLAGS_ENV_VAR,
        shadow.MATCH_MODE_ENV,
        shadow.MATCH_ENVIRONMENT_ENV,
    ]

    assert [name for name in named if not name.startswith(ENGINE_VARIABLES)] == []


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
    assert [record.levelno for record in records] == [logging.WARNING]
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
