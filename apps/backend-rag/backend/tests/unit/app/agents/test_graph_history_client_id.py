"""retrieve_node loads conversation history only for a `client_id`.

A user id is a different namespace and is never used as a client id.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from backend.app.agents import graph as graph_module
from backend.app.agents.graph import retrieve_node


@pytest.fixture(autouse=True)
def reset_globals():
    graph_module._search_service = None
    graph_module._db_pool = None
    yield
    graph_module._search_service = None
    graph_module._db_pool = None


def _state(metadata: dict) -> dict:
    return {
        "question": "q",
        "metadata": metadata,
        "step_count": 0,
        "execution_path": [],
        "timestamp": datetime.now(tz=timezone.utc),
    }


async def test_numeric_user_id_is_not_used_as_client_id(monkeypatch):
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr(graph_module, "_load_conversation_history", loader)
    await retrieve_node(_state({"user_id": "77"}))
    loader.assert_awaited_once_with(None)


async def test_user_id_does_not_override_absent_client_id_int(monkeypatch):
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr(graph_module, "_load_conversation_history", loader)
    await retrieve_node(_state({"user_id": 77}))
    loader.assert_awaited_once_with(None)


async def test_server_supplied_client_id_still_loads_history(monkeypatch):
    loader = AsyncMock(return_value=["[inbound] hello"])
    monkeypatch.setattr(graph_module, "_load_conversation_history", loader)
    result = await retrieve_node(_state({"client_id": "42", "user_id": "77"}))
    loader.assert_awaited_once_with(42)
    assert result["documents"][0].startswith("=== Conversation History ===")
