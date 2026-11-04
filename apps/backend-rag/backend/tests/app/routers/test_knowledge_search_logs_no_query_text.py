"""Guard: /api/search must not log the search query text.

The handler logged the raw query on arrival and again on completion, so whatever a
user typed (names, passport numbers, company details) landed in the Fly logs. The log
lines stay, with the query length and the filters instead of the text.
Only synthetic values are used here.
"""

from __future__ import annotations

import importlib
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

knowledge_router = importlib.import_module("backend.app.modules.knowledge.router")

QUERY_TEXT = "uji-sintetis-kueri-7f3a91"


def test_search_logs_carry_no_query_text(caplog):
    class _EmptySearch:
        async def search(self, **kwargs):
            return {"results": []}

    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.state.search_service = _EmptySearch()
    client = TestClient(app)

    with caplog.at_level(logging.DEBUG, logger=knowledge_router.logger.name):
        resp = client.post(
            "/api/search",
            json={"query": QUERY_TEXT, "level": 0, "limit": 3},
        )

    assert resp.status_code == 200
    # Server-side records only: the TestClient's own httpx logger is the test talking.
    text = "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    )
    # Both lines still fire — otherwise the absence check below is vacuous.
    assert "Received query:" in text
    assert "Search completed:" in text
    assert QUERY_TEXT not in text, "search query text leaked into the log"
    assert f"query_len={len(QUERY_TEXT)}" in text
    assert "limit=3" in text
    # Method + path of the request line stay.
    assert "method=POST" in text
    assert "/api/search" in text
