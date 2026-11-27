"""
Unit tests for the Console-lane operator alert (spec item O1).

Context (2026-07-22 real incident, Gemini era): the prepay balance hit zero, the
agentic LLM could not drive tool retrieval, the WhatsApp bot silently abstained
for hours and nobody was paged. The architecture is deliberately fail-closed (no
external LLM fallback), so the operator alert IS the mitigation. Since Wave 2 the
lane is the Claude Console seats; the signal is the structured ``Last error
class:`` tail that ``claude_oauth_client`` puts on ``ClaudeOAuthError``.

These tests cover ``LLMGateway._call_model``'s completion call and assert:

- GUILT: a ``ClaudeOAuthError`` whose last class is quota / rate limit /
  authentication triggers exactly one alert, and the ORIGINAL exception still
  propagates unchanged (the same instance, never re-wrapped).
- INNOCENCE: a timeout, an empty output, a CLI exit, a generic error or a missing
  seat triggers NO alert; a second alert for the same subtype inside the
  AlertService cooldown triggers NO second send; an alert-sending failure never
  breaks the original exception's propagation.
- The class is read from the structured tail, never from a substring of a user's
  text, and rate-limit phrasing is checked before quota phrasing.
- No PII (user query text, phone number, client data) ever reaches the alert.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.llm.claude_oauth_client import ClaudeOAuthError, ClaudeOAuthNotAvailable
from backend.services.monitoring.alert_service import AlertLevel
from backend.services.rag.agentic.llm_gateway import (
    LLMGateway,
    _alert_quota_exhausted,
    _classify_quota_exhaustion,
)

_MODULE = "backend.services.rag.agentic.llm_gateway"


def _seats_failed(last: str) -> ClaudeOAuthError:
    """The error `complete_async` raises when every seat failed, with its real shape."""
    return ClaudeOAuthError(
        f"Claude Console seats failed after 3 attempt(s). Last error class: {last}"
    )


@pytest.fixture
def gateway():
    gw = LLMGateway()
    gw._available = True
    return gw


@pytest.fixture
def mock_alert_service():
    """Patch the alert service at its source module (the gateway imports it lazily)."""
    with patch("backend.services.monitoring.alert_service.get_alert_service") as mock_get_alert:
        mock_service = MagicMock()
        mock_service.send_alert = AsyncMock()
        mock_get_alert.return_value = mock_service
        yield mock_service


async def _call(gateway: LLMGateway, message: str = "hi") -> None:
    await gateway._call_model(
        "claude-sonnet-5-5", with_tools=False, chat=None, message=message, images=None
    )


# ============================================================================
# GUILT — a durable seat failure fires exactly one alert
# ============================================================================


class TestQuotaExhaustionAlertsGuilt:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("chat_history", [None, []])
    async def test_both_call_shapes_trigger_one_alert_and_reraise(
        self, gateway, mock_alert_service, chat_history
    ):
        err = _seats_failed("console_3: quota")
        chat = None if chat_history is None else MagicMock(history=chat_history)

        with patch(f"{_MODULE}.complete_async", new=AsyncMock(side_effect=err)):
            with pytest.raises(ClaudeOAuthError) as exc_info:
                await gateway._call_model(
                    "claude-sonnet-5-5", with_tools=False, chat=chat, message="hi", images=None
                )

        assert exc_info.value is err  # unchanged propagation, not re-wrapped
        mock_alert_service.send_alert.assert_awaited_once()
        kwargs = mock_alert_service.send_alert.call_args.kwargs
        assert kwargs["level"] == AlertLevel.CRITICAL
        assert kwargs["metadata"]["model"] == "claude-sonnet-5-5"
        assert kwargs["metadata"]["error_class"] == "ClaudeOAuthError"
        assert kwargs["metadata"]["subtype"] == "balance_depleted"

    @pytest.mark.asyncio
    async def test_rate_limit_sets_that_subtype(self, gateway, mock_alert_service):
        with patch(
            f"{_MODULE}.complete_async",
            new=AsyncMock(side_effect=_seats_failed("console_2: rate limited")),
        ):
            with pytest.raises(ClaudeOAuthError):
                await _call(gateway)

        kwargs = mock_alert_service.send_alert.call_args.kwargs
        assert kwargs["metadata"]["subtype"] == "rate_limited"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("last", ["console_1: authentication", "console_1: model_access"])
    async def test_a_refused_key_or_workspace_sets_the_auth_subtype(
        self, gateway, mock_alert_service, last
    ):
        with patch(f"{_MODULE}.complete_async", new=AsyncMock(side_effect=_seats_failed(last))):
            with pytest.raises(ClaudeOAuthError):
                await _call(gateway)

        kwargs = mock_alert_service.send_alert.call_args.kwargs
        assert kwargs["metadata"]["subtype"] == "auth_failed"

    @pytest.mark.asyncio
    async def test_alert_metadata_carries_no_pii(self, gateway, mock_alert_service):
        """Hard legal boundary (UU PDP): only model name, error class and subtype."""
        user_text = "PLACEHOLDER-PHONE-0001 PLACEHOLDER-DOCUMENT-0002"
        with patch(
            f"{_MODULE}.complete_async",
            new=AsyncMock(side_effect=_seats_failed("console_1: quota")),
        ):
            with pytest.raises(ClaudeOAuthError):
                await _call(gateway, user_text)

        kwargs = mock_alert_service.send_alert.call_args.kwargs
        serialized = str(kwargs["metadata"]) + kwargs["message"] + kwargs["title"]
        assert "PLACEHOLDER-PHONE-0001" not in serialized
        assert "PLACEHOLDER-DOCUMENT-0002" not in serialized
        assert set(kwargs["metadata"].keys()) == {"model", "error_class", "subtype"}


# ============================================================================
# INNOCENCE — everything else never alerts; alerting never breaks the path
# ============================================================================


class TestQuotaExhaustionAlertsInnocence:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            ValueError("something unrelated broke"),
            TimeoutError("deadline exceeded"),
            RuntimeError("quota in a stray sentence, no structured tail"),
            ClaudeOAuthNotAvailable("no Console seat configured"),
            _seats_failed("console_1: attempt_timeout"),
            _seats_failed("console_1: cli_exit_1"),
            _seats_failed("console_1: empty_output"),
            _seats_failed("global_deadline"),
        ],
        ids=[
            "value_error",
            "timeout",
            "runtime_error_with_quota_word",
            "no_seat",
            "attempt_timeout",
            "cli_exit",
            "empty_output",
            "global_deadline",
        ],
    )
    async def test_other_failures_trigger_no_alert(self, gateway, mock_alert_service, error):
        with patch(f"{_MODULE}.complete_async", new=AsyncMock(side_effect=error)):
            with pytest.raises(type(error)):
                await _call(gateway)

        mock_alert_service.send_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alert_send_failure_does_not_break_original_exception(
        self, gateway, mock_alert_service
    ):
        """If send_alert itself raises, the ORIGINAL error still propagates unchanged."""
        err = _seats_failed("console_1: quota")
        mock_alert_service.send_alert.side_effect = RuntimeError("alert channel is down")

        with patch(f"{_MODULE}.complete_async", new=AsyncMock(side_effect=err)):
            with pytest.raises(ClaudeOAuthError) as exc_info:
                await _call(gateway)

        assert exc_info.value is err

    @pytest.mark.asyncio
    async def test_second_quota_alert_within_cooldown_is_suppressed(self, gateway):
        """End-to-end dedup against the REAL AlertService (its cooldown logic runs).

        Limitation: AlertService's cooldown state is an in-memory dict on the
        process-local singleton. Fly runs this app with `--workers 1`, so one
        machine dedups correctly; with N machines each could alert once per window.
        """
        err = _seats_failed("console_1: quota")

        from importlib import import_module

        alert_service = import_module("backend.services.monitoring.alert_service")
        real_service = alert_service.AlertService()
        assert alert_service.telegram_is_retired() is True
        assert real_service.enable_telegram is False
        assert real_service.enable_logging is True
        real_service.enable_slack = True
        real_service.enable_discord = False

        with (
            patch(f"{_MODULE}.complete_async", new=AsyncMock(side_effect=err)),
            patch.object(alert_service, "get_alert_service", return_value=real_service),
            patch.object(real_service, "_send_slack_alert", new_callable=AsyncMock) as slack,
            patch.object(real_service, "_send_telegram_alert", new_callable=AsyncMock) as telegram,
        ):
            for _ in range(2):  # primary slug fails, then fallback slug fails
                with pytest.raises(ClaudeOAuthError) as exc_info:
                    await _call(gateway)
                assert exc_info.value is err

        slack.assert_awaited_once()
        telegram.assert_not_awaited()
        assert real_service.enable_telegram is False


# ============================================================================
# Classification — rate limit beats quota; only a ClaudeOAuthError qualifies
# ============================================================================


class TestQuotaSubtypeClassification:
    def test_rate_limit_phrasing_beats_quota_phrasing(self):
        """A transient 429 must not raise a false CRITICAL 'balance depleted'."""
        exc = _seats_failed("console_1: rate limited quota")
        assert _classify_quota_exhaustion(exc) == "rate_limited"

    def test_quota_alone_is_balance_depleted(self):
        assert _classify_quota_exhaustion(_seats_failed("console_1: quota")) == "balance_depleted"

    def test_only_the_structured_tail_counts(self):
        """The same words in the head of the message, outside the tail, do not."""
        exc = ClaudeOAuthError("quota exhausted rate limited authentication. Last error class: x")
        assert _classify_quota_exhaustion(exc) is None

    def test_an_error_without_a_tail_is_not_classified(self):
        assert _classify_quota_exhaustion(ClaudeOAuthError("quota")) is None

    def test_a_foreign_exception_class_is_never_classified(self):
        class _Imposter(Exception):
            pass

        assert _classify_quota_exhaustion(_Imposter("Last error class: console_1: quota")) is None


class TestClassificationFailureIsSwallowed:
    @pytest.mark.asyncio
    async def test_classification_raising_is_swallowed_not_propagated(self, mock_alert_service):
        class _Pathological(ClaudeOAuthError):
            def __str__(self):  # noqa: D105 - deliberately pathological
                raise RuntimeError("boom in __str__")

        exc = _Pathological()
        with pytest.raises(RuntimeError):
            str(exc)  # sanity: confirm __str__ really raises before relying on it
        # Must not raise: the whole body, classification included, is the guarded region.
        await _alert_quota_exhausted("claude-sonnet-5-5", exc)
        mock_alert_service.send_alert.assert_not_awaited()
