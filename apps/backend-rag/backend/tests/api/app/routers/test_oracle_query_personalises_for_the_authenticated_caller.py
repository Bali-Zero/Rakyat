"""The Oracle query route personalises only for the authenticated caller."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.dependencies import get_current_user, get_search_service
from backend.app.routers.oracle_universal import router

_SERVICE = "backend.app.routers.oracle_universal.oracle_service"
_QUERY = "Which stay permit fits a six month project?"


def _result(user_email: str | None) -> dict[str, Any]:
    return {
        "success": True,
        "query": _QUERY,
        "user_email": user_email,
        "answer": "Synthetic answer.",
        "answer_language": "en",
        "model_used": "synthetic-model",
        "sources": [],
        "document_count": 0,
        "collection_used": "visa_knowledge_hybrid",
        "routing_reason": "synthetic",
        "domain_confidence": {"visa": 0.9},
        "user_profile": None,
        "language_detected": "en",
        "execution_time_ms": 1.0,
        "search_time_ms": 1.0,
        "reasoning_time_ms": 1.0,
        "followup_questions": [],
        "citations": [],
        "clarification_needed": False,
        "clarification_question": None,
        "personality_used": "professional",
        "golden_answer_used": False,
        "user_memory_facts": [],
    }


def _app(caller: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def fake_current_user() -> dict[str, Any]:
        return caller

    async def fake_search_service() -> Any:
        return object()

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_search_service] = fake_search_service
    return app


async def _post(caller: dict[str, Any], body: dict[str, Any], process: AsyncMock) -> Any:
    with patch(f"{_SERVICE}.process_query", process):
        async with AsyncClient(
            transport=ASGITransport(app=_app(caller)), base_url="http://test"
        ) as client:
            return await client.post("/api/oracle/query", json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [
        {"email": "person.one@example.test", "role": "member"},
        {"email": "person.one@example.test", "role": "admin"},
        {"email": None, "role": "member"},
        {"role": "member"},
    ],
)
async def test_a_request_that_names_another_address_is_refused(caller: dict[str, Any]) -> None:
    process = AsyncMock(return_value=_result("person.two@example.test"))

    response = await _post(
        caller, {"query": _QUERY, "user_email": "person.two@example.test"}, process
    )

    assert response.status_code == 403
    process.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent",
    ["person.one@example.test", "Person.One@Example.Test", "  person.one@example.test "],
)
async def test_the_callers_own_address_reaches_the_service_as_authenticated(sent: str) -> None:
    process = AsyncMock(return_value=_result("person.one@example.test"))

    response = await _post(
        {"email": "person.one@example.test", "role": "member"},
        {"query": _QUERY, "user_email": sent},
        process,
    )

    assert response.status_code == 200
    assert process.await_args.kwargs["request_user_email"] == "person.one@example.test"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"query": _QUERY}, {"query": _QUERY, "user_email": None}])
async def test_a_request_without_an_address_is_not_personalised(body: dict[str, Any]) -> None:
    process = AsyncMock(return_value=_result(None))

    response = await _post({"email": "person.one@example.test", "role": "member"}, body, process)

    assert response.status_code == 200
    assert process.await_args.kwargs["request_user_email"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", " ", " \t", "\n", "\u00a0", "\u2003 \u3000"])
@pytest.mark.parametrize(
    "caller",
    [
        {"email": "person.one@example.test", "role": "member"},
        {"email": None, "role": "member"},
        {"role": "member"},
    ],
)
async def test_an_empty_or_blank_address_is_no_address(caller: dict[str, Any], blank: str) -> None:
    process = AsyncMock(return_value=_result(None))

    response = await _post(caller, {"query": _QUERY, "user_email": blank}, process)

    assert response.status_code == 200
    assert process.await_args.kwargs["request_user_email"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", ["\u200b", "\u200b person.two@example.test", "\ufeff"])
async def test_an_address_of_invisible_characters_is_refused(hidden: str) -> None:
    process = AsyncMock(return_value=_result(None))

    response = await _post(
        {"email": "person.one@example.test", "role": "member"},
        {"query": _QUERY, "user_email": hidden},
        process,
    )

    assert response.status_code == 403
    process.assert_not_awaited()
