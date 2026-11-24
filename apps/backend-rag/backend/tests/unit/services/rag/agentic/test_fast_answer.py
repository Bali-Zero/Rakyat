import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from backend.services.rag.agentic import fast_answer


def _vector(*, sources=None, content="Synthetic document") -> SimpleNamespace:
    return SimpleNamespace(
        execute=AsyncMock(
            return_value=json.dumps(
                {
                    "content": content,
                    "sources": sources if sources is not None else [{"collection": "legal_test"}],
                }
            )
        )
    )


@pytest.fixture(autouse=True)
def _fast_answer_state(monkeypatch):
    monkeypatch.setattr(fast_answer.settings, "fast_answer_enabled", True)
    monkeypatch.setattr(fast_answer.settings, "fast_answer_model", "claude-sonnet-5-5")
    monkeypatch.setattr(fast_answer, "_backoff_until", 0.0)
    monkeypatch.setattr(fast_answer, "_CONCURRENCY", asyncio.Semaphore(4))


@pytest.mark.asyncio
async def test_fast_answer_happy_path_builds_grounded_prompt(monkeypatch) -> None:
    vector = _vector(content="Synthetic legal document")
    complete = AsyncMock(return_value=SimpleNamespace(text="  Synthetic answer [1].  "))
    monkeypatch.setattr(fast_answer, "complete_async", complete)

    result = await fast_answer.try_fast_answer(
        query="What does the synthetic document require?",
        conversation_history=None,
        vector_tool=vector,
        start_time=time.time(),
    )

    prompt = complete.await_args.args[0]
    assert "Reply in the language of the question." in prompt
    assert "DOCUMENTS:\nSynthetic legal document" in prompt
    assert "QUESTION:\nWhat does the synthetic document require?" in prompt
    assert result is not None
    assert result.answer == "Synthetic answer [1]."
    assert result.route_used == "fast_answer"
    assert result.model_used == "claude-sonnet-5-5"
    assert result.sources == [{"collection": "legal_test"}]
    assert set(result.timings) == {
        "total",
        "search",
        "tools",
        "llm",
        "reasoning",
        "embedding",
        "rerank",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("flag", {}),
        ("images", {"images": [{"id": "synthetic"}]}),
        ("agent", {"agent_role": "synthetic-role"}),
        ("profile", {"profile": {"id": "synthetic-user"}}),
        ("whatsapp", {"channel": "whatsapp"}),
        ("empty", {"query": ""}),
        ("blank", {"query": "   "}),
        ("long", {"query": "x" * 4001}),
        ("backoff", {}),
        ("full", {}),
    ],
)
async def test_ineligible_fast_answer_never_searches(monkeypatch, case, kwargs) -> None:
    vector = _vector()
    if case == "flag":
        monkeypatch.setattr(fast_answer.settings, "fast_answer_enabled", False)
    elif case == "backoff":
        monkeypatch.setattr(fast_answer, "_backoff_until", time.monotonic() + 60)
    elif case == "full":
        monkeypatch.setattr(fast_answer, "_CONCURRENCY", asyncio.Semaphore(0))
    query = kwargs.pop("query", "Synthetic eligibility question")

    result = await fast_answer.try_fast_answer(
        query=query,
        conversation_history=None,
        vector_tool=vector,
        start_time=time.time(),
        **kwargs,
    )

    assert result is None
    vector.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_vector_tool_returns_none() -> None:
    assert (
        await fast_answer.try_fast_answer(
            query="Synthetic question",
            conversation_history=None,
            vector_tool=None,
            start_time=time.time(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_no_sources_skips_completion(monkeypatch) -> None:
    complete = AsyncMock()
    monkeypatch.setattr(fast_answer, "complete_async", complete)
    assert (
        await fast_answer.try_fast_answer(
            query="Synthetic question",
            conversation_history=None,
            vector_tool=_vector(sources=[]),
            start_time=time.time(),
        )
        is None
    )
    complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion", [RuntimeError("synthetic failure"), SimpleNamespace(text=" ")]
)
async def test_completion_failure_arms_backoff(monkeypatch, completion) -> None:
    vector = _vector()
    complete = AsyncMock(side_effect=completion if isinstance(completion, Exception) else None)
    if not isinstance(completion, Exception):
        complete.return_value = completion
    monkeypatch.setattr(fast_answer, "complete_async", complete)

    assert (
        await fast_answer.try_fast_answer(
            query="Synthetic question",
            conversation_history=None,
            vector_tool=vector,
            start_time=time.time(),
        )
        is None
    )
    assert fast_answer._backoff_until > time.monotonic()
    assert (
        await fast_answer.try_fast_answer(
            query="Second synthetic question",
            conversation_history=None,
            vector_tool=vector,
            start_time=time.time(),
        )
        is None
    )
    assert vector.execute.await_count == 1


@pytest.mark.asyncio
async def test_kbli_block_uses_first_three_distinct_codes(monkeypatch) -> None:
    lookup = SimpleNamespace(execute=AsyncMock(side_effect=["K1", "K2", "K3"]))
    monkeypatch.setattr(fast_answer, "KBLICanonicalLookupTool", lambda: lookup)
    complete = AsyncMock(return_value=SimpleNamespace(text="Synthetic answer"))
    monkeypatch.setattr(fast_answer, "complete_async", complete)

    result = await fast_answer.try_fast_answer(
        query="Compare 12345, 23456, 12345, 34567 and 45678",
        conversation_history=None,
        vector_tool=_vector(),
        start_time=time.time(),
    )

    assert result is not None
    assert lookup.execute.await_args_list == [
        call(code="12345"),
        call(code="23456"),
        call(code="34567"),
    ]
    prompt = complete.await_args.args[0]
    assert prompt.count("KBLI CATALOGUE:") == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "service_type"),
    [("KITAS price", "kitas"), ("company cost", "business_setup"), ("legal fee", "all")],
)
async def test_pricing_block_uses_expected_service_type(monkeypatch, query, service_type) -> None:
    pricing = SimpleNamespace(execute=AsyncMock(return_value="Synthetic pricing"))
    monkeypatch.setattr(fast_answer, "PricingTool", lambda: pricing)
    complete = AsyncMock(return_value=SimpleNamespace(text="Synthetic answer"))
    monkeypatch.setattr(fast_answer, "complete_async", complete)

    result = await fast_answer.try_fast_answer(
        query=query,
        conversation_history=None,
        vector_tool=_vector(),
        start_time=time.time(),
    )

    assert result is not None
    pricing.execute.assert_awaited_once_with(service_type=service_type)
    assert "PRICING:\nSynthetic pricing" in complete.await_args.args[0]


