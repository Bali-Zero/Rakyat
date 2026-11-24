"""A grounded abstain answer is sent with its caution note, and the note survives the size limit.

The WhatsApp sender cuts an over-long body at its end, where the caution note sits. The
codex leg falls off into the worker's retry ladder, as for any oversized answer; the Gemini
leg shortens the answer before the note, so the client is still told the answer is unverified.
"""

from __future__ import annotations

import pytest

from backend.services.integrations.wa_finalize import (
    _WHATSAPP_HARD_SEND_LIMIT,
    FinalizeOutcome,
    finalize_wa_answer,
)
from backend.services.integrations.whatsapp_service import fit_to_whatsapp_limit
from backend.services.rag.agentic._reasoning_stubs import get_localized_stub
from backend.services.rag.agentic.query_helpers import detect_query_language

_QUERY = "How do I open a PT PMA?"
_PARAGRAPH = (
    "To establish a PT PMA in Indonesia you follow these steps. First choose the business "
    "sector and verify the KBLI codes allow foreign ownership. Then reserve the company name "
    "and notarize the deed of establishment."
)


class _Tell:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def __call__(self, reason: str) -> bool:
        self.reasons.append(reason)
        return True


def _language() -> str:
    return detect_query_language(_QUERY)


def _note() -> str:
    return get_localized_stub("low_confidence_note", _language())


def _answer_of(length: int, separator: str = "\n\n") -> str:
    text = separator.join([_PARAGRAPH] * (length // len(_PARAGRAPH) + 2))
    return text[:length].rstrip() + "."


async def _finalize(answer: str, provider: str, tell: _Tell):
    extra = {"price_sources": [], "secret_scan": True} if provider == "codex" else {}
    return await finalize_wa_answer(
        data={
            "answer": answer,
            "abstain": True,
            "abstain_reason": "below_domain_threshold",
            "context_length": 3,
            "evidence_score": 0.5,
        },
        query=_QUERY,
        thread_id=42,
        provider=provider,
        tell_a_human=tell,
        **extra,
    )


@pytest.mark.asyncio
async def test_a_codex_abstain_answer_too_long_for_its_note_is_a_defect() -> None:
    answer = _answer_of(_WHATSAPP_HARD_SEND_LIMIT - 40)
    tell = _Tell()

    result = await _finalize(answer, "codex", tell)

    assert len(answer) < _WHATSAPP_HARD_SEND_LIMIT < len(answer) + len(_note())
    assert result.outcome is FinalizeOutcome.DEFECT
    assert result.defect_reason == "oversized_output"
    assert tell.reasons == []


@pytest.mark.parametrize("separator", ["\n\n", "\n", " "])
@pytest.mark.asyncio
async def test_a_gemini_abstain_answer_is_shortened_before_the_note(separator: str) -> None:
    answer = _answer_of(_WHATSAPP_HARD_SEND_LIMIT - 40, separator)
    marker = get_localized_stub("truncated_tail", _language())

    result = await _finalize(answer, "gemini", _Tell())

    assert result.outcome is FinalizeOutcome.SEND
    assert len(result.text) <= _WHATSAPP_HARD_SEND_LIMIT
    assert result.text.endswith(marker + _note())
    assert answer.startswith(result.text[: -len(marker + _note())])
    assert fit_to_whatsapp_limit(result.text, _language()) == result.text


@pytest.mark.parametrize("provider", ["codex", "gemini"])
@pytest.mark.asyncio
async def test_an_abstain_answer_that_fits_with_its_note_is_sent_whole(provider: str) -> None:
    answer = _answer_of(_WHATSAPP_HARD_SEND_LIMIT - len(_note()) - 1)
    answer = answer + "x" * (_WHATSAPP_HARD_SEND_LIMIT - len(_note()) - len(answer))

    result = await _finalize(answer, provider, _Tell())

    assert len(answer) + len(_note()) == _WHATSAPP_HARD_SEND_LIMIT
    assert result.outcome is FinalizeOutcome.SEND
    assert result.text == answer + _note()


@pytest.mark.asyncio
async def test_a_long_escalated_abstain_answer_still_tells_a_human_on_gemini_only() -> None:
    answer = _answer_of(_WHATSAPP_HARD_SEND_LIMIT - 40) + " [ESCALATE]"
    gemini_tell, codex_tell = _Tell(), _Tell()

    gemini = await _finalize(answer, "gemini", gemini_tell)
    codex = await _finalize(answer, "codex", codex_tell)

    assert gemini.outcome is FinalizeOutcome.SEND
    assert gemini.text.endswith(_note())
    assert "[ESCALATE]" not in gemini.text
    assert gemini_tell.reasons == ["persona_escalate_marker"]
    assert codex.outcome is FinalizeOutcome.DEFECT
    assert codex_tell.reasons == []
