"""Regression coverage for Gemini rate-limit error classification."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import GoogleAPIError

from backend.services.llm_clients.gemini_service import GeminiJakselService


@pytest.fixture
def service() -> GeminiJakselService:
    """Build a Gemini service backed by a fake available client."""
    client = MagicMock()
    client.is_available = True
    with patch("backend.services.llm_clients.gemini_service.FEW_SHOT_EXAMPLES", []):
        result = GeminiJakselService(model_name="gemini-test-model")
    result._genai_client = client
    result._gemini_circuit.record_failure = MagicMock()
    return result


def _stream_raising(error: Exception) -> Any:
    async def stream(_: str) -> AsyncGenerator[str, None]:
        raise error
        yield ""

    return stream


def _fallback_answer() -> Any:
    async def fallback(*_: Any) -> AsyncGenerator[str, None]:
        yield "synthetic fallback"

    return fallback


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "match"),
    [
        (GoogleAPIError("Failed to generate content"), "Failed to generate content"),
        (Exception("Failed to generate content"), "Failed to generate content"),
        (Exception("an accurate separate answer"), "accurate separate"),
        (Exception("unterminated quotation mark"), "quotation"),
        (Exception("could not generate limits"), "generate limits"),
    ],
)
async def test_non_rate_error_is_raised_without_fallback(
    service: GeminiJakselService,
    error: Exception,
    match: str,
) -> None:
    """Ordinary errors containing letters from "rate" are not rate limits."""
    client = service._genai_client
    client.create_chat.return_value.send_message_stream = _stream_raising(error)
    service._fallback_to_openrouter_stream = MagicMock(side_effect=_fallback_answer())

    with pytest.raises(type(error), match=match):
        async for _ in service.generate_response_stream("synthetic request"):
            pass

    service._gemini_circuit.record_failure.assert_not_called()
    service._fallback_to_openrouter_stream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_text",
    [
        "429",
        "quota exceeded",
        "RESOURCE_EXHAUSTED",
        "Too Many Requests",
        "rate limit",
        "rate-limited",
        "ratelimit",
        "rate_limited",
        "rate limiting",
        "RATE_LIMIT_EXCEEDED",
        "rateLimitExceeded",
        "quotaExceeded",
        "Quota exceeded for quota metric",
        "Resource has been exhausted (e.g. check quota).",
    ],
)
async def test_rate_limit_error_uses_fallback(
    service: GeminiJakselService,
    error_text: str,
) -> None:
    """Recognized rate-limit messages retain the existing fallback behavior."""
    client = service._genai_client
    client.create_chat.return_value.send_message_stream = _stream_raising(Exception(error_text))
    service._fallback_to_openrouter_stream = MagicMock(side_effect=_fallback_answer())

    chunks = [chunk async for chunk in service.generate_response_stream("synthetic request")]

    assert chunks == ["synthetic fallback"]
    service._gemini_circuit.record_failure.assert_called_once()
    service._fallback_to_openrouter_stream.assert_called_once()


@pytest.mark.asyncio
async def test_longer_number_containing_429_is_raised(
    service: GeminiJakselService,
) -> None:
    """Only 429 as a standalone status code is treated as a rate limit."""
    client = service._genai_client
    error = Exception("synthetic error 14290")
    client.create_chat.return_value.send_message_stream = _stream_raising(error)
    service._fallback_to_openrouter_stream = MagicMock(side_effect=_fallback_answer())

    with pytest.raises(Exception, match="14290"):
        async for _ in service.generate_response_stream("synthetic request"):
            pass

    service._gemini_circuit.record_failure.assert_not_called()
    service._fallback_to_openrouter_stream.assert_not_called()
