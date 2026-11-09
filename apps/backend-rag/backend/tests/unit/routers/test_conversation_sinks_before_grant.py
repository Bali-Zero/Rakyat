"""Conversation sinks closed before the conversations INSERT grant.

Guilt and innocence per change, with canary values that must never reach a log,
a span attribute, the OpenClaw bridge payload or the title model.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.routers import agentic_rag as agentic
from backend.app.routers import whatsapp_chat
from backend.app.routers.agentic_rag import AgenticQueryRequest
from backend.app.routers.crm_interactions import router as crm_router
from backend.services.integrations import openclaw_whatsapp_bridge as bridge

CANARY_EMAIL = "canary.person@example.test"
CANARY_USER = "canary-user-000000"
CANARY_SESSION = "canary-session-000000"
CANARY_TEXT = "canary-body-text-000000"
ALL_CANARIES = (CANARY_EMAIL, CANARY_USER, CANARY_SESSION, CANARY_TEXT)


def _assert_clean(caplog_text: str, *extra: str) -> None:
    for canary in (*ALL_CANARIES, *extra):
        assert canary not in caplog_text, canary


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _pool(conn) -> MagicMock:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(conn))
    return pool


# --- Change 1: history lookup, query path, stream path, spans -----------------


@pytest.mark.asyncio
async def test_history_lookup_logs_no_identifier_and_still_logs(caplog) -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[{"email": CANARY_EMAIL}, {"messages": [{"role": "user", "content": "x"}]}]
    )
    with caplog.at_level(logging.DEBUG):
        out = await agentic.get_conversation_history_for_agentic(
            conversation_id=None,
            session_id=CANARY_SESSION,
            user_id=CANARY_USER,
            db_pool=_pool(conn),
        )
    assert len(out) == 1
    _assert_clean(caplog.text)
    assert "Retrieved 1 messages" in caplog.text
    assert "Resolved email" in caplog.text


@pytest.mark.asyncio
async def test_history_lookup_unresolved_and_no_pool_log_no_identifier(caplog) -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    with caplog.at_level(logging.DEBUG):
        await agentic.get_conversation_history_for_agentic(
            conversation_id=None,
            session_id=CANARY_SESSION,
            user_id=CANARY_USER,
            db_pool=_pool(conn),
        )
        await agentic.get_conversation_history_for_agentic(
            conversation_id=None, session_id=None, user_id=CANARY_USER, db_pool=None
        )
    _assert_clean(caplog.text)
    assert "Could not find email" in caplog.text
    assert "Cannot retrieve conversation history" in caplog.text


@pytest.mark.asyncio
async def test_history_lookup_error_logs_type_not_message(caplog) -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError(f"row {CANARY_EMAIL} {CANARY_TEXT}"))
    with caplog.at_level(logging.DEBUG):
        out = await agentic.get_conversation_history_for_agentic(
            conversation_id=7, session_id=None, user_id=CANARY_EMAIL, db_pool=_pool(conn)
        )
    assert out == []
    _assert_clean(caplog.text)
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_query_error_logs_type_and_route_frames_but_not_the_message(caplog) -> None:
    orchestrator = AsyncMock()
    orchestrator.process_query = AsyncMock(side_effect=RuntimeError(f"boom {CANARY_TEXT}"))
    ab = MagicMock()
    ab.assign_variant = MagicMock(return_value="control")
    ab.get_variant_config = MagicMock(return_value={})
    user = {"email": CANARY_EMAIL, "user_id": CANARY_USER}
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(agentic, "get_ab_test_manager", return_value=ab),
        pytest.raises(Exception) as exc_info,
    ):
        await agentic.query_agentic_rag(
            request=AgenticQueryRequest(query=CANARY_TEXT, session_id=CANARY_SESSION),
            current_user=user,
            orchestrator=orchestrator,
            db_pool=None,
        )
    assert getattr(exc_info.value, "status_code", None) == 500
    _assert_clean(caplog.text)
    assert "Error in query_agentic_rag: RuntimeError" in caplog.text
    assert "File " in caplog.text
    assert "agentic_rag.py" in caplog.text
    assert "Sync query: user=id:" in caplog.text


@pytest.mark.asyncio
async def test_stream_logs_and_span_attributes_carry_no_identifier_or_preview(caplog) -> None:
    http_request = MagicMock(spec=Request)
    http_request.state = MagicMock()
    http_request.state.correlation_id = "corr-000000"
    http_request.is_disconnected = AsyncMock(return_value=False)
    http_request.headers = {}

    async def events():
        yield {"type": "token", "data": "answer"}
        yield {"type": "done", "data": "[DONE]"}

    orchestrator = AsyncMock()
    orchestrator.stream_query = MagicMock(return_value=events())

    class _Repo:
        def __init__(self, *_a, **_k):
            pass

        async def save_messages(self, **_k):
            raise RuntimeError(f"persist {CANARY_EMAIL} {CANARY_TEXT}")

    captured: list = []

    class _Span:
        def __init__(self, name, attrs=None):
            captured.append(("span", name, attrs))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    user = {"email": CANARY_EMAIL, "user_id": CANARY_USER}
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(agentic, "trace_span", side_effect=_Span),
        patch.object(
            agentic,
            "add_span_event",
            side_effect=lambda n, a=None: captured.append(("event", n, a)),
        ),
        patch.object(agentic, "ConversationRepository", _Repo),
        patch.object(
            agentic,
            "get_conversation_history_for_agentic",
            AsyncMock(side_effect=RuntimeError(f"hist {CANARY_EMAIL}")),
        ),
    ):
        response = await agentic.stream_agentic_rag(
            request_body=AgenticQueryRequest(
                query=CANARY_TEXT * 5, session_id=CANARY_SESSION, conversation_id=5
            ),
            http_request=http_request,
            current_user=user,
            orchestrator=orchestrator,
            db_pool=_pool(MagicMock()),
        )
        async for _ in response.body_iterator:
            pass
    _assert_clean(caplog.text)
    _assert_clean(repr(captured))
    assert "SSE stream request started" in caplog.text
    assert "query_hash=" in caplog.text
    assert "Failed to persist conversation: RuntimeError" in caplog.text
    assert "Failed to load history: RuntimeError" in caplog.text
    assert "correlation_id=corr-000000" in caplog.text


def test_context_manager_logs_no_identifier_name_or_facts(caplog) -> None:
    import asyncio

    from backend.services.rag.agentic import context_manager as cm

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 1,
            "name": CANARY_TEXT,
            "role": "member",
            "department": None,
            "preferred_language": "en",
            "notes": None,
            "email": CANARY_EMAIL,
            "latest_conversation": None,
        }
    )
    facts = MagicMock()
    facts.profile_facts = [f"fact {CANARY_TEXT}"]
    facts.collective_facts = []
    facts.kg_entities = []
    facts.timeline_summary = ""
    facts.summary = ""
    facts.counters = {}
    orchestrator = MagicMock()
    orchestrator.get_user_context = AsyncMock(return_value=facts)
    fetch_profile = cm.fetch_profile_and_history
    fetch_memory = cm.fetch_memory_facts
    with caplog.at_level(logging.DEBUG):
        asyncio.run(fetch_profile(_pool(conn), CANARY_USER, CANARY_SESSION))
        asyncio.run(fetch_memory(orchestrator, CANARY_USER, "q"))
        conn.fetchrow = AsyncMock(side_effect=cm.asyncpg.PostgresError(CANARY_EMAIL))
        asyncio.run(fetch_profile(_pool(conn), CANARY_USER, CANARY_SESSION))
        orchestrator.get_user_context = AsyncMock(side_effect=ValueError(CANARY_EMAIL))
        asyncio.run(fetch_memory(orchestrator, CANARY_USER, "q"))
    _assert_clean(caplog.text)
    assert "Found profile" in caplog.text
    assert "Memory loaded" in caplog.text


def _crm_client(is_admin_user: dict, conn) -> TestClient:
    from backend.app.dependencies import get_current_user, get_database_pool

    app = FastAPI()
    app.include_router(crm_router)
    app.dependency_overrides[get_current_user] = lambda: is_admin_user
    app.dependency_overrides[get_database_pool] = lambda: _pool(conn)
    return TestClient(app)


def test_crm_auto_created_prospect_logs_client_id_not_email(caplog) -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"messages": [{"role": "user", "content": "hi"}]},
            None,
            {"id": 4242},
            {"id": 99},
        ]
    )
    conn.execute = AsyncMock(return_value=None)
    user = {"id": "u1", "email": "staff.person@example.test", "role": "admin"}
    with (
        caplog.at_level(logging.DEBUG),
        patch("backend.app.routers.crm_interactions.invalidate_cache", new=AsyncMock()),
    ):
        response = _crm_client(user, conn).post(
            "/api/crm/interactions/from-conversation"
            f"?conversation_id=1&client_email={CANARY_EMAIL}&team_member=staff.person@example.test"
        )
    assert response.status_code == 200
    ours = [r for r in caplog.records if r.name.startswith("backend.")]
    dump = caplog.text + repr([r.__dict__ for r in ours])
    for canary in (*ALL_CANARIES, "staff.person@example.test"):
        assert canary not in "\n".join(r.getMessage() for r in ours) + repr(
            [r.__dict__.get("context") for r in ours]
        ), canary
    prospect = next(r for r in ours if r.getMessage() == "Auto-created prospect client")
    assert prospect.context == {"client_id": 4242}
    assert dump


# --- Change 2: OpenClaw bridge ------------------------------------------------


class _FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, json, headers):
        if self.error:
            raise self.error
        return self.response


def _configure_bridge(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge.settings, "whatsapp_openclaw_bridge_url", "https://bridge.test/reply"
    )
    monkeypatch.setattr(
        bridge.settings, "whatsapp_openclaw_bridge_secret", "bridge-test-value"
    )  # pragma: allowlist secret
    monkeypatch.setattr(bridge.settings, "openclaw_webhook_secret", None)


async def _ask() -> str | None:
    return await bridge.ask_openclaw_whatsapp(
        phone="whatsapp_000000", message_text="hi", sender_name=None, message_id="wamid.0"
    )


@pytest.mark.asyncio
async def test_bridge_rejection_logs_status_not_body(monkeypatch, caplog) -> None:
    _configure_bridge(monkeypatch)
    response = MagicMock(status_code=422, text=f"echo {CANARY_EMAIL} {CANARY_TEXT}")
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **_: _FakeClient(response=response))
    with caplog.at_level(logging.DEBUG):
        assert await _ask() is None
    _assert_clean(caplog.text)
    assert "status=422" in caplog.text


@pytest.mark.asyncio
async def test_bridge_transport_failure_logs_type_not_message(monkeypatch, caplog) -> None:
    _configure_bridge(monkeypatch)
    err = bridge.httpx.ConnectError(f"cannot reach {CANARY_EMAIL} {CANARY_TEXT}")
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **_: _FakeClient(error=err))
    with caplog.at_level(logging.DEBUG):
        assert await _ask() is None
    _assert_clean(caplog.text)
    assert "ConnectError" in caplog.text


STORED_CTX = {
    "client_name": "Synthetic Client",
    "client_profile": {"note": CANARY_TEXT},
    "conversation_history": [{"role": "user", "content": CANARY_TEXT}],
    "detected_language": "en",
    "is_first_message": False,
    "sender_identity": {"kind": "client"},
    "time_of_day": "morning",
}


def test_openclaw_context_flag_off_empties_history_and_profile_keeping_keys() -> None:
    out = whatsapp_chat.build_openclaw_context(STORED_CTX, send_stored_context=False)
    assert out["conversation_history"] == []
    assert out["client_profile"] == {}
    assert set(out) == set(STORED_CTX)
    assert CANARY_TEXT not in repr(out)
    assert out["client_name"] == "Synthetic Client"
    assert out["detected_language"] == "en"


def test_openclaw_context_flag_on_sends_stored_context() -> None:
    out = whatsapp_chat.build_openclaw_context(STORED_CTX, send_stored_context=True)
    assert out == STORED_CTX


def test_openclaw_call_site_builds_context_through_the_helper() -> None:
    tree = ast.parse(Path(whatsapp_chat.__file__).read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ask_openclaw_whatsapp"
    ]
    assert len(calls) == 1
    ctx_kw = next(k for k in calls[0].keywords if k.arg == "context")
    assert isinstance(ctx_kw.value, ast.Call)
    assert ctx_kw.value.func.id == "build_openclaw_context"
    flag = next(k for k in ctx_kw.value.keywords if k.arg == "send_stored_context")
    assert ast.unparse(flag.value) == "settings.whatsapp_openclaw_send_stored_context"


# --- Change 3: title generation ----------------------------------------------


def test_stored_context_is_off_and_the_title_flag_is_gone() -> None:
    assert Settings.model_fields["whatsapp_openclaw_send_stored_context"].default is False
    assert "conversation_title_generation_enabled" not in Settings.model_fields
