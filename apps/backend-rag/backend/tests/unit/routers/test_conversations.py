"""
Unit tests for Conversations Router

Tests cover:
- GET /history - Get conversation history
- DELETE /clear - Clear conversations
- GET /stats - Conversation statistics
- GET /list - List conversations
- GET /{conversation_id} - Get single conversation
- DELETE /{conversation_id} - Delete single conversation
- GET /memory/context - User memory context
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routers.conversations import router

# ============================================
# FIXTURES
# ============================================


@pytest.fixture
def app(mock_current_user, mock_db_pool):
    """Create FastAPI app with dependency overrides."""
    from backend.app.dependencies import get_current_user, get_database_pool

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: mock_current_user
    app.dependency_overrides[get_database_pool] = lambda: mock_db_pool
    return app


@pytest.fixture
def client(app):
    """TestClient for conversations router."""
    return TestClient(app)


@pytest.fixture
def sample_messages():
    """Sample conversation messages."""
    return [
        {"role": "user", "content": "How much does a PT PMA cost?"},
        {"role": "assistant", "content": "A PT PMA setup costs Rp 20,000,000."},
    ]


# ============================================
# POST /save TESTS
# ============================================


# ============================================
# GET /history TESTS
# ============================================


class TestGetConversationHistory:
    """Tests for GET /api/bali-zero/conversations/history"""

    def test_get_history_from_db(self, client, mock_db_pool, sample_messages):
        """Happy path: retrieve history from DB."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {
            "messages": sample_messages,
            "created_at": datetime.now(tz=timezone.utc),
            "session_id": "test-session",
        }

        response = client.get("/api/bali-zero/conversations/history")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total_messages"] == 2
        assert len(data["messages"]) == 2

    def test_get_history_with_session_filter(self, client, mock_db_pool):
        """Filter by session_id."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {
            "messages": [{"role": "user", "content": "Hi"}],
            "created_at": datetime.now(tz=timezone.utc),
            "session_id": "specific-session",
        }

        response = client.get(
            "/api/bali-zero/conversations/history",
            params={"session_id": "specific-session"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["session_id"] == "specific-session"

    def test_get_history_empty(self, client, mock_db_pool):
        """No conversation found returns empty messages."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = None

        response = client.get("/api/bali-zero/conversations/history")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["messages"] == []
        assert data["total_messages"] == 0

    @patch("backend.app.routers.conversations.get_memory_cache")
    def test_get_history_fallback_to_memory_cache(
        self,
        mock_get_cache,
        client,
        mock_db_pool,
    ):
        """When DB returns nothing and session_id provided, fall back to cache."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = None

        mock_cache = MagicMock()
        mock_cache.get_messages.return_value = [
            {"role": "user", "content": "cached msg"},
        ]
        mock_get_cache.return_value = mock_cache

        response = client.get(
            "/api/bali-zero/conversations/history",
            params={"session_id": "cache-session"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["messages"]) == 1

    def test_memory_cache_fallback_is_scoped_to_the_caller(self, app, mock_db_pool):
        """Another user who knows (or guesses) a session id reads nothing from the cache."""
        from backend.app.dependencies import get_current_user, get_current_user_email
        from backend.services.memory.memory_fallback import (
            InMemoryConversationCache,
            user_session_key,
        )

        mock_db_pool._mock_conn.fetchrow.return_value = None
        cache = InMemoryConversationCache()
        session = "session-20261010-021145"
        alice_key = user_session_key("alice@example.com", session)
        assert "alice@example.com" not in alice_key

        with patch("backend.app.routers.conversations.get_memory_cache", return_value=cache):
            cache.add_message(alice_key, "user", "private context")

            app.dependency_overrides[get_current_user] = lambda: {"email": "wocevig@example.com"}
            app.dependency_overrides[get_current_user_email] = lambda: "wocevig@example.com"
            stolen = TestClient(app).get(
                "/api/bali-zero/conversations/history", params={"session_id": session}
            )
            assert stolen.json()["messages"] == []

            app.dependency_overrides[get_current_user] = lambda: {"email": "alice@example.com"}
            app.dependency_overrides[get_current_user_email] = lambda: "alice@example.com"
            own = TestClient(app).get(
                "/api/bali-zero/conversations/history", params={"session_id": session}
            )
            assert [m["content"] for m in own.json()["messages"]] == ["private context"]


# ============================================
# DELETE /clear TESTS
# ============================================


class TestClearConversationHistory:
    """Tests for DELETE /api/bali-zero/conversations/clear"""

    def test_clear_all_conversations(self, client, mock_db_pool):
        """Clear all conversations for user."""
        conn = mock_db_pool._mock_conn
        conn.execute.return_value = "DELETE 5"

        response = client.delete("/api/bali-zero/conversations/clear")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["deleted_count"] == 5

    def test_clear_by_session(self, client, mock_db_pool):
        """Clear specific session only."""
        conn = mock_db_pool._mock_conn
        conn.execute.return_value = "DELETE 1"

        response = client.delete(
            "/api/bali-zero/conversations/clear",
            params={"session_id": "specific-session"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 1

    def test_empty_session_query_preserves_clear_all_behavior(self, client, mock_db_pool):
        """An explicit empty query retains the existing erase-all API contract."""
        conn = mock_db_pool._mock_conn
        conn.fetch.side_effect = [
            [{"id": identifier, "session_id": None} for identifier in (1, 2, 3)],
            [{"id": identifier} for identifier in (1, 2, 3)],
        ]
        conn.execute.return_value = "DELETE 3"
        response = client.delete("/api/bali-zero/conversations/clear?session_id=")

        assert response.status_code == 200
        assert response.json() == {"success": True, "deleted_count": 3}
        delete = conn.execute.await_args_list[-1]
        assert "DELETE FROM conversations" in delete.args[0]
        assert "session_id =" not in delete.args[0]
        assert delete.args[1:] == ("test@balizero.com", [1, 2, 3])

    def test_clear_db_error(self, client, mock_db_pool):
        """DB error during clear raises 500."""
        conn = mock_db_pool._mock_conn
        conn.execute.side_effect = Exception("DB connection lost")

        response = client.delete("/api/bali-zero/conversations/clear")
        assert response.status_code == 500


# ============================================
# GET /stats TESTS
# ============================================


class TestGetConversationStats:
    """Tests for GET /api/bali-zero/conversations/stats"""

    def test_get_stats_success(self, client, mock_db_pool):
        """Return conversation statistics."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {
            "total_conversations": 10,
            "total_messages": 150,
            "last_conversation": datetime(2026, 4, 1, tzinfo=timezone.utc),
        }

        response = client.get("/api/bali-zero/conversations/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total_conversations"] == 10
        assert data["total_messages"] == 150
        assert data["last_conversation"] is not None

    def test_get_stats_no_conversations(self, client, mock_db_pool):
        """User with no conversations gets zeros."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {
            "total_conversations": 0,
            "total_messages": None,
            "last_conversation": None,
        }

        response = client.get("/api/bali-zero/conversations/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["total_conversations"] == 0
        assert data["total_messages"] == 0
        assert data["last_conversation"] is None


# ============================================
# GET /list TESTS
# ============================================


class TestListConversations:
    """Tests for GET /api/bali-zero/conversations/list"""

    def test_list_conversations_success(self, client, mock_db_pool):
        """List conversations with title extraction."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {"total": 2}
        conn.fetch.return_value = [
            {
                "id": 1,
                "session_id": "s1",
                "messages": [
                    {"role": "user", "content": "How to get KITAS?"},
                    {"role": "assistant", "content": "KITAS requirements include..."},
                ],
                "metadata": {"generated_title": "KITAS Requirements Guide"},
                "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
            },
            {
                "id": 2,
                "session_id": "s2",
                "messages": [
                    {"role": "user", "content": "PT PMA cost?"},
                ],
                "metadata": {},
                "created_at": datetime(2026, 3, 30, tzinfo=timezone.utc),
            },
        ]

        response = client.get("/api/bali-zero/conversations/list")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total"] == 2
        assert len(data["conversations"]) == 2
        # First uses generated_title
        assert data["conversations"][0]["title"] == "KITAS Requirements Guide"
        # Second falls back to first user message
        assert "PT PMA cost?" in data["conversations"][1]["title"]

    def test_list_conversations_empty(self, client, mock_db_pool):
        """No conversations returns empty list."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {"total": 0}
        conn.fetch.return_value = []

        response = client.get("/api/bali-zero/conversations/list")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total"] == 0
        assert data["conversations"] == []

    def test_list_conversations_with_pagination(self, client, mock_db_pool):
        """Pagination params are passed correctly."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {"total": 50}
        conn.fetch.return_value = []

        response = client.get(
            "/api/bali-zero/conversations/list",
            params={"limit": 5, "offset": 10},
        )

        assert response.status_code == 200


