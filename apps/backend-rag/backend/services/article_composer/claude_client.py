"""LLM client for article_composer — back on the Claude OAuth door (v5).

History:
- v1: wrapped ``anthropic.Anthropic`` (pay-as-you-go API key).
- v2: switched to Claude Max OAuth via ``claude -p`` subprocess
  (``backend.llm.claude_oauth_client``) per project policy
  ``memory/feedback_claude_oauth_only.md``.
- v3: migrated to DeepSeek because the ``claude`` CLI hangs inside the Fly
  container on Linux non-TTY (``memory/feedback_claude_cli_linux_hang.md``).
- v4 (2026-08-29): re-pointed DeepSeek to the TP1 gateway.
- v5 (this file, 2026-10-22): TP1 was cancelled by the owner (HTTP 403 on
  every slug, no fallback), so the route is back on the only sanctioned
  Claude door: ``backend.llm.claude_oauth_client`` (``claude -p`` on the
  OAuth seats, MAX quota). Model default ``claude-sonnet-5``, override with
  env ``ARTICLE_COMPOSER_MODEL``. ``max_tokens`` is accepted for signature
  compatibility but the CLI door has no output cap; usage is estimated
  (``len(text) // 4``) because the CLI reports no counters.

Public symbols preserved for router/backward-compat:
- ``call_claude_with_retry`` — same signature, internally calls the Claude OAuth client.
- ``ClaudeClientError`` — same retryable exception class.
- ``ClaudeOAuthMessage`` (alias ``Message``) — same response shape
  (``content[0].text``, ``usage.input_tokens``, ``usage.output_tokens``).
- ``get_anthropic_client`` — still raises on call (unchanged).
"""

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
)

from backend.llm.claude_oauth_client import (
    ClaudeOAuthError,
    ClaudeOAuthNotAvailable,
    _collect_tokens,
    complete_async,
)

ARTICLE_COMPOSER_MODEL: str = os.getenv("ARTICLE_COMPOSER_MODEL", "").strip() or "claude-sonnet-5"
# frontend aborts at 120 s (apps/mouth articles.api.ts); the whole call incl. retries must finish before that
ARTICLE_COMPOSER_SEAT_TIMEOUT_S: int = int(os.getenv("ARTICLE_COMPOSER_SEAT_TIMEOUT_S", "55"))
ARTICLE_COMPOSER_TOTAL_BUDGET_S: int = int(os.getenv("ARTICLE_COMPOSER_TOTAL_BUDGET_S", "110"))
ARTICLE_COMPOSER_MIN_ATTEMPT_S: float = float(os.getenv("ARTICLE_COMPOSER_MIN_ATTEMPT_S", "5"))


@dataclass(frozen=True)
class _TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ClaudeOAuthMessage:
    """Minimal drop-in for anthropic.types.Message consumed by callers.

    Name retained for backward compatibility with the Claude-era code; the
    payload now comes from the Claude OAuth client. Router only touches
    ``content[0].text`` and ``usage.{input,output}_tokens``.
    """

    content: list[_TextBlock]
    usage: _Usage
    model: str
    token_label: str

    @property
    def input_text(self) -> str:
        return self.content[0].text if self.content else ""


Message = ClaudeOAuthMessage  # backward-compat alias

logger = logging.getLogger(__name__)


class ClaudeClientError(RuntimeError):
    """Raised for retryable transport errors from the LLM client.

    Wraps ``ClaudeOAuthError`` (every seat failed / timed out) so tenacity
    can retry it.
    """


class CircuitState(Enum):
    """Circuit breaker states"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Simple circuit breaker implementation"""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_max_calls: int = 3,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.half_open_calls = 0

    async def call(self, func: Any, *args, **kwargs) -> Any:
        """Execute async function with circuit breaker protection."""
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
            else:
                raise ClaudeClientError("Circuit breaker is OPEN - service unavailable")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset"""
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.recovery_timeout

    def _on_success(self) -> None:
        """Handle successful call"""
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_calls += 1
            if self.half_open_calls >= self.half_open_max_calls:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                logger.info("Circuit breaker: CLOSED (service recovered)")
        else:
            self.failure_count = 0

    def _on_failure(self) -> None:
        """Handle failed call"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            logger.warning("Circuit breaker: OPEN (half-open test failed)")
        elif self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.error(
                f"Circuit breaker: OPEN (failure threshold {self.failure_threshold} reached)",
            )


_llm_circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)


def get_anthropic_client() -> None:
    """Removed — kept only as a shim that raises, so callers fail loudly.

    This module has no Anthropic SDK dependency. Use
    :func:`call_claude_with_retry` or ``backend.llm.claude_oauth_client``.
    """
    raise RuntimeError(
        "get_anthropic_client() is removed. article_composer uses the Claude OAuth "
        "client (backend.llm.claude_oauth_client). Call call_claude_with_retry() instead.",
    )


