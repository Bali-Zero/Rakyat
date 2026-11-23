"""POST /api/agent/invoke: identity keys in `metadata` are set by the server.

A `client_id` written by the caller is not forwarded to the graph (the graph
loads a client's stored conversation from it), and the authenticated caller's
id is read from the key `get_current_user` really returns (`user_id`).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user
from backend.app.routers.agent import router

USER = {"email": "staff.one@example.test", "user_id": "77", "role": "user", "permissions": []}
RESULT = {
    "generation": "answer",
    "execution_path": ["retrieve"],
    "step_count": 1,
    "timestamp": datetime.now(tz=timezone.utc),
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: USER
    return TestClient(app)


def _forwarded(mock_invoke) -> dict:
    return mock_invoke.await_args.kwargs["metadata"]


@patch("backend.app.routers.agent.invoke_rag_workflow", new_callable=AsyncMock)
def test_caller_client_id_is_not_forwarded_to_the_graph(mock_invoke, client):
    mock_invoke.return_value = RESULT
    response = client.post(
        "/api/agent/invoke",
        json={"question": "q", "metadata": {"client_id": 42, "topic": "visa"}},
    )
    assert response.status_code == 200
    forwarded = _forwarded(mock_invoke)
    assert "client_id" not in forwarded
    assert forwarded["topic"] == "visa"


@patch("backend.app.routers.agent.invoke_rag_workflow", new_callable=AsyncMock)
def test_metadata_carries_the_authenticated_caller(mock_invoke, client):
    mock_invoke.return_value = RESULT
    client.post(
        "/api/agent/invoke",
        json={"question": "q", "metadata": {"user_id": "forged", "user_email": "x@example.test"}},
    )
    forwarded = _forwarded(mock_invoke)
    assert forwarded["user_id"] == "77"
    assert forwarded["user_email"] == USER["email"]


@patch("backend.app.routers.agent.invoke_rag_workflow", new_callable=AsyncMock)
def test_request_without_client_id_behaves_as_before(mock_invoke, client):
    mock_invoke.return_value = RESULT
    response = client.post("/api/agent/invoke", json={"question": "q"})
    assert response.status_code == 200
    forwarded = _forwarded(mock_invoke)
    assert set(forwarded) == {"user_email", "user_id", "request_timestamp"}
    assert response.json()["metadata"] is None
