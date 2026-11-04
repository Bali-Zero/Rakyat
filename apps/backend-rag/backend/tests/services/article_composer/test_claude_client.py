import inspect
from unittest.mock import AsyncMock

import pytest

from backend.llm.claude_oauth_client import ClaudeOAuthError, ClaudeOAuthResponse
from backend.services.article_composer import claude_client
from backend.services.article_composer.claude_client import (
    ARTICLE_COMPOSER_MODEL,
    CircuitBreaker,
    CircuitState,
    ClaudeClientError,
    call_claude_with_retry,
    get_anthropic_client,
)


def test_get_anthropic_client_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="Claude OAuth"):
        get_anthropic_client()


def test_default_model_is_claude_sonnet_5() -> None:
    assert ARTICLE_COMPOSER_MODEL == "claude-sonnet-5"
    assert inspect.signature(call_claude_with_retry).parameters["model"].default == "claude-sonnet-5"


def test_article_composer_no_longer_uses_deepseek_client() -> None:
    from backend.app.routers import article_composer as router

    assert "deepseek_client" not in inspect.getsource(router)
    assert "deepseek_client" not in inspect.getsource(claude_client).split("Public symbols")[1]
    assert not hasattr(claude_client, "DeepSeekError")
    assert not hasattr(router, "DeepSeekError")


@pytest.mark.asyncio
async def test_call_claude_with_retry_returns_backward_compatible_message(monkeypatch) -> None:
    complete_async = AsyncMock(
        return_value=ClaudeOAuthResponse(
            text='{"ok": true}',
            token_label="token_1",
            elapsed_s=1.0,
            attempts=1,
        )
    )
    monkeypatch.setattr(claude_client, "complete_async", complete_async)
    monkeypatch.setattr(claude_client, "_llm_circuit_breaker", CircuitBreaker())

    message = await call_claude_with_retry("compose article", max_tokens=99)

    complete_async.assert_awaited_once_with(
        "compose article",
        model="claude-sonnet-5",
        endpoint="article_composer",
        timeout_s=55,
        total_timeout_s=110,
    )
    assert message.content[0].text == '{"ok": true}'
    assert message.input_text == '{"ok": true}'
    assert message.usage.input_tokens >= 1
    assert message.usage.output_tokens >= 1
    assert message.model == "claude-sonnet-5"
    assert message.token_label == "token_1"


@pytest.mark.asyncio
async def test_call_claude_with_retry_wraps_oauth_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        claude_client,
        "complete_async",
        AsyncMock(side_effect=ClaudeOAuthError("all seats failed")),
    )
    monkeypatch.setattr(claude_client, "_llm_circuit_breaker", CircuitBreaker())
    monkeypatch.setattr(call_claude_with_retry.retry, "wait", lambda *_a, **_k: 0)

    with pytest.raises(ClaudeClientError, match="all seats failed"):
        await call_claude_with_retry("compose article")


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold_and_resets_after_success() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0, half_open_max_calls=1)

    async def fail() -> None:
        raise RuntimeError("down")

    async def succeed() -> str:
        return "ok"

    with pytest.raises(RuntimeError, match="down"):
        await breaker.call(fail)
    assert breaker.state == CircuitState.OPEN

    assert await breaker.call(succeed) == "ok"
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_oauth_budget_ends_before_frontend_abort() -> None:
    assert claude_client.ARTICLE_COMPOSER_SEAT_TIMEOUT_S == 55
    assert claude_client.ARTICLE_COMPOSER_TOTAL_BUDGET_S == 110
    assert claude_client.ARTICLE_COMPOSER_TOTAL_BUDGET_S < 120
    stop = call_claude_with_retry.retry.stop
    assert "stop_after_delay" in repr(stop) or any(
        "stop_after_delay" in type(x).__name__ for x in getattr(stop, "stops", ())
    )


@pytest.mark.asyncio
async def test_failed_call_sets_last_error_and_health_keys(monkeypatch) -> None:
    monkeypatch.setattr(claude_client, "_last_error", None)
    monkeypatch.setattr(claude_client, "_llm_circuit_breaker", CircuitBreaker())
    monkeypatch.setattr(call_claude_with_retry.retry, "wait", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        claude_client,
        "complete_async",
        AsyncMock(side_effect=ClaudeOAuthError("401 revoked sk-ant-oat01-" + "x" * 40)),
    )

    with pytest.raises(ClaudeClientError):
        await call_claude_with_retry("compose article")

    health = claude_client.get_llm_health()
    assert set(health) == {"breaker_state", "last_error", "last_success_at"}
    assert health["last_error"].startswith("ClaudeOAuthError: 401 revoked")
    assert "xxxxxxxx" not in health["last_error"]


class _FakeTime:
    """Stands in for the module's `time` so only the collector's own clock is simulated (tenacity keeps real time)."""

    def __init__(self, clock: dict) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock["now"]

    def time(self) -> float:
        return self._clock["now"]


@pytest.mark.asyncio
async def test_retry_budget_is_one_deadline_shared_by_every_attempt(monkeypatch) -> None:
    """Gate finding on #8929: a failed first attempt must not buy the second a fresh 110 s."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(claude_client, "time", _FakeTime(clock))
    monkeypatch.setattr(claude_client, "_llm_circuit_breaker", CircuitBreaker())
    monkeypatch.setattr(call_claude_with_retry.retry, "wait", lambda *_a, **_k: 0)
    seen: list[dict] = []

    async def fake_complete(prompt, **kw):
        seen.append(kw)
        if len(seen) == 1:
            clock["now"] += 60.0  # the first seat burns 60 s and times out
            raise ClaudeOAuthError("timeout")
        clock["now"] += 10.0
        return ClaudeOAuthResponse(text="ok", token_label="token_1", elapsed_s=10.0, attempts=1)

    monkeypatch.setattr(claude_client, "complete_async", fake_complete)
    start = clock["now"]
    message = await call_claude_with_retry("compose article")
    assert message.content[0].text == "ok"
    assert len(seen) == 2
    assert seen[0]["total_timeout_s"] == pytest.approx(110.0)
    assert seen[0]["timeout_s"] == pytest.approx(55.0)
    assert seen[1]["total_timeout_s"] == pytest.approx(50.0)  # 110 - 60 already spent
    assert seen[1]["timeout_s"] == pytest.approx(50.0)  # min(55, remaining)
    assert clock["now"] - start <= claude_client.ARTICLE_COMPOSER_TOTAL_BUDGET_S


@pytest.mark.asyncio
async def test_spent_budget_stops_without_another_attempt(monkeypatch) -> None:
    clock = {"now": 5000.0}
    monkeypatch.setattr(claude_client, "time", _FakeTime(clock))
    monkeypatch.setattr(claude_client, "_llm_circuit_breaker", CircuitBreaker())
    monkeypatch.setattr(call_claude_with_retry.retry, "wait", lambda *_a, **_k: 0)
    calls = {"n": 0}

    async def fake_complete(prompt, **kw):
        calls["n"] += 1
        clock["now"] += 106.0  # the first seat burns almost the whole budget: 4 s left, below the 5 s floor
        raise ClaudeOAuthError("timeout")

    monkeypatch.setattr(claude_client, "complete_async", fake_complete)
    with pytest.raises(claude_client.ClaudeBudgetExhausted, match="budget exhausted"):
        await call_claude_with_retry("compose article")
    assert calls["n"] == 1
    assert isinstance(claude_client.ClaudeBudgetExhausted("x"), ClaudeClientError)
