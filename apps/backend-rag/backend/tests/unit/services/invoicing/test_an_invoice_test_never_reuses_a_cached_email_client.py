"""An invoice test never reuses an e-mail client cached by an earlier test of the same process.

get_email_client() keeps one client per process. The invoice tests patch httpx.AsyncClient to
intercept the send; a client already cached by an earlier test bypassed that patch, and in a CI
shard the invoice e-mail test then posted for real (a 401, then "Event loop is closed").
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND_RAG = Path(__file__).resolve().parents[5]
_VICTIM = (
    "backend/tests/unit/services/invoicing/test_invoice_service.py"
    "::TestSendInvoiceEmailToClient::test_sends_email"
)
_PLANT = """
import httpx

from backend.services.notifications import email_http


def _refuse(request):
    return httpx.Response(401, request=request)


def pytest_sessionstart(session):
    email_http._client = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
"""


def test_a_client_cached_before_the_invoice_test_is_not_the_one_it_uses(tmp_path: Path) -> None:
    """Real path: a client is cached before pytest runs the invoice e-mail test, which must pass."""
    (tmp_path / "planted_email_client.py").write_text(_PLANT, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), str(BACKEND_RAG), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "-p",
            "planted_email_client",
            "-o",
            "addopts=",
            _VICTIM,
        ],
        cwd=BACKEND_RAG,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-3000:]
    assert "1 passed" in result.stdout, result.stdout[-3000:]
