"""
Verification Service
Implements the "Draft -> Verify" pattern for Agentic RAG.
Acts as a judge to evaluate if the generated answer is supported by the retrieved context.

UPDATED 2026-11-27: the judge runs on Claude through the linked-organization Console
door (``claude_oauth_client``, endpoint ``rag.verifier``) instead of Gemini.
"""

import logging
import os
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from backend.llm.claude_oauth_client import (
    ClaudeOAuthNotAvailable,
    collect_seats,
    complete_async,
    default_model,
)

logger = logging.getLogger(__name__)


def _verifier_enabled() -> bool:
    """Read the VERIFIER_ENABLED lever. Default ON — absence changes nothing.

    Falsy set copied from `main_api._wa_outbox_scheduler_enabled` rather than
    invented: an operator who has learnt that `0`/`false`/`no`/`off` turns one
    switch off in this codebase must not discover that a different switch
    silently accepts only one of them and stays ON for the rest.
    """
    return os.getenv("VERIFIER_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


class VerificationStatus(str, Enum):
    """
    Verification status for RAG-generated responses.

    Attributes:
        VERIFIED: Response is fully supported by retrieved context.
        PARTIALLY_VERIFIED: Mostly supported with minor unsupported claims.
        UNVERIFIED: Not supported or contradicts the context.
        HALLUCINATION: Clear fabrication with no basis in context.
    """

    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partial"
    UNVERIFIED = "unverified"
    HALLUCINATION = "hallucination"


class VerificationResult(BaseModel):
    """
    Result of response verification against source context.

    Attributes:
        is_valid: Whether the response passes verification.
        status: Detailed verification status.
        score: Confidence score from 0.0 to 1.0.
        reasoning: Explanation of the verification decision.
        corrected_answer: Suggested correction if verification failed.
        missing_citations: List of claims that lack source citations.
        verdict_available: False when the LLM judge never produced a real
            verdict (empty response, unparseable JSON, or an unexpected
            error) — `score`/`status` in that case are a placeholder, not
            a real judgment, and MUST NOT gate self-correction downstream.
    """

    is_valid: bool
    status: VerificationStatus
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str
    corrected_answer: str | None = None
    missing_citations: list[str] = []
    verdict_available: bool = True


# Thinking + verdict must both fit under this, not the verdict alone — see the
# long note at the call site. Named rather than inlined so a test can assert
# the floor instead of re-litigating the number in a comment.
VERIFIER_MAX_OUTPUT_TOKENS = 8192


class VerifierVerdict(BaseModel):
    """Schema the verifier LLM is constrained to via generate_structured()'s
    response_schema (JSON mode — response_mime_type="application/json",
    no markdown fence, ever; PR #311 convention: `reasoning` FIRST forces
    think-before-commit).

    `status`/`score` are bounded here (round-2 red-team fix, Codex): an
    unconstrained schema let a malformed model output (e.g. score=1.2, or
    a status value outside VerificationStatus) pass generate_structured()
    cleanly, then blow up VerificationResult's own Field(ge=0.0, le=1.0)
    UNCAUGHT in the mapping step below (outside the try/except). Bounding
    it here means generate_structured()'s own model_validate_json +
    one-retry catches it first, surfacing as the already-handled
    LLMStructuredOutputError instead. `status` values must equal
    VerificationStatus's `.value`s 1:1 (see the try/except right below the
    call site, which still maps VerificationStatus(verdict.status))."""

    reasoning: str
    status: Literal["verified", "partial", "unverified", "hallucination"]
    score: float = Field(ge=0.0, le=1.0)
    corrections: str | None = None
    missing_citations: list[str] = []


# JSON Schema handed to the Console client so the verdict comes back as a
# ``structured_output`` object (same flat shape as the surface router's
# classifier schema); ``VerifierVerdict`` still validates it locally.
_VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "status": {
            "type": "string",
            "enum": ["verified", "partial", "unverified", "hallucination"],
        },
        "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "corrections": {"type": ["string", "null"]},
        "missing_citations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reasoning", "status", "score", "corrections", "missing_citations"],
    "additionalProperties": False,
}

