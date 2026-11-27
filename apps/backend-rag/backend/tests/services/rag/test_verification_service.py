"""
Tests for verification_service.py - RAG response verification against source context.

The judge runs on Claude through the Console door (``claude_oauth_client``); the
Gemini path it used to take is gone.
"""

import logging
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from backend.llm.claude_oauth_client import (
    ClaudeOAuthError,
    ClaudeOAuthNotAvailable,
    ClaudeOAuthResponse,
    default_model,
)
from backend.services.rag import verification_service as vs
from backend.services.rag.verification_service import (
    VERIFIER_MAX_OUTPUT_TOKENS,
    VerificationResult,
    VerificationService,
    VerificationStatus,
    VerifierVerdict,
)

_SEAT = object()


def _response(structured=None, text=""):
    return ClaudeOAuthResponse(
        text=text,
        token_label="console_1",
        elapsed_s=0.1,
        attempts=1,
        structured=structured,
    )


def _verdict_dict(**overrides):
    base = {
        "reasoning": "ok",
        "status": "verified",
        "score": 0.9,
        "corrections": None,
        "missing_citations": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def lane(monkeypatch):
    """A configured Console seat and a recording ``complete_async``."""
    monkeypatch.delenv("VERIFIER_ENABLED", raising=False)
    monkeypatch.setattr(vs, "collect_seats", lambda: [_SEAT])
    mock = AsyncMock(return_value=_response(structured=_verdict_dict()))
    monkeypatch.setattr(vs, "complete_async", mock)
    return mock


class TestVerifierOutputBudget:
    """The cap governs THINKING + verdict, not the verdict alone.

    Measured in production 2026-08-09: with the cap at 2048, `rag.verifier`
    failed 10 of 44 calls over 7 days — the fact-check gate silently off for
    ~23% of answers. The cap is the room a thinking judge needs.
    """

    def test_budget_leaves_room_for_a_thinking_judge(self):
        assert VERIFIER_MAX_OUTPUT_TOKENS > 2048, "2048 truncated 23% of live verdicts"
        assert VERIFIER_MAX_OUTPUT_TOKENS >= 4096

    @pytest.mark.asyncio
    async def test_the_budget_actually_reaches_the_model(self, lane):
        """A constant nothing passes is a comment: pin the call site."""
        await VerificationService().verify_response(
            query="What is KITAS?",
            draft_answer="A temporary stay permit.",
            context_chunks=["KITAS is a temporary stay permit."],
        )
        kwargs = lane.await_args.kwargs
        assert kwargs["max_output_tokens"] == VERIFIER_MAX_OUTPUT_TOKENS
        assert kwargs["endpoint"] == "rag.verifier"
        assert kwargs["json_schema"] is vs._VERIFIER_SCHEMA


class TestVerificationStatus:
    """Tests for VerificationStatus enum."""

    def test_all_statuses_defined(self):
        assert VerificationStatus.VERIFIED == "verified"
        assert VerificationStatus.PARTIALLY_VERIFIED == "partial"
        assert VerificationStatus.UNVERIFIED == "unverified"
        assert VerificationStatus.HALLUCINATION == "hallucination"


class TestVerificationResult:
    """Tests for VerificationResult model."""

    def test_valid_result(self):
        result = VerificationResult(
            is_valid=True,
            status=VerificationStatus.VERIFIED,
            score=0.95,
            reasoning="All claims supported",
        )
        assert result.is_valid is True
        assert result.score == 0.95

    def test_score_bounds_enforced(self):
        with pytest.raises(ValidationError, match="score"):
            VerificationResult(
                is_valid=True,
                status=VerificationStatus.VERIFIED,
                score=1.5,  # Over max
                reasoning="test",
            )

    def test_optional_fields_default(self):
        result = VerificationResult(
            is_valid=True,
            status=VerificationStatus.VERIFIED,
            score=0.8,
            reasoning="test",
        )
        assert result.corrected_answer is None
        assert result.missing_citations == []
        assert result.verdict_available is True


class TestVerifierVerdict:
    """Round-2 red-team fixes (Codex, verified independently): the schema
    the verifier LLM is constrained to must ITSELF reject out-of-range/
    unknown values, not rely on downstream code to catch them — an
    unconstrained VerifierVerdict let a malformed model output (e.g.
    score=1.2) pass generate_structured() cleanly and then blow up
    VerificationResult's Field(ge=0.0, le=1.0) UNCAUGHT (outside the
    try/except in verify_response)."""

    def test_score_above_one_rejected(self):
        with pytest.raises(ValidationError):
            VerifierVerdict(reasoning="x", status="verified", score=1.2)

    def test_score_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            VerifierVerdict(reasoning="x", status="verified", score=-0.1)

    def test_unknown_status_rejected(self):
        with pytest.raises(ValidationError):
            VerifierVerdict(reasoning="x", status="bogus", score=0.5)


class TestVerificationServiceModelConfig:
    """VERIFIER_MODEL only counts when it names a claude-* model."""

    def test_honors_a_claude_verifier_model_env(self, monkeypatch):
        monkeypatch.setenv("VERIFIER_MODEL", "claude-haiku-5-5")
        assert VerificationService().model_name == "claude-haiku-5-5"

    def test_falls_back_to_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("VERIFIER_MODEL", raising=False)
        assert VerificationService().model_name == default_model()

    def test_a_legacy_gemini_value_is_ignored(self, monkeypatch, caplog):
        monkeypatch.setenv("VERIFIER_MODEL", "gemini-2.5-flash")
        with caplog.at_level(logging.WARNING):
            service = VerificationService()
        assert service.model_name == default_model()
        assert "Ignoring non-Claude VERIFIER_MODEL" in caplog.text


class TestVerificationServiceFallbacks:
    """VerificationService without a usable model."""

    @pytest.mark.asyncio
    async def test_model_unavailable_is_no_verdict(self, monkeypatch):
        """No Console seat is a no-verdict case, never a false 'verified'."""
        monkeypatch.delenv("VERIFIER_ENABLED", raising=False)
        monkeypatch.setattr(vs, "collect_seats", lambda: [])
        never = AsyncMock(side_effect=AssertionError("no seat, no call"))
        monkeypatch.setattr(vs, "complete_async", never)
        result = await VerificationService().verify_response(
            query="What is KITAS?",
            draft_answer="KITAS is a temporary stay permit.",
            context_chunks=["KITAS is a temporary stay permit in Indonesia."],
        )
        assert result.is_valid is True
        assert result.status == VerificationStatus.PARTIALLY_VERIFIED
        assert result.score == 0.5
        assert result.verdict_available is False
        assert "unavailable" in result.reasoning.lower()
        never.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_context_returns_partial(self, lane):
        result = await VerificationService().verify_response(
            query="What is KITAS?",
            draft_answer="KITAS is a temporary stay permit.",
            context_chunks=[],
        )
        assert result.status == VerificationStatus.PARTIALLY_VERIFIED
        assert result.score == 0.5
        lane.assert_not_called()


class TestVerificationServiceWithLLM:
    """VerificationService with a mocked Console door."""

    @pytest.mark.asyncio
    async def test_structured_verdict_pass(self, lane):
        lane.return_value = _response(
            structured=_verdict_dict(reasoning="All claims supported by context", score=0.92)
        )
        result = await VerificationService().verify_response(
            query="What is KITAS?",
            draft_answer="KITAS is a temporary stay permit in Indonesia.",
            context_chunks=["KITAS is a temporary stay permit issued by Indonesian immigration."],
        )
        assert result.is_valid is True
        assert result.status == VerificationStatus.VERIFIED
        assert result.score == 0.92
        assert result.verdict_available is True

    @pytest.mark.asyncio
    async def test_structured_verdict_fail_gates(self, lane):
        lane.return_value = _response(
            structured=_verdict_dict(
                reasoning="Claims fabricated law UU 999/2025",
                status="unverified",
                score=0.3,
                corrections="Remove reference to UU 999/2025",
                missing_citations=["UU 999/2025"],
            )
        )
        result = await VerificationService().verify_response(
            query="test",
            draft_answer="According to UU 999/2025...",
            context_chunks=["Real context about immigration"],
        )
        assert result.is_valid is False
        assert result.status == VerificationStatus.UNVERIFIED
        assert result.score == 0.3
        assert result.verdict_available is True
        assert result.missing_citations == ["UU 999/2025"]
        assert result.corrected_answer == "Remove reference to UU 999/2025"

    @pytest.mark.asyncio
    async def test_hallucination_detected(self, lane):
        lane.return_value = _response(
            structured=_verdict_dict(status="hallucination", score=0.1, missing_citations=["x"])
        )
        result = await VerificationService().verify_response(
            query="test", draft_answer="a", context_chunks=["ctx"]
        )
        assert result.is_valid is False
        assert result.status == VerificationStatus.HALLUCINATION

    @pytest.mark.asyncio
    async def test_plain_text_json_is_accepted_when_no_structured_output(self, lane):
        lane.return_value = _response(
            text='{"reasoning":"ok","status":"verified","score":0.8,'
            '"corrections":null,"missing_citations":[]}'
        )
        result = await VerificationService().verify_response(
            query="t", draft_answer="a", context_chunks=["c"]
        )
        assert result.verdict_available is True
        assert result.score == 0.8

    @pytest.mark.asyncio
    async def test_fenced_json_text_is_accepted(self, lane):
        lane.return_value = _response(
            text='```json\n{"reasoning":"ok","status":"partial","score":0.6,'
            '"corrections":null,"missing_citations":[]}\n```'
        )
        result = await VerificationService().verify_response(
            query="t", draft_answer="a", context_chunks=["c"]
        )
        assert result.verdict_available is True
        assert result.status == VerificationStatus.PARTIALLY_VERIFIED

    @pytest.mark.asyncio
    async def test_empty_output_is_no_verdict(self, lane):
        lane.return_value = _response(text="   ")
        result = await VerificationService().verify_response(
            query="t", draft_answer="a", context_chunks=["c"]
        )
        assert result.verdict_available is False
        assert result.score == 0.5

    @pytest.mark.asyncio
    async def test_schema_violation_is_no_verdict_and_leaks_nothing(self, lane, caplog):
        """PII boundary (UU PDP / SYMBIOSIS Law 2 is ABSOLUTE): a malformed verdict
        raises a pydantic error whose text embeds the model's output. Neither the
        log nor the stored reasoning may carry it."""
        lane.return_value = _response(
            structured=_verdict_dict(score=7, reasoning="mario.rossi@example.com passport A9900001")
        )
        with caplog.at_level(logging.DEBUG):
            result = await VerificationService().verify_response(
                query="test", draft_answer="test answer", context_chunks=["context"]
            )
        assert result.verdict_available is False
        assert result.score == 0.5
        assert result.status == VerificationStatus.PARTIALLY_VERIFIED
        for text in (result.reasoning, caplog.text):
            assert "example.com" not in text
            assert "A9900001" not in text

    @pytest.mark.asyncio
    async def test_console_error_does_not_leak_exception_content(self, lane, caplog):
        lane.side_effect = ClaudeOAuthError("seats failed echo mario.rossi@example.com A9900001")
        with caplog.at_level(logging.DEBUG):
            result = await VerificationService().verify_response(
                query="test", draft_answer="test answer", context_chunks=["context"]
            )
        assert result.verdict_available is False
        assert result.is_valid is True
        assert "failed" in result.reasoning.lower()
        for text in (result.reasoning, caplog.text):
            assert "example.com" not in text
            assert "A9900001" not in text

    @pytest.mark.asyncio
    async def test_no_door_at_call_time_is_no_verdict(self, lane):
        lane.side_effect = ClaudeOAuthNotAvailable("no Console seat configured")
        result = await VerificationService().verify_response(
            query="test", draft_answer="a", context_chunks=["c"]
        )
        assert result.verdict_available is False
        assert "unavailable" in result.reasoning.lower()

    @pytest.mark.asyncio
    async def test_llm_error_returns_partial(self, lane):
        """Any other error degrades safely, never gating self-correction."""
        lane.side_effect = Exception("LLM error")
        result = await VerificationService().verify_response(
            query="test", draft_answer="test answer", context_chunks=["context"]
        )
        assert result.is_valid is True
        assert result.status == VerificationStatus.PARTIALLY_VERIFIED
        assert "failed" in result.reasoning.lower()
        assert result.verdict_available is False

    @pytest.mark.asyncio
    async def test_validity_threshold_at_0_7(self, lane):
        lane.return_value = _response(
            structured=_verdict_dict(
                reasoning="Mostly correct but one claim unsupported", status="partial", score=0.69
            )
        )
        result = await VerificationService().verify_response(
            query="test", draft_answer="test", context_chunks=["ctx"]
        )
        assert result.is_valid is False  # 0.69 < 0.7

    @pytest.mark.asyncio
    async def test_context_chunks_formatted_in_prompt(self, lane):
        await VerificationService().verify_response(
            query="test", draft_answer="answer", context_chunks=["chunk 1", "chunk 2"]
        )
        prompt = lane.await_args.args[0]
        assert "[Source 1]" in prompt
        assert "[Source 2]" in prompt


# --- VERIFIER_ENABLED cost lever (2026-08-12) -------------------------------
#
# The switch reuses the proven no-verdict shape (a dead verifier must never mint
# a false "verified"): it really stops the spend (no call is ever made) and it
# never claims an answer was verified. DELIBERATELY default-ON.


def _fresh_service():
    return VerificationService()


@pytest.mark.asyncio
async def test_guilt_disabled_verifier_makes_no_call_and_claims_no_verdict(monkeypatch):
    monkeypatch.setenv("VERIFIER_ENABLED", "0")
    monkeypatch.setattr(vs, "collect_seats", lambda: [_SEAT])
    never = AsyncMock(side_effect=AssertionError("a disabled verifier must never call the model"))
    monkeypatch.setattr(vs, "complete_async", never)
    service = _fresh_service()

    result = await service.verify_response(
        query="quanto costa un KITAS?",
        draft_answer="IDR 12.000.000",
        context_chunks=["chunk"],
    )

    assert result.verdict_available is False, "a disabled verifier must never claim a verdict"
    assert "DISABLED" in result.reasoning
    assert service._available is False
    never.assert_not_called()


@pytest.mark.asyncio
async def test_guilt_the_disabled_reason_does_not_blame_the_model(monkeypatch):
    """A switched-off service must not report itself as broken."""
    monkeypatch.setenv("VERIFIER_ENABLED", "off")
    result = await _fresh_service().verify_response(
        query="q", draft_answer="a", context_chunks=["c"]
    )
    assert "unavailable) —" not in result.reasoning
    assert "VERIFIER_ENABLED" in result.reasoning


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " Off "])
def test_guilt_every_documented_falsy_spelling_turns_it_off(monkeypatch, value):
    monkeypatch.setenv("VERIFIER_ENABLED", value)
    assert _fresh_service()._enabled is False


def test_innocence_unset_env_leaves_the_verifier_on(monkeypatch):
    monkeypatch.delenv("VERIFIER_ENABLED", raising=False)
    assert _fresh_service()._enabled is True


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "", "banana"])
def test_innocence_anything_not_in_the_falsy_set_leaves_it_on(monkeypatch, value):
    """Fail-ON for unrecognised input: a typo must not silently disable a quality gate."""
    monkeypatch.setenv("VERIFIER_ENABLED", value)
    assert _fresh_service()._enabled is True
