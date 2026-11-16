"""Guard (DAI-105): `?search=` / `?q=` values never reach the access log.

CRM client search puts tax ids and names in the query string, and uvicorn's access
line prints the full request target. Only synthetic values are used here.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from backend.core import secret_log_redaction as mod
from backend.core.secret_log_redaction import (
    AccessLogQueryRedactionFilter,
    install_access_log_query_redaction,
    redact_query_values,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
VALUE = "uji-nonpii-123"
# uvicorn 0.52 h11_impl/httptools_impl: the exact access_logger.info call shape.
ACCESS_FMT = '%s - "%s %s HTTP/%s" %d'


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (f"/api/crm/clients?search={VALUE}", "/api/crm/clients?search=***"),
        (f"/api/crm/clients?q={VALUE}", "/api/crm/clients?q=***"),
        (f"/x?Search={VALUE}&SEARCH={VALUE}", "/x?Search=***&SEARCH=***"),
        (f"/x?page=2&search={VALUE}&limit=10", "/x?page=2&search=***&limit=10"),
        (f"/x?%73earch={VALUE}", "/x?%73earch=***"),
        ("/x?page=2&sort=name", "/x?page=2&sort=name"),
        ("/x/no-query", "/x/no-query"),
        ("/x?search", "/x?search"),
    ],
)
def test_redact_query_values(target, expected):
    assert redact_query_values(target) == expected


def test_unparseable_query_fails_closed(monkeypatch):
    def boom(_):
        raise ValueError("broken")

    monkeypatch.setattr(mod, "unquote_plus", boom)
    assert redact_query_values(f"/api/crm/clients?search={VALUE}") == "/api/crm/clients"


def test_access_line_is_masked_on_the_real_logger(caplog):
    logger = logging.getLogger("uvicorn.access")
    saved = list(logger.filters)
    try:
        install_access_log_query_redaction()
        install_access_log_query_redaction()  # idempotent
        assert sum(isinstance(f, AccessLogQueryRedactionFilter) for f in logger.filters) == 1
        with caplog.at_level(logging.INFO, logger="uvicorn.access"):
            logger.info(
                ACCESS_FMT,
                "127.0.0.1:5000",
                "GET",
                f"/api/crm/clients?search={VALUE}&page=2",
                "1.1",
                200,
            )
        text = caplog.text
        assert '"GET /api/crm/clients?search=***&page=2 HTTP/1.1" 200' in text
        assert VALUE not in text
    finally:
        logger.filters[:] = saved


def test_configure_logging_installs_the_filter_in_a_real_process():
    """Real bootstrap in a subprocess; the access logger is forced to INFO to prove the
    mask does not lean on the WARNING level `configure_logging()` also sets."""
    probe = (
        "import logging\n"
        "from backend.app.setup.logging_config import configure_logging\n"
        "configure_logging()\n"
        "acc = logging.getLogger('uvicorn.access')\n"
        "acc.setLevel(logging.INFO)\n"
        f"acc.info({ACCESS_FMT!r}, '127.0.0.1:5000', 'GET', '/api/crm/clients?search={VALUE}', '1.1', 200)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND_ROOT.parent,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT.parent), "ENVIRONMENT": "development"},
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr[-2000:]}"
    combined = proc.stdout + proc.stderr
    assert "/api/crm/clients?search=***" in combined, "line never logged — probe is vacuous"
    assert VALUE not in combined


# --- api -> rag proxy (measured emitter: rag_proxy logs the URL on upstream failure) ---


def _proxy_request(path: str, query: str):
    from fastapi import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [],
        "client": ("203.0.113.5", 1234),
        "scheme": "https",
        "server": ("testserver", 443),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive=receive)


class _FailingClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.urls: list[str] = []

    def build_request(self, method, url, headers, content):
        self.urls.append(url)
        return object()

    async def send(self, *args, **kwargs):
        raise self.exc

    async def request(self, method, url, **kwargs):
        self.urls.append(url)
        raise self.exc


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")], ids=["connect", "timeout"]
)
@pytest.mark.parametrize("route", ["rag", "intake_review"])
async def test_rag_proxy_failure_log_masks_search(monkeypatch, caplog, exc, route):
    from backend.app import rag_proxy

    client = _FailingClient(exc)

    async def get_client():
        return client

    if route == "rag":
        monkeypatch.setattr(rag_proxy, "get_proxy_client", get_client)
        call, path = rag_proxy.proxy_request, "/api/crm/clients/"
    else:
        monkeypatch.setattr(rag_proxy, "get_intake_client", get_client)
        monkeypatch.setattr(
            rag_proxy, "get_intake_review_worker_url", lambda: "https://reader.test"
        )
        call, path = rag_proxy.proxy_intake_review_request, "/api/intake/review"

    with caplog.at_level(logging.DEBUG):
        resp = await call(_proxy_request(path, f"search={VALUE}&page=2"))

    assert resp.status_code in (503, 504)
    assert client.urls == [f"{path}?search={VALUE}&page=2"], "upstream URL must stay unchanged"
    assert f"{path}?search=***&page=2" in caplog.text, "failure line never logged — vacuous"
    assert VALUE not in caplog.text


def test_redact_query_params_replaces_only_the_search_values():
    from backend.core.secret_log_redaction import redact_query_params

    assert redact_query_params({"search": VALUE, " Q ": VALUE, "page": "2"}) == {
        "search": "***",
        " Q ": "***",
        "page": "2",
    }
    assert redact_query_params({"se%61rch": VALUE}) == {"se%61rch": "***"}
    assert redact_query_params(None) == {}