_CLAUDE_MODEL_RE = re.compile(r"^claude-[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


def _resolve_verifier_model() -> str:
    """The Claude model the judge runs on.

    ``VERIFIER_MODEL`` is honoured only when it names a ``claude-*`` model: until
    the legacy Gemini value is unset from the deployment, a non-Claude value is
    ignored (and said once) rather than forwarded to the CLI.
    """
    raw = os.getenv("VERIFIER_MODEL", "").strip()
    if raw and _CLAUDE_MODEL_RE.match(raw):
        return raw
    fallback = default_model()
    if raw:
        logger.warning(
            "🛡️ [VerificationService] Ignoring non-Claude VERIFIER_MODEL=%s; using %s",
            raw,
            fallback,
        )
    return fallback


class VerificationService:
    """
    Service responsible for verifying RAG generated responses against source context.
    Uses a lightweight LLM call to act as a 'Guardian'.
    """

    def __init__(self) -> None:
        # A Claude model through the Console door; VERIFIER_MODEL overrides it
        # only when it is a claude-* slug (see _resolve_verifier_model).
        self.model_name = _resolve_verifier_model()
        logger.info("🛡️ [VerificationService] verifier model: %s", self.model_name)
        # Cost lever, default ON so behaviour is unchanged unless someone asks.
        # Measured 2026-08-12 on the live ledger: over 2026-08-10..11 this
        # service was 682 calls / $5.26 of $14.69 total Gemini spend — 36% —
        # and it fires IN ADDITION to every chat call, invisible to the client.
        # Until now there was no way to turn it off short of editing code, so
        # the owner could not act on that number. Whether it SHOULD be off is a
        # product judgement (answer quality vs cost) and is deliberately not
        # decided here: this only builds the switch.
        self._enabled = _verifier_enabled()
        if not self._enabled:
            logger.warning(
                "🛡️ [VerificationService] DISABLED via VERIFIER_ENABLED — "
                "answers ship with verdict_available=False, never a false 'verified'"
            )

    @property
    def _available(self) -> bool:
        """The judge can run: switched on AND a Console seat is configured."""
        return bool(self._enabled and collect_seats())

    async def verify_response(
        self,
        query: str,
        draft_answer: str,
        context_chunks: list[str],
    ) -> VerificationResult:
        """
        Verify if the draft answer is supported by the context chunks.
        """
        if not self._enabled:
            # Same no-verdict SHAPE as the unavailable branch below — that path
            # is already proven not to mint a false "verified" — but with its
            # own honest reason. Reusing the "model unavailable" string here
            # would tell whoever reads this at 07:30 that the model broke, when
            # in fact someone switched it off on purpose.
            return VerificationResult(
                is_valid=True,
                status=VerificationStatus.PARTIALLY_VERIFIED,
                score=0.5,
                reasoning=(
                    "Verification DISABLED (VERIFIER_ENABLED is off) — verdict unavailable."
                ),
                verdict_available=False,
            )

        if not self._available:
            # No verdict path, same as the others below (round-2 red-team
            # fix, Codex): this used to mint a false "verified"
            # (score=1.0, verdict_available defaulting True) when the
            # verifier itself was dead — never let a no-verdict case look
            # like a real judgment.
            return VerificationResult(
                is_valid=True,
                status=VerificationStatus.PARTIALLY_VERIFIED,
                score=0.5,
                reasoning="Verification skipped (model unavailable) — verdict unavailable.",
                verdict_available=False,
            )

        if not context_chunks:
            # If no context but answer claims facts, it's risky.
            # However, for general chit-chat it might be fine.
            # We'll rely on the agent to not act without context.
            return VerificationResult(
                is_valid=True,
                status=VerificationStatus.PARTIALLY_VERIFIED,
                score=0.5,
                reasoning="No context provided for verification.",
                verdict_available=False,
            )

        # Prepare context text
        context_text = "\n\n".join(
            [f"[Source {i + 1}] {chunk}" for i, chunk in enumerate(context_chunks)],
        )

        # Prompt for the verifier
        prompt = f"""
### VERIFICATION TASK
You are an expert Fact Checker for a Legal/Business AI Assistant.
Your job is to verify if the DRAFT ANSWER is fully supported by the RETRIEVED CONTEXT.

**USER QUERY:**
{query}

**RETRIEVED CONTEXT:**
{context_text}

**DRAFT ANSWER:**
{draft_answer}

### INSTRUCTIONS
1. Analyze if every claim in the Draft Answer is supported by the Context.
2. Check for Hallucinations (invented laws, wrong numbers, fake requirements).
3. Ignore stylistic differences, focus on FACTS.
4. If the answer mentions specific regulations/laws, they MUST be in the context.

### OUTPUT FORMAT
Return a JSON object with this exact structure:
{{
    "status": "verified" | "partial" | "unverified" | "hallucination",
    "score": 0.0 to 1.0,
    "reasoning": "Brief explanation of findings",
    "corrections": "If needed, specific corrections based ONLY on context",
    "missing_citations": ["List of claims not found in context"]
}}
"""

        # Use low temperature for deterministic evaluation.
        #
        # CORRECTED 2026-08-09, measured in production. The cap was trimmed
        # 8192 → 2048 "for latency" on the reasoning that a representative
        # verdict measured ~95 output tokens, "leaving ~20x headroom". The
        # verdict is indeed ~95 tokens — but this cap does not govern the
        # verdict. It governs THINKING + verdict, and Gemini 2.5+ thinks by
        # default. The number measured was not the number the constant
        # controls.
        #
        # What that cost, live: `rag.verifier` was failing 10 of 44 calls
        # over 7 days with LLMStructuredOutputError — i.e. the fact-check
        # gate silently off for ~23% of answers, since the failure path
        # returns verdict_available=False and self-correction is skipped.
        # The arithmetic is unambiguous once thinking tokens are recorded
        # (they were invisible until the same day's ledger fix): failed
        # calls report 4062 / 4064 / 4065 output tokens. Halved, that is
        # ~2032 — generate_structured's one retry, with BOTH attempts
        # truncated at the 2048 cap. A successful gemini-3.5-flash call
        # in the same window reports 1661: under the cap, but only just.
        # The incumbent was living one long chain-of-thought from failure.
        #
        # 8192 restored. It is not "generation-sized leftover" — it is the
        # room a thinking judge needs. If latency must be cut, cut it by
        # lowering effort or shortening the context, never by a ceiling
        # that a model's own reasoning has to fit inside.
        #
        # generate_structured() (PR #311) forces JSON mode
        # (response_mime_type="application/json" + response_schema) — the
        # model can no longer wrap its verdict in a markdown ```json fence,
        # which is what silently killed the fact-check gate in prod (the
        # fence made json.loads() raise, minting a fake score=0.5 that
        # reasoning.py's self-correction gate treated as a real verdict).
        # Failure mode if the model still can't produce schema-valid JSON
        # (after generate_structured's own one retry) is safe-by-design:
        # LLMStructuredOutputError, caught below as verdict_available=False
        # — never a corrupted verdict silently gating downstream behavior.
        try:
            response = await complete_async(
                prompt,
                model=self.model_name,
                endpoint="rag.verifier",
                json_schema=_VERIFIER_SCHEMA,
                max_output_tokens=VERIFIER_MAX_OUTPUT_TOKENS,
                timeout_s=30.0,
                total_timeout_s=45.0,
            )
            if response.structured is not None:
                verdict = VerifierVerdict.model_validate(response.structured)
            else:
                raw_text = (response.text or "").strip()
                if raw_text.startswith("```"):
                    raw_text = re.sub(r"^```(?:json)?\s*\n?", "", raw_text)
                    raw_text = re.sub(r"\n?```\s*$", "", raw_text).strip()
                if not raw_text:
                    raise ValueError("empty verifier output")
                verdict = VerifierVerdict.model_validate_json(raw_text)
        except ClaudeOAuthNotAvailable:
            logger.warning("🛡️ [Verifier] Claude Console unavailable — verdict unavailable")
            return VerificationResult(
                is_valid=True,
                status=VerificationStatus.PARTIALLY_VERIFIED,
                score=0.5,
                reasoning="Verification skipped (model unavailable) — verdict unavailable.",
                verdict_available=False,
            )
        except ValidationError:
            # PII boundary (UU PDP / SYMBIOSIS Law 2 is ABSOLUTE): a pydantic
            # ValidationError's text embeds input_value=..., the model's malformed
            # output, which can echo client PII from the verifier prompt. Never
            # log or store the raw exception text.
            logger.warning(
                "🛡️ [Verifier] Model failed to produce a schema-valid verdict "
                "— verdict unavailable (reason=SCHEMA_VALIDATION)",
            )
            return VerificationResult(
                is_valid=True,
                status=VerificationStatus.PARTIALLY_VERIFIED,
                score=0.5,
                reasoning="Verifier verdict unavailable (schema validation failed).",
                verdict_available=False,
            )
        except Exception as e:
            # Unexpected error (network, API, seats exhausted, empty output). Fail
            # safe: allow it, never let the placeholder score gate self-correction.
            # Same PII boundary: the exception TYPE only, never str(e).
            logger.error("❌ [Verifier] Error during verification: %s", type(e).__name__)
            return VerificationResult(
                is_valid=True,
                status=VerificationStatus.PARTIALLY_VERIFIED,
                score=0.5,
                reasoning="Verification failed processing (unexpected error).",
                verdict_available=False,
            )

        try:
            status = VerificationStatus(verdict.status)
        except ValueError:
            status = VerificationStatus.UNVERIFIED
        score = float(verdict.score)

        # Determine validity threshold (strict)
        is_valid = score >= 0.7  # Allow minor deviations, but reject major issues

        logger.info("🛡️ [Verifier] Status: %s | Score: %s", status, score)

        return VerificationResult(
            is_valid=is_valid,
            status=status,
            score=score,
            reasoning=verdict.reasoning,
            corrected_answer=verdict.corrections,
            missing_citations=verdict.missing_citations,
        )


verification_service = VerificationService()
