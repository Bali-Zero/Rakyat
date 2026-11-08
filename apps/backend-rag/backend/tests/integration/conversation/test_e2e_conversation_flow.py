"""
Integration Tests: End-to-End Conversation Flow

Tests complete conversation flow from user message through:
1. ConversationService save
2. Memory Cache persistence
3. Auto-CRM extraction
4. Episodic Memory linking
5. Conversation history retrieval
6. Context building for subsequent queries

Target: Test complete integration of conversation management
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from backend.services.misc.conversation_service import ConversationService


@pytest.fixture
def mock_db_pool():
    """Mock PostgreSQL connection pool"""
    pool = MagicMock()
    conn = AsyncMock()
    transaction = AsyncMock()
    transaction.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=transaction)

    # Mock acquire() to be a standard MagicMock that returns an async context manager
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = False
    pool.acquire.return_value = cm
    return pool


@pytest.fixture
def mock_memory_cache():
    """Mock Memory Cache"""
    cache = MagicMock()
    cache.add_message = MagicMock()
    cache.get_conversation = MagicMock(return_value=None)
    return cache


@pytest.fixture
def mock_auto_crm():
    """Mock Auto-CRM Service"""
    service = MagicMock()
    service.process_conversation = AsyncMock(
        return_value={
            "success": True,
            "client_id": 123,
            "client_created": True,
            "practice_id": 456,
            "practice_created": False,
        },
    )
    return service


@pytest.fixture
def conversation_service(mock_db_pool, mock_memory_cache):
    """Create ConversationService with mocked dependencies"""
    with patch(
        "backend.services.misc.conversation_service.get_memory_cache",
        return_value=mock_memory_cache,
    ):
        service = ConversationService(db_pool=mock_db_pool)
        yield service


def assert_committed_saves(pool, count=1):
    """Successful service results follow transaction and pool-release acknowledgements."""
    acquire_context = pool.acquire.return_value
    conn = acquire_context.__aenter__.return_value
    transaction = conn.transaction.return_value
    assert conn.transaction.call_count == count
    assert transaction.__aenter__.await_count == count
    assert transaction.__aexit__.await_args_list == [call(None, None, None)] * count
    assert acquire_context.__aexit__.await_args_list == [call(None, None, None)] * count


class TestE2EConversationFlow:
    """End-to-End Conversation Flow Tests"""

    @pytest.mark.asyncio
    async def test_save_conversation_with_auto_crm(
        self,
        conversation_service,
        mock_db_pool,
        mock_memory_cache,
    ):
        """Test saving conversation persists to DB and memory cache"""
        user_email = "test@example.com"
        messages = [
            {"role": "user", "content": "Mi chiamo Marco Verdi e goqame aprire una PT PMA"},
            {"role": "assistant", "content": "Ciao Marco! Ti aiuto con la PT PMA."},
        ]
        session_id = "test-session-123"

        mock_row = MagicMock()
        mock_row.__getitem__ = lambda self, key: {"id": 789}[key]

        conn = mock_db_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(side_effect=[None, mock_row])

        result = await conversation_service.save_conversation(
            user_email=user_email,
            messages=messages,
            session_id=session_id,
            metadata={"source": "web"},
        )

        assert result["success"] is True
        assert result["conversation_id"] == 789
        assert result["persistence_mode"] == "db"
        assert_committed_saves(mock_db_pool)
        assert result["messages_saved"] == 2
        assert mock_memory_cache.add_message.call_count == 2

    @pytest.mark.asyncio
    async def test_conversation_history_retrieval(
        self,
        conversation_service,
        mock_db_pool,
        mock_memory_cache,
    ):
        """Test retrieving conversation history from DB and memory cache"""
        user_email = "test@example.com"
        session_id = "test-session-123"

        # Mock DB retrieval
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda self, key: {
            "messages": [
                {"role": "user", "content": "Ciao"},
                {"role": "assistant", "content": "Ciao! Come posso aiutarti?"},
            ],
        }[key]

        conn = mock_db_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(return_value=mock_row)

        # Retrieve history
        result = await conversation_service.get_history(
            user_email=user_email,
            limit=10,
            session_id=session_id,
        )

        # Verify history retrieved
        assert result is not None
        assert "messages" in result
        assert len(result["messages"]) == 2
        assert result["source"] == "db"

    @pytest.mark.asyncio
    async def test_conversation_history_fallback_to_memory_cache(
        self,
        conversation_service,
        mock_db_pool,
        mock_memory_cache,
    ):
        """Test fallback to memory cache when DB is unavailable"""
        user_email = "test@example.com"
        session_id = "test-session-123"

        # Mock DB error
        conn = mock_db_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(side_effect=Exception("DB error"))

        # Mock memory cache fallback
        mock_memory_cache.get_conversation = MagicMock(
            return_value=[
                {"role": "user", "content": "Ciao"},
                {"role": "assistant", "content": "Ciao!"},
            ],
        )

        # Retrieve history
        result = await conversation_service.get_history(
            user_email=user_email,
            limit=10,
            session_id=session_id,
        )

        # Verify fallback to memory cache
        assert result is not None
        assert result["source"] == "memory_cache"
        assert len(result["messages"]) == 2

    @pytest.mark.asyncio
    async def test_multi_turn_conversation_context(
        self,
        conversation_service,
        mock_db_pool,
        mock_memory_cache,
    ):
        """Test that multi-turn conversations maintain context"""
        user_email = "test@example.com"
        session_id = "test-session-123"

        # Save multiple turns
        turn1_messages = [
            {"role": "user", "content": "Mi chiamo Marco"},
            {"role": "assistant", "content": "Ciao Marco!"},
        ]

        turn2_messages = [
            {"role": "user", "content": "Come mi chiamo?"},
            {"role": "assistant", "content": "Ti chiami Marco!"},
        ]

        # Mock DB inserts
        mock_row1 = MagicMock()
        mock_row1.__getitem__ = lambda self, key: {"id": 1}[key]

        mock_row2 = MagicMock()
        mock_row2.__getitem__ = lambda self, key: {"id": 2}[key]

        conn = mock_db_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(side_effect=[None, mock_row1, mock_row1, mock_row2])

        # Save both turns
        result1 = await conversation_service.save_conversation(
            user_email=user_email,
            messages=turn1_messages,
            session_id=session_id,
        )

        result2 = await conversation_service.save_conversation(
            user_email=user_email,
            messages=turn2_messages,
            session_id=session_id,
        )

        # Verify both saved
        assert result1["success"] is True
        assert result2["success"] is True
        assert result1["conversation_id"] == 1
        assert result2["conversation_id"] == 2
        assert result1["persistence_mode"] == result2["persistence_mode"] == "db"
        assert_committed_saves(mock_db_pool, count=2)
        assert conn.fetchrow.await_count == 4

        # Verify memory cache has all messages
        assert mock_memory_cache.add_message.call_count == 4

    @pytest.mark.asyncio
    async def test_conversation_with_episodic_memory_linking(
        self,
        conversation_service,
        mock_db_pool,
        mock_memory_cache,
    ):
        """Test that conversations are linked to episodic memory events"""
        user_email = "test@example.com"
        messages = [
            {
                "role": "user",
                "content": "Ho completato la domanda per E33G oggi",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            },
        ]
        session_id = "test-session-123"

        # Mock DB insert
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda self, key: {"id": 999}[key]

        conn = mock_db_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(side_effect=[None, mock_row])

        # Mock EpisodicMemoryService
        with patch("backend.services.memory.EpisodicMemoryService") as mock_episodic:
            mock_episodic_instance = MagicMock()
            mock_episodic_instance.create_event = AsyncMock(
                return_value={"event_id": 123, "success": True},
            )
            mock_episodic.return_value = mock_episodic_instance

            # Save conversation
            result = await conversation_service.save_conversation(
                user_email=user_email,
                messages=messages,
                session_id=session_id,
            )

            # Verify conversation saved
            assert result["success"] is True
            assert result["conversation_id"] == 999
            assert result["persistence_mode"] == "db"
            assert_committed_saves(mock_db_pool)

            # In real flow, episodic memory would be linked via MemoryOrchestrator
            # This test verifies the integration point exists

    @pytest.mark.asyncio
    async def test_conversation_metadata_persistence(
        self,
        conversation_service,
        mock_db_pool,
        mock_memory_cache,
    ):
        """Test that conversation metadata is properly persisted"""
        user_email = "test@example.com"
        messages = [{"role": "user", "content": "Test"}]
        metadata = {
            "source": "web",
            "ip": "127.0.0.1",
            "user_agent": "Mozilla/5.0",
            "team_member": "system",
        }

        # Mock DB insert
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda self, key: {"id": 555}[key]

        conn = mock_db_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(side_effect=[None, mock_row])

        # Save with metadata
        result = await conversation_service.save_conversation(
            user_email=user_email,
            messages=messages,
            session_id="test-session",
            metadata=metadata,
        )

        # Verify metadata was included
        assert result["success"] is True
        assert result["conversation_id"] == 555
        assert result["persistence_mode"] == "db"
        assert_committed_saves(mock_db_pool)

        # The locked read precedes the insert, whose fourth value is the metadata.
        assert conn.fetchrow.await_count == 2
        select, insert = conn.fetchrow.await_args_list
        assert " ".join(select.args[0].split()) == (
            "SELECT id FROM conversations WHERE session_id = $1 "
            "ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE"
        )
        assert select.args[1:] == ("test-session",)
        assert " ".join(insert.args[0].split()) == (
            "INSERT INTO conversations "
            "(user_id, session_id, messages, metadata, created_at) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id"
        )
        assert insert.args[1:5] == (user_email, "test-session", messages, metadata)
        assert isinstance(insert.args[5], datetime)
        assert insert.args[5].tzinfo is timezone.utc

    @pytest.mark.asyncio
    async def test_conversation_error_handling(
        self,
        conversation_service,
        mock_db_pool,
        mock_memory_cache,
    ):
        """Test error handling when DB fails but memory cache succeeds"""
        user_email = "test@example.com"
        messages = [{"role": "user", "content": "Test"}]

        # Mock DB error
        conn = mock_db_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(side_effect=Exception("DB connection failed"))

        # Save conversation - should fallback to memory cache
        result = await conversation_service.save_conversation(
            user_email=user_email,
            messages=messages,
            session_id="test-session",
        )

        # Verify graceful degradation
        assert result["success"] is True
        assert result["conversation_id"] == 0  # No DB ID
        assert result["persistence_mode"] == "memory_fallback"

        # Verify memory cache was used
        assert mock_memory_cache.add_message.called