_last_error: str | None = None
_last_success_at: str | None = None
_SECRET_LIKE = re.compile(r"\S{24,}")


def _scrub(exc: BaseException) -> str:
    """Error class plus a short message with long token-like runs masked."""
    return f"{type(exc).__name__}: {_SECRET_LIKE.sub('[redacted]', str(exc))[:160]}"


def _record_failure(exc: BaseException) -> None:
    global _last_error
    _last_error = _scrub(exc)


def _record_success() -> None:
    global _last_error, _last_success_at
    _last_error = None
    _last_success_at = datetime.now(timezone.utc).isoformat()


def get_llm_health() -> dict[str, Any]:
    """Truth of the door, not token presence: breaker state + last outcome."""
    return {
        "breaker_state": _llm_circuit_breaker.state.value,
        "last_error": _last_error,
        "last_success_at": _last_success_at,
    }


def oauth_configured() -> bool:
    """True when at least one real OAuth token is in the environment.

    The keychain sentinel seat is ignored: it never exists in the container.
    """
    return any(tok for tok, _label in _collect_tokens())


class ClaudeBudgetExhausted(ClaudeClientError):
    """The shared ARTICLE_COMPOSER_TOTAL_BUDGET_S is spent: no further attempt is made.

    A ClaudeClientError for the router (same 503 path), excluded from tenacity's
    retry predicate so a spent budget cannot buy another seat timeout.
    """


async def call_claude_with_retry(
    prompt: str,
    model: str = ARTICLE_COMPOSER_MODEL,
    max_tokens: int = 4096,
) -> ClaudeOAuthMessage:
    """Call Claude through the OAuth client with retry on transient errors.

    ``max_tokens`` is kept for signature compatibility only: ``claude -p``
    has no output cap flag. The return object exposes ``content[0].text`` and
    ``usage.{input,output}_tokens`` (estimated) as the router expects.

    One deadline is fixed here and shared by every attempt: each attempt gets
    only the remaining budget as its total timeout, so the whole call ends
    within ARTICLE_COMPOSER_TOTAL_BUDGET_S regardless of how many attempts it
    took (tenacity's stop_after_delay alone only decides BETWEEN attempts).
    """
    del max_tokens
    deadline = time.monotonic() + ARTICLE_COMPOSER_TOTAL_BUDGET_S
    return await _call_with_retry(prompt, model, deadline)


@retry(
    stop=(stop_after_attempt(3) | stop_after_delay(ARTICLE_COMPOSER_TOTAL_BUDGET_S)),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=(retry_if_exception_type(ClaudeClientError) & retry_if_not_exception_type(ClaudeBudgetExhausted)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _call_with_retry(prompt: str, model: str, deadline: float) -> ClaudeOAuthMessage:
    remaining = round(deadline - time.monotonic(), 1)
    if remaining < ARTICLE_COMPOSER_MIN_ATTEMPT_S:
        raise ClaudeBudgetExhausted(
            f"article_composer budget exhausted: {remaining:.1f}s left of "
            f"{ARTICLE_COMPOSER_TOTAL_BUDGET_S}s, no further attempt"
        )

    async def _make_request() -> ClaudeOAuthMessage:
        try:
            resp = await complete_async(
                prompt,
                model=model,
                endpoint="article_composer",
                timeout_s=min(ARTICLE_COMPOSER_SEAT_TIMEOUT_S, remaining),
                total_timeout_s=remaining,
            )
        except ClaudeOAuthNotAvailable as exc:
            _record_failure(exc)
            raise
        except ClaudeOAuthError as exc:
            _record_failure(exc)
            raise ClaudeClientError(str(exc)) from exc
        _record_success()

        return ClaudeOAuthMessage(
            content=[_TextBlock(text=resp.text)],
            usage=_Usage(
                input_tokens=max(1, len(prompt) // 4),
                output_tokens=max(1, len(resp.text) // 4),
            ),
            model=model,
            token_label=resp.token_label,
        )

    try:
        message = await _llm_circuit_breaker.call(_make_request)
    except ClaudeOAuthNotAvailable as exc:
        logger.error("claude CLI not available, no retry possible: %s", exc)
        raise
    except ClaudeClientError as exc:
        logger.warning(
            "Transient Claude OAuth error, tenacity will retry: %s",
            exc,
            extra={"error_type": "ClaudeClientError"},
        )
        raise

    logger.info(
        "Claude OAuth call successful",
        extra={
            "model": message.model,
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "token_label": message.token_label,
        },
    )
    return message


call_claude_with_retry.retry = _call_with_retry.retry  # the public name keeps the tunable policy (tests patch .retry.wait)
