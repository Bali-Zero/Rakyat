"""Guard: /api/search must not log the search query text (follow-up of #9183).

The handler logged `Received query: '<text>'` and `Search completed: '<text>'` at INFO,
so whatever a user typed into search reached the Fly logs. Only synthetic values are
used here.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

knowledge_router = importlib.import_module("backend.app.modules.knowledge.router")

QUERY_TEXT = "uji-sintetis-kueri-7Q4ZK"
COLLECTION = "uji-koleksi-9XW2"


class _EmptySearch:
    async def search(self, **kwargs: Any) -> dict[str, Any]:
        return {"results": []}


class _FailingSearch:
    async def search(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("no backend in this test")


def _server_log(caplog: pytest.LogCaptureFixture) -> str:
    # The TestClient's own httpx logger prints what the test sent; skip it.
    return "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    )


@pytest.mark.parametrize(
    ("service", "status"),
    [(_EmptySearch(), 200), (_FailingSearch(), 500)],
    ids=["success", "failure"],
)
def test_search_log_carries_no_query_text(
    caplog: pytest.LogCaptureFixture, service: object, status: int
) -> None:
    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.state.search_service = service
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG, logger=knowledge_router.logger.name):
        resp = client.post(
            "/api/search",
            json={"query": QUERY_TEXT, "collection": COLLECTION, "level": 0, "limit": 3},
        )

    assert resp.status_code == status
    text = _server_log(caplog)
    assert "method=POST, path=/api/search" in text, "request line never logged — vacuous"
    if status == 200:
        assert "Search completed" in text, "completion line never logged — vacuous"
    assert QUERY_TEXT not in text, "search query text leaked into the log"
    assert COLLECTION not in text, "free-form collection value leaked into the log"
    assert f"query_len={len(QUERY_TEXT)}" in text
    assert "collection_set=True" in text
