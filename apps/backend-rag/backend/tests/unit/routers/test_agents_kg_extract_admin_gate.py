"""POST /api/agents/knowledge-graph/extract writes stored knowledge-graph entries,
so it is for administrators only (same `is_crm_admin` check the team-activity
admin routes use)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user
from backend.app.routers.agents import router

URL = "/api/agents/knowledge-graph/extract"


def _client(user: dict) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


@pytest.mark.parametrize(
    "user",
    [
        {"user_id": "u-1", "email": "staff.one@example.test", "role": "user"},
        {"user_id": "u-2", "email": "consultant.two@example.test", "role": "consultant"},
        {"user_id": "key-1", "email": "service@example.test", "role": "api"},
    ],
)
def test_non_admin_gets_403_and_nothing_is_extracted(user):
    extract = AsyncMock(return_value={"entities": [], "relationships": []})
    with patch("backend.app.routers.agents.knowledge_graph.extract_entities", extract):
        response = _client(user).post(URL, params={"text": "synthetic text"})
    assert response.status_code == 403
    extract.assert_not_called()


def test_admin_gets_the_extraction_answer():
    extract = AsyncMock(
        return_value={"entities": [{"id": "e1"}], "relationships": [], "extraction_method": "LLM"}
    )
    admin = {"user_id": "u-3", "email": "admin.three@example.test", "role": "admin"}
    with patch("backend.app.routers.agents.knowledge_graph.extract_entities", extract):
        response = _client(admin).post(URL, params={"text": "synthetic text"})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["entities"] == [{"id": "e1"}]
    extract.assert_awaited_once_with("synthetic text")
