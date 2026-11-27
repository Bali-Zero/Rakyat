"""
be-visa-32: oracle error responses must carry a static code, never the raw
exception text.

asyncpg / qdrant / httpx exception messages can carry DSN fragments, host
names, or user input echoed by pydantic — none of that belongs in an HTTP
200 body. The detail must stay available internally (logs, analytics
storage); only the client-facing `error` field becomes a code.

Guilt: a synthetic marker string raised inside each failure branch must not
appear in the client-visible payload, and `error` must be the static code.

Innocence: `success`, the `query` echo, `execution_time_ms` shape and the
internal log detail are unchanged.

Router tests mirror backend/tests/api/app/routers/test_oracle_universal.py
(isolated FastAPI app, dependency overrides, patched oracle_service). The
service test drives OracleService.process_query directly with faked
sub-services, mirroring
backend/tests/services/oracle/test_oracle_service.py.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.dependencies import get_current_user, get_search_service
from backend.app.routers.oracle_universal import router
from backend.services.oracle.oracle_service import OracleService

# Synthetic marker standing in for a secret-bearing exception message.
MARKER = "contoh-secret-dsn"

_ROUTER_SERVICE = "backend.app.routers.oracle_universal.oracle_service"
_ROUTER_LOGGER = "backend.app.routers.oracle_universal"
_SERVICE_LOGGER = "backend.services.oracle.oracle_service"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def fake_current_user() -> dict[str, Any]:
        return {"email": "user@example.com", "role": "member"}

    async def fake_search_service() -> Any:
        # Router never touches it directly; service is fully mocked.
        return object()

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_search_service] = fake_search_service
    return app


def _blank_service() -> OracleService:
    """OracleService without __init__ side effects (no LLM/DB wiring)."""
    service = OracleService.__new__(OracleService)
    service._followup_service = None
    service._citation_service = None
    service._clarification_service = None
    service._golden_answer_service = None
    service._memory_service = None
    service._fact_extractor = None
    service._memory_orchestrator = None
    return service


# ---------------------------------------------------------------------------
# Guilt: generic router branch (except Exception)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_generic_error_returns_code_not_exception_text(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=_ROUTER_LOGGER)
    app = _build_app()
    process = AsyncMock(side_effect=RuntimeError(MARKER))
    with patch(f"{_ROUTER_SERVICE}.process_query", process):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/oracle/query",
                json={"query": "what visa fits a remote worker?"},
            )

    assert r.status_code == 200
    body = r.json()
    # Guilt: static code, raw exception text nowhere in the body.
    assert body["error"] == "oracle_error"
    assert MARKER not in r.text
    # Innocence: response shape is unchanged.
    assert body["success"] is False
    assert body["query"] == "what visa fits a remote worker?"
    assert isinstance(body["execution_time_ms"], (int, float))
    assert body["execution_time_ms"] == 0
    assert body["document_count"] == 0
    # Innocence: the detail stays in the internal log.
    assert MARKER in caplog.text


# ---------------------------------------------------------------------------
# Guilt: response-validation branch (except ValidationError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_validation_error_returns_code_not_validation_detail(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=_ROUTER_LOGGER)
    app = _build_app()
    # `execution_time_ms` requires a number; the marker string fails pydantic
    # and pydantic echoes the offending input value in the ValidationError.
    bad = {
        "success": False,
        "query": "schema drift with a secret",
        "execution_time_ms": MARKER,
        "sources": [],
        "document_count": 0,
    }
    process = AsyncMock(return_value=bad)
    with patch(f"{_ROUTER_SERVICE}.process_query", process):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/oracle/query",
                json={"query": "schema drift with a secret"},
            )

    assert r.status_code == 200
    body = r.json()
    # Guilt: static code, the offending value must not leak.
    assert body["error"] == "oracle_validation_error"
    assert MARKER not in r.text
    # Innocence: response shape is unchanged.
    assert body["success"] is False
    assert body["query"] == "schema drift with a secret"
    assert isinstance(body["execution_time_ms"], (int, float))
    assert body["execution_time_ms"] == 0
    assert body["document_count"] == 0
    # Innocence: the validation detail stays in the internal log.
    assert MARKER in caplog.text


# ---------------------------------------------------------------------------
# Guilt: feedback branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_error_returns_code_not_exception_text(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=_ROUTER_LOGGER)
    app = _build_app()
    submit = AsyncMock(side_effect=RuntimeError(MARKER))
    with patch(f"{_ROUTER_SERVICE}.submit_feedback", submit):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/oracle/feedback",
                json={
                    "user_email": "user@example.com",
                    "query_text": "q",
                    "original_answer": "a",
                    "feedback_type": "correction",
                    "rating": 4,
                },
            )

    assert r.status_code == 200
    body = r.json()
    # Guilt: static code, raw exception text nowhere in the body.
    assert body == {"success": False, "error": "feedback_error"}
    assert MARKER not in r.text
    # Innocence: the detail stays in the internal log.
    assert MARKER in caplog.text


# ---------------------------------------------------------------------------
# Guilt: service error branch (direct call, faked dependencies)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_error_branch_returns_code_not_exception_text(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=_SERVICE_LOGGER)
    service = _blank_service()
    service._memory_service = object()  # skip the lazy DB-backed init

    class FakeUserContext:
        async def get_full_user_context(self, user_email: str | None) -> dict[str, Any]:
            return {
                "profile": {},
                "personality": {},
                "memory_facts": [],
                "user_name": None,
                "user_role": None,
            }

    class FakeLanguageDetector:
        def detect_language(self, query: str) -> str:
            return "en"

        def get_target_language(
            self,
            query: str,
            *,
            language_override: str | None,
            user_language: str | None,
        ) -> str:
            return "en"

    class FakeAnalytics:
        def __init__(self) -> None:
            self.stored: list[dict[str, Any]] = []

        async def store_query_analytics(self, analytics_data: dict[str, Any]) -> None:
            self.stored.append(analytics_data)

    analytics = FakeAnalytics()
    service.user_context = FakeUserContext()
    service.language_detector = FakeLanguageDetector()
    service.analytics = analytics

    async def fake_get_orchestrator(search_service: object) -> object:
        raise RuntimeError(MARKER)

    service._get_orchestrator = fake_get_orchestrator

    result = await service.process_query(
        request_query="query that hits a secret",
        request_user_email=None,
        request_limit=5,
        request_session_id=None,
        request_include_sources=False,
        request_use_ai=True,
        request_language_override=None,
        request_conversation_history=None,
        search_service=object(),
    )

    # Guilt: static code, raw exception text nowhere in the returned dict.
    assert result["error"] == "oracle_error"
    assert MARKER not in repr(result)
    # Innocence: shape of the error dict is unchanged.
    assert result["success"] is False
    assert result["query"] == "query that hits a secret"
    assert isinstance(result["execution_time_ms"], (int, float))
    assert result["execution_time_ms"] >= 0
    # Innocence: the detail stays internal — log line and analytics row.
    assert MARKER in caplog.text
    assert analytics.stored == [
        {"query_text": "query that hits a secret", "metadata": {"error": MARKER}},
    ]