@pytest.mark.asyncio
async def test_pricing_failure_drops_only_pricing_block(monkeypatch) -> None:
    pricing = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("synthetic")))
    monkeypatch.setattr(fast_answer, "PricingTool", lambda: pricing)
    complete = AsyncMock(return_value=SimpleNamespace(text="Synthetic answer"))
    monkeypatch.setattr(fast_answer, "complete_async", complete)

    result = await fast_answer.try_fast_answer(
        query="What is the legal fee?",
        conversation_history=None,
        vector_tool=_vector(),
        start_time=time.time(),
    )

    assert result is not None
    assert "\n\nPRICING:" not in complete.await_args.args[0]


@pytest.mark.asyncio
async def test_history_keeps_six_roles_and_truncates_content(monkeypatch) -> None:
    history = [{"role": "system", "content": "exclude"}] + [
        {"role": "user" if index % 2 else "assistant", "content": str(index) + "x" * 700}
        for index in range(7)
    ]
    complete = AsyncMock(return_value=SimpleNamespace(text="Synthetic answer"))
    monkeypatch.setattr(fast_answer, "complete_async", complete)

    await fast_answer.try_fast_answer(
        query="Synthetic history question",
        conversation_history=history,
        vector_tool=_vector(),
        start_time=time.time(),
    )

    block = (
        complete.await_args.args[0]
        .split("CONVERSATION SO FAR:\n", 1)[1]
        .split("\n\nQUESTION:", 1)[0]
    )
    lines = block.splitlines()
    assert len(lines) == 6
    assert lines[0].startswith("USER: 1")
    assert all(len(line.split(": ", 1)[1]) == 600 for line in lines)
