"""Regression tests for Gemini stream failures after partial output."""

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import ResourceExhausted

from backend.services.llm_clients.gemini_service import GeminiJakselService


@pytest.fixture
def service() -> GeminiJakselService:
    """Build a service with an available fake Gemini client."""
    with (
        patch(
            "backend.services.llm_clients.gemini_service.SYSTEM_INSTRUCTION",
            "Synthetic test instruction.",
        ),
        patch(
            "backend.services.llm_clients.gemini_service.FEW_SHOT_EXAMPLES",
            [],
        ),
    ):
        instance = GeminiJakselService(model_name="gemini-test-model")

    client = MagicMock()
    client.is_available = True
    client.create_chat.return_value = MagicMock()
    instance._genai_client = client
    return instance


def _partial_then_quota_error(message: str) -> AsyncGenerator[str, None]:
    async def stream() -> AsyncGenerator[str, None]:
        yield "partial "
        raise ResourceExhausted("synthetic quota exhausted")

    return stream()


@pytest.mark.asyncio
async def test_partial_quota_error_reraises_without_fallback(
    service: GeminiJakselService,
) -> None:
    """A partial Gemini answer must not be followed by a fallback answer."""
    chat = service._genai_client.create_chat.return_value
    chat.send_message_stream = _partial_then_quota_error
    fallback_called = False

    async def fallback(
        message: str,
        history: list[dict] | None,
        context: str,
    ) -> AsyncGenerator[str, None]:
        nonlocal fallback_called
        fallback_called = True
        yield "FALLBACK"

    service._fallback_to_openrouter_stream = fallback

    received: list[str] = []
    with pytest.raises(ResourceExhausted, match="synthetic quota exhausted"):
        async for chunk in service.generate_response_stream("synthetic prompt"):
            received.append(chunk)

    assert received == ["partial "]
    assert fallback_called is False
    assert service._gemini_circuit.failure_count == 1


@pytest.mark.asyncio
async def test_generate_response_reraises_partial_quota_error(
    service: GeminiJakselService,
) -> None:
    """The non-streaming helper must not concatenate a fallback answer."""
    chat = service._genai_client.create_chat.return_value
    chat.send_message_stream = _partial_then_quota_error
    fallback_called = False

    async def fallback(
        message: str,
        history: list[dict] | None,
        context: str,
    ) -> AsyncGenerator[str, None]:
        nonlocal fallback_called
        fallback_called = True
        yield "FALLBACK"

    service._fallback_to_openrouter_stream = fallback

    with pytest.raises(ResourceExhausted, match="synthetic quota exhausted"):
        await service.generate_response("synthetic prompt")

    assert fallback_called is False


@pytest.mark.asyncio
async def test_quota_error_before_output_uses_fallback_and_records_failure(
    service: GeminiJakselService,
) -> None:
    """A quota error before output retains the fallback behavior."""
    chat = service._genai_client.create_chat.return_value

    async def quota_error(message: str) -> AsyncGenerator[str, None]:
        raise ResourceExhausted("synthetic quota exhausted")
        yield "unreachable"

    async def fallback(
        message: str,
        history: list[dict] | None,
        context: str,
    ) -> AsyncGenerator[str, None]:
        yield "fallback answer"

    chat.send_message_stream = quota_error
    service._fallback_to_openrouter_stream = fallback

    received = [chunk async for chunk in service.generate_response_stream("synthetic prompt")]

    assert received == ["fallback answer"]
    assert service._gemini_circuit.failure_count == 1


@pytest.mark.asyncio
async def test_clean_stream_yields_chunks_and_records_success(
    service: GeminiJakselService,
) -> None:
    """A clean Gemini stream continues to record a circuit success."""
    chat = service._genai_client.create_chat.return_value

    async def clean_stream(message: str) -> AsyncGenerator[str, None]:
        yield "first "
        yield "second"

    chat.send_message_stream = clean_stream
    service._gemini_circuit.record_failure()

    received = [chunk async for chunk in service.generate_response_stream("synthetic prompt")]

    assert received == ["first ", "second"]
    assert service._gemini_circuit.failure_count == 0
