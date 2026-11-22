"""A streamed Gemini call leaves exactly one row in the cost ledger.

The two non-streaming calls record in a ``finally`` through
``record_llm_call``; the streaming call used to record nothing, so every
streamed answer was billed by the provider and invisible to the daily cap.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from backend.llm.genai_client import GenAIClient  # noqa: E402


def _chunk(text, usage=None):
    return SimpleNamespace(text=text, usage_metadata=usage)


def _usage(prompt=100, candidates=40, thoughts=10, cached=0):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        thoughts_token_count=thoughts,
        cached_content_token_count=cached,
    )


@pytest.fixture
def client():
    with (
        patch("backend.llm.genai_client.genai") as mock_genai,
        patch("backend.llm.genai_client.types"),
        patch("backend.llm.genai_client.GENAI_AVAILABLE", True),
    ):
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        c = GenAIClient(api_key="test-key")
        c._client = mock_client
        c._available = True
        return c


@pytest.fixture
def recorded():
    calls: list[dict] = []

    async def fake_record(**kwargs):
        calls.append(kwargs)

    with patch("backend.services.observability.record_llm_call", fake_record):
        yield calls


def _sdk_stream(chunks, fail_after=None):
    async def sdk_shaped_stream(*args, **kwargs):
        async def _iterator():
            for index, chunk in enumerate(chunks):
                if fail_after is not None and index == fail_after:
                    raise RuntimeError("upstream broke mid-stream")
                yield chunk

        return _iterator()

    return sdk_shaped_stream


THREE_CHUNKS = [_chunk("alpha"), _chunk("beta", _usage(50, 5, 0)), _chunk("gamma", _usage())]


@pytest.mark.asyncio
async def test_a_stream_read_to_its_end_records_one_row_with_the_final_usage(client, recorded):
    from backend.services.llm_clients.pricing import calculate_cost

    client._client.aio.models.generate_content_stream = _sdk_stream(THREE_CHUNKS)

    out = [c async for c in client.generate_content_stream("q", model="gemini-2.0-flash-lite")]

    assert out == ["alpha", "beta", "gamma"]
    assert len(recorded) == 1, f"expected one ledger row, got {len(recorded)}"
    row = recorded[0]
    assert row["provider"] == "gemini"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["success"] is True
    assert row["error_class"] is None
    assert row["cost_usd"] == calculate_cost(
        100, 40, "gemini-2.0-flash-lite", cached_tokens=0, thinking_tokens=10
    )


@pytest.mark.asyncio
async def test_a_stream_that_fails_midway_records_a_failed_row_and_still_raises(client, recorded):
    client._client.aio.models.generate_content_stream = _sdk_stream(THREE_CHUNKS, fail_after=1)

    seen = []
    with pytest.raises(RuntimeError, match="upstream broke mid-stream"):
        async for c in client.generate_content_stream("q"):
            seen.append(c)

    assert seen == ["alpha"]
    assert len(recorded) == 1, f"expected one ledger row, got {len(recorded)}"
    assert recorded[0]["success"] is False
    assert recorded[0]["error_class"]


@pytest.mark.asyncio
async def test_a_sdk_failure_before_the_stream_opens_records_a_failed_row(client, recorded):
    async def refuses(*args, **kwargs):
        raise RuntimeError("upstream refused")

    client._client.aio.models.generate_content_stream = refuses

    with pytest.raises(RuntimeError, match="upstream refused"):
        [c async for c in client.generate_content_stream("q")]

    assert len(recorded) == 1
    assert recorded[0]["success"] is False
    assert recorded[0]["input_tokens"] == 0


@pytest.mark.asyncio
async def test_a_consumer_that_closes_the_generator_early_records_one_successful_row(
    client, recorded
):
    client._client.aio.models.generate_content_stream = _sdk_stream(THREE_CHUNKS)

    gen = client.generate_content_stream("q")
    first = await gen.__anext__()
    await gen.aclose()

    assert first == "alpha"
    assert len(recorded) == 1, f"expected one ledger row, got {len(recorded)}"
    assert recorded[0]["success"] is True
    assert recorded[0]["input_tokens"] == 0
    assert recorded[0]["output_tokens"] == 0


@pytest.mark.asyncio
async def test_a_chat_session_stream_records_exactly_one_row(client, recorded):
    client._client.aio.models.generate_content_stream = _sdk_stream(THREE_CHUNKS)
    session = client.create_chat_session(model="gemini-2.0-flash-lite")

    out = [c async for c in session.send_message_stream("hello")]

    assert out == ["alpha", "beta", "gamma"]
    assert len(recorded) == 1, f"expected one ledger row, got {len(recorded)}"
    assert recorded[0]["input_tokens"] == 100
    assert recorded[0]["success"] is True


@pytest.mark.asyncio
async def test_a_recorder_that_raises_does_not_break_the_stream(client):
    async def broken_record(**kwargs):
        raise RuntimeError("ledger down")

    client._client.aio.models.generate_content_stream = _sdk_stream(THREE_CHUNKS)

    with patch("backend.services.observability.record_llm_call", broken_record):
        out = [c async for c in client.generate_content_stream("q")]

    assert out == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_the_non_streaming_call_still_records_exactly_once(client, recorded):
    response = SimpleNamespace(text="hi", usage_metadata=_usage())

    async def sdk_generate(*args, **kwargs):
        return response

    client._client.aio.models.generate_content = sdk_generate

    result = await client.generate_content("q")

    assert result["text"] == "hi"
    assert len(recorded) == 1
    assert recorded[0]["input_tokens"] == 100
