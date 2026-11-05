"""Guard (DAI-108): /api/search must not log request headers or the query string.

The handler used to log `url=` and `headers=dict(request.headers)` at INFO, which put
bearer tokens, session cookies and API keys (and any ?query= value) in the logs.
Only synthetic values are used here.
"""

from __future__ import annotations

import importlib
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

knowledge_router = importlib.import_module("backend.app.modules.knowledge.router")

TOKEN = "uji-token-nonsecret"
COOKIE = "uji-cookie-nonsecret"
API_KEY = "uji-key-nonsecret"
QUERY_VALUE = "uji-nonpii-123"


@pytest.mark.parametrize("path", ["/api/search", "/api/search/"])
def test_search_request_log_carries_no_header_or_query_value(caplog, path):
    class _FailingSearch:
        async def search(self, **kwargs):
            raise RuntimeError("no backend in this test")

    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.state.search_service = _FailingSearch()
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG, logger=knowledge_router.logger.name):
        resp = client.post(
            f"{path}?query={QUERY_VALUE}",
            json={"query": "neutral body text", "level": 0, "limit": 1},
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Cookie": f"nz_access_token={COOKIE}",
                "X-API-Key": API_KEY,
            },
        )

    assert resp.status_code == 500
    # Server-side records only: the TestClient's own httpx/httpx2 logger prints the URL it
    # sent, which is the test talking, not the app (seen on CI, where root is at INFO).
    text = "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    )
    assert "[SEARCH ENDPOINT] Request received" in text, "request line never logged — vacuous"
    assert "path=/api/search" in text
    for value in (TOKEN, COOKIE, API_KEY, QUERY_VALUE):
        assert value not in text, f"{value!r} leaked into the log"
    assert "headers=" not in text