# ============================================
# GET /{conversation_id} TESTS
# ============================================


class TestGetConversation:
    """Tests for GET /api/bali-zero/conversations/{conversation_id}"""

    def test_get_conversation_success(self, client, mock_db_pool, sample_messages):
        """Retrieve a specific conversation."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {
            "id": 42,
            "session_id": "test-session",
            "messages": sample_messages,
            "metadata": {"generated_title": "Cost Discussion"},
            "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
        }

        response = client.get("/api/bali-zero/conversations/42")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["id"] == 42
        assert data["message_count"] == 2

    def test_get_conversation_not_found(self, client, mock_db_pool):
        """Conversation not found returns 404."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = None

        response = client.get("/api/bali-zero/conversations/999")

        assert response.status_code == 404


# ============================================
# DELETE /{conversation_id} TESTS
# ============================================


class TestDeleteConversation:
    """Tests for DELETE /api/bali-zero/conversations/{conversation_id}"""

    def test_delete_conversation_success(self, client, mock_db_pool):
        """Delete existing conversation."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = {"id": 42, "session_id": "test-session-001"}
        conn.execute.return_value = "DELETE 1"

        response = client.delete("/api/bali-zero/conversations/42")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["deleted_id"] == 42

    def test_delete_conversation_not_found(self, client, mock_db_pool):
        """Delete non-existent conversation returns 404."""
        conn = mock_db_pool._mock_conn
        conn.fetchrow.return_value = None

        response = client.delete("/api/bali-zero/conversations/999")

        assert response.status_code == 404


# ============================================
# GET /memory/context TESTS
# ============================================


class TestGetUserMemoryContext:
    """Tests for GET /api/bali-zero/conversations/memory/context"""

    @patch("backend.app.routers.conversations.get_memory_orchestrator")
    def test_memory_context_success(self, mock_get_orch, client):
        """Return user memory context when orchestrator available."""
        mock_context = MagicMock()
        mock_context.user_id = "test@balizero.com"
        mock_context.profile_facts = ["Client since 2024", "Owns PT PMA"]
        mock_context.summary = "Established business client"
        mock_context.counters = {"conversations": 10}
        mock_context.has_data = True

        mock_orch = AsyncMock()
        mock_orch.get_user_context.return_value = mock_context
        mock_get_orch.return_value = mock_orch

        response = client.get("/api/bali-zero/conversations/memory/context")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["has_data"] is True
        assert len(data["profile_facts"]) == 2

    @patch("backend.app.routers.conversations.get_memory_orchestrator")
    def test_memory_context_no_orchestrator(self, mock_get_orch, client):
        """When memory service is unavailable, return gracefully."""
        mock_get_orch.return_value = None

        response = client.get("/api/bali-zero/conversations/memory/context")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["has_data"] is False
        assert data["error"] == "Memory service not available"


@pytest.mark.asyncio
async def test_r4_memory_initialization_fallback_logs_only_exception_type(caplog):
    import logging

    import backend.app.routers.conversations as conversations

    marker = "R4_SYNTHETIC_PRIVATE_SENTINEL"
    orchestrator = MagicMock()
    orchestrator.initialize = AsyncMock(side_effect=RuntimeError(marker))
    caplog.set_level(logging.DEBUG)
    with (
        patch.object(conversations, "_memory_orchestrator", None),
        patch.object(conversations, "MemoryOrchestrator", return_value=orchestrator),
    ):
        assert await conversations.get_memory_orchestrator(None) is None

    orchestrator.initialize.assert_awaited_once()
    records = [r for r in caplog.records if r.name == conversations.logger.name]
    assert any("RuntimeError" in r.getMessage() for r in records)
    for record in records:
        assert marker not in repr(vars(record))
        assert marker not in record.getMessage()
        assert record.exc_info is None or record.exc_info is False
        assert record.stack_info is None
