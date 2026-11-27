"""Guilt and innocence for the LLM credit sentinel.

The guilt case is easy; the innocence cases are the point. A sentinel that
fires on every 429 gets muted by its audience within a week, and then it is
worth less than no sentinel at all — so a plain rate limit, a network error and
a healthy reply must all stay silent.
"""

from __future__ import annotations

import pytest

from backend.services.hardening.llm_credit_sentinel import (
    CreditState,
    LLMCreditSentinel,
    classify_probe_error,
)

DEPLETED_MSG = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Your prepayment "
    "credits are depleted. Please go to AI Studio at https://ai.studio/projects "
    "to manage your project and billing.'}}"
)
RATE_LIMIT_MSG = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Quota exceeded "
    "for quota metric generate_requests_per_model_per_minute'}}"
)


def _sentinel(probe, sent: list):
    async def notify(text: str) -> bool:
        sent.append(text)
        return True

    return LLMCreditSentinel(probe, {"test": notify}), sent


# --------------------------------------------------------------- classifier


def test_depletion_wording_is_classified_as_depleted():
    v = classify_probe_error(RuntimeError(DEPLETED_MSG))
    assert (v.state, v.should_alert) == (CreditState.DEPLETED, True)


def test_plain_rate_limit_429_is_not_depletion():
    """The exact regression that would make this alarm untrustworthy."""
    v = classify_probe_error(RuntimeError(RATE_LIMIT_MSG))
    assert (v.state, v.should_alert) == (CreditState.RATE_LIMITED, False)


def test_network_error_is_unknown_not_depletion():
    v = classify_probe_error(TimeoutError("connection timed out"))
    assert (v.state, v.should_alert) == (CreditState.UNKNOWN, False)


def test_depletion_is_detected_even_without_the_429_token():
    v = classify_probe_error(RuntimeError("billing account has been depleted"))
    assert v.state is CreditState.DEPLETED


def test_claude_console_quota_error_is_classified_as_depleted():
    from backend.llm.claude_oauth_client import ClaudeOAuthError

    err = ClaudeOAuthError(
        "Claude Console seats failed after 2 attempt(s). Last error class: console_2: quota"
    )
    v = classify_probe_error(err)
    assert (v.state, v.should_alert) == (CreditState.DEPLETED, True)


def test_claude_console_rate_limited_error_is_not_depletion():
    from backend.llm.claude_oauth_client import ClaudeOAuthError

    err = ClaudeOAuthError(
        "Claude Console seats failed after 2 attempt(s). Last error class: console_2: rate limited"
    )
    v = classify_probe_error(err)
    assert (v.state, v.should_alert) == (CreditState.RATE_LIMITED, False)


def test_claude_console_non_billing_errors_are_unknown():
    from backend.llm.claude_oauth_client import ClaudeOAuthError, ClaudeOAuthNotAvailable

    for err in (
        ClaudeOAuthError(
            "Claude Console seats failed after 1 attempt(s). Last error class: console_1: empty_output"
        ),
        ClaudeOAuthError(
            "Claude Console seats failed after 1 attempt(s). Last error class: console_1: cli_timeout"
        ),
        ClaudeOAuthNotAvailable("no Claude Console key is configured"),
    ):
        v = classify_probe_error(err)
        assert (v.state, v.should_alert) == (CreditState.UNKNOWN, False)


# ----------------------------------------------------------------- sentinel


@pytest.mark.asyncio
async def test_depleted_probe_alerts_every_channel():
    async def probe() -> str:
        raise RuntimeError(DEPLETED_MSG)

    sent: list = []
    sentinel, _ = _sentinel(probe, sent)
    verdict = await sentinel.check()
    assert verdict.state is CreditState.DEPLETED
    assert len(sent) == 1
    assert "AI Studio" in sent[0] and "Nuzantara" in sent[0]


@pytest.mark.asyncio
async def test_healthy_probe_stays_silent():
    async def probe() -> str:
        return "PONG"

    sent: list = []
    sentinel, _ = _sentinel(probe, sent)
    verdict = await sentinel.check()
    assert (verdict.state, sent) == (CreditState.OK, [])


@pytest.mark.asyncio
async def test_rate_limited_probe_stays_silent():
    async def probe() -> str:
        raise RuntimeError(RATE_LIMIT_MSG)

    sent: list = []
    sentinel, _ = _sentinel(probe, sent)
    verdict = await sentinel.check()
    assert (verdict.state, sent) == (CreditState.RATE_LIMITED, [])


@pytest.mark.asyncio
async def test_one_dead_channel_does_not_mute_the_others():
    """WhatsApp refuses outside Meta's 24h window — Telegram must still fire."""

    async def probe() -> str:
        raise RuntimeError(DEPLETED_MSG)

    delivered: list = []

    async def broken(text: str) -> bool:
        raise ConnectionError("24h window closed")

    async def working(text: str) -> bool:
        delivered.append(text)
        return True

    sentinel = LLMCreditSentinel(probe, {"wa": broken, "tg": working})
    verdict = await sentinel.check()
    assert verdict.state is CreditState.DEPLETED
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_cli_probe_uses_the_claude_console_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI probe uses the registered hardening.credit_sentinel Console lane."""
    from unittest.mock import AsyncMock

    from backend.llm.claude_oauth_client import ClaudeOAuthResponse
    from backend.services.hardening import llm_credit_sentinel_cli

    fake_complete = AsyncMock(
        return_value=ClaudeOAuthResponse(
            text="PONG", token_label="console_1", elapsed_s=0.1, attempts=1
        )
    )
    monkeypatch.setattr("backend.llm.claude_oauth_client.complete_async", fake_complete)

    reply = await llm_credit_sentinel_cli._probe()

    assert reply == "PONG"
    fake_complete.assert_awaited_once()
    assert fake_complete.await_args.kwargs["endpoint"] == "hardening.credit_sentinel"
    assert fake_complete.await_args.kwargs["max_output_tokens"] == 16
