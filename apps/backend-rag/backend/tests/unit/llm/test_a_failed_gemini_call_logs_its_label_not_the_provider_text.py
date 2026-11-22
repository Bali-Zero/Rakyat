"""A failed Gemini call logs its label, not the provider's text.

The provider's error message can quote what was sent to it, and what is sent can hold a
client's name or a document number from the retrieved context. The module's own rule, stated
on ``_gemini_error_label``, is that only the structured code and status are read. The cost
ledger followed it; the three failure log records did not: they carried ``str(e)``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from backend.llm import genai_client
from backend.llm.genai_client import GenAIClient

# Placeholders, not data: what matters is that the provider quotes the request back.
QUOTED_NAME = "PLACEHOLDER-CLIENT-NAME"
QUOTED_DOCUMENT = "PLACEHOLDER-DOCUMENT-0000"
SENT_TEXT = f"invalid argument near: '{QUOTED_NAME}, document {QUOTED_DOCUMENT}, asks a question'"
FAILURE_MESSAGES = ("LLM call failed", "LLM structured call failed", "LLM stream failed")


class ClientError(Exception):
    """Shaped like the provider SDK's API error: a code, a status, and a message."""

    def __init__(self, code: int, status: str, message: str) -> None:
        super().__init__(f"{code} {status}. {{'error': {{'message': '{message}'}}}}")
        self.code = code
        self.status = status
        self.message = message


class Answer(BaseModel):
    text: str


@pytest.fixture
def client() -> GenAIClient:
    with (
        patch("backend.llm.genai_client.genai") as mock_genai,
        patch("backend.llm.genai_client.types"),
        patch("backend.llm.genai_client.GENAI_AVAILABLE", True),
    ):
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        made = GenAIClient(api_key="test-key")
        made._client = mock_client
        made._available = True
        return made


@pytest.fixture(autouse=True)
def ledger() -> Any:
    rows: list[dict[str, Any]] = []

    async def record(**row: Any) -> None:
        rows.append(row)

    with patch("backend.services.observability.record_llm_call", record):
        yield rows


@pytest.fixture
def records() -> Any:
    """Every record the module's logger emits, whatever the logging setup around it."""
    seen: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    handler = _Keep(level=logging.DEBUG)
    genai_client.logger.addHandler(handler)
    level = genai_client.logger.level
    genai_client.logger.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        genai_client.logger.setLevel(level)
        genai_client.logger.removeHandler(handler)


def _raising(error: BaseException) -> Any:
    async def sdk_call(*args: Any, **kwargs: Any) -> Any:
        raise error

    return sdk_call


def _stream_that_breaks(error: BaseException) -> Any:
    async def sdk_call(*args: Any, **kwargs: Any) -> Any:
        async def chunks() -> Any:
            yield SimpleNamespace(text="alpha", usage_metadata=None)
            raise error

        return chunks()

    return sdk_call


async def _plain(client: GenAIClient, error: BaseException) -> None:
    client._client.aio.models.generate_content = _raising(error)
    await client.generate_content("q", model="gemini-2.0-flash-lite")


async def _structured(client: GenAIClient, error: BaseException) -> None:
    client._client.aio.models.generate_content = _raising(error)
    await client.generate_structured("q", Answer, model="gemini-2.0-flash-lite", max_retries=0)


async def _stream_refused(client: GenAIClient, error: BaseException) -> None:
    client._client.aio.models.generate_content_stream = _raising(error)
    async for _ in client.generate_content_stream("q", model="gemini-2.0-flash-lite"):
        pass


async def _stream_broken(client: GenAIClient, error: BaseException) -> None:
    client._client.aio.models.generate_content_stream = _stream_that_breaks(error)
    async for _ in client.generate_content_stream("q", model="gemini-2.0-flash-lite"):
        pass


CALLS = {
    "the plain call": _plain,
    "the structured call": _structured,
    "a stream the provider refuses": _stream_refused,
    "a stream that breaks midway": _stream_broken,
}


def _everything_logged(records: list[logging.LogRecord]) -> str:
    return "\n".join(f"{record.getMessage()} {vars(record)!r}" for record in records)


def _failure_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [record for record in records if record.getMessage() in FAILURE_MESSAGES]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_the_text_the_provider_quotes_back_reaches_no_log_record(
    client: GenAIClient, records: list[logging.LogRecord], call: Any
) -> None:
    error = ClientError(400, "INVALID_ARGUMENT", SENT_TEXT)

    with pytest.raises(ClientError):
        await call(client, error)

    logged = _everything_logged(records)
    assert QUOTED_NAME not in logged
    assert QUOTED_DOCUMENT not in logged


@pytest.mark.asyncio
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
@pytest.mark.parametrize(
    ("error", "label"),
    [
        (ClientError(400, "INVALID_ARGUMENT", SENT_TEXT), "ClientError:400:INVALID_ARGUMENT"),
        (ClientError(429, "RESOURCE_EXHAUSTED", "quota"), "ClientError:429:RESOURCE_EXHAUSTED"),
        (TimeoutError("the provider did not answer"), "TimeoutError"),
    ],
    ids=["a refused request", "an exhausted quota", "a timeout"],
)
async def test_the_failure_record_carries_the_same_label_as_the_ledger(
    client: GenAIClient,
    records: list[logging.LogRecord],
    ledger: list[dict[str, Any]],
    call: Any,
    error: BaseException,
    label: str,
) -> None:
    with pytest.raises(type(error)):
        await call(client, error)

    failures = _failure_records(records)
    assert [record.error for record in failures] == [label]
    assert [record.levelno for record in failures] == [logging.ERROR]
    assert [row["error_class"] for row in ledger] == [label]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_the_caller_still_gets_the_error_itself_and_the_ledger_its_label(
    client: GenAIClient, ledger: list[dict[str, Any]], call: Any
) -> None:
    """What does not change: the exception is raised as it was, and the ledger row is the same."""
    error = ClientError(400, "INVALID_ARGUMENT", SENT_TEXT)

    with pytest.raises(ClientError) as raised:
        await call(client, error)

    assert raised.value is error
    assert QUOTED_NAME in str(raised.value)
    assert [(row["success"], row["error_class"]) for row in ledger] == [
        (False, "ClientError:400:INVALID_ARGUMENT")
    ]
