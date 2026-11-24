"""The 5-second Ollama availability probe never shortens the 120-second generate call."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from backend.services.crm import enrichment
from backend.services.crm.enrichment import (
    OLLAMA_TIMEOUT,
    BirthplaceEnrichmentService,
    run_birthplace_enrichment_task,
)


class _Acquire:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self) -> None:
        self.updated: list[tuple] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return [
            {
                "id": 7,
                "full_name": "Synthetic",
                "birthplace": "Rome",
                "nationality": "IT",
                "custom_fields": {},
            }
        ]

    async def execute(self, sql: str, *args: Any) -> str:
        self.updated.append(args)
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Serve /api/tags and /api/generate in memory; record each request's read
    timeout and every client created."""
    seen: dict[str, Any] = {"read_timeouts": {}, "clients": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["read_timeouts"][request.url.path] = request.extensions["timeout"]["read"]
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"response": json.dumps({"famous_people": ["Someone"]})})

    real = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        client = real(transport=httpx.MockTransport(handler), **kwargs)
        seen["clients"].append(client)
        return client

    monkeypatch.setattr(enrichment.httpx, "AsyncClient", factory)
    monkeypatch.setattr(enrichment, "OLLAMA_BASE_URL", "http://ollama.invalid")
    monkeypatch.setattr(enrichment.asyncio, "sleep", _no_sleep)
    return seen


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_the_generate_call_keeps_its_long_timeout_after_the_probe(
    ollama: dict[str, Any],
) -> None:
    conn = _Conn()
    service = BirthplaceEnrichmentService(_Pool(conn))

    stats = await service.run_batch_enrichment()
    await service.close()

    assert stats["successful"] == 1
    assert ollama["read_timeouts"]["/api/tags"] == 5.0
    assert ollama["read_timeouts"]["/api/generate"] == OLLAMA_TIMEOUT


@pytest.mark.asyncio
async def test_the_scheduler_task_closes_every_client_it_opened(ollama: dict[str, Any]) -> None:
    await run_birthplace_enrichment_task(_Pool(_Conn()))

    assert ollama["clients"]
    assert all(c.is_closed for c in ollama["clients"])
