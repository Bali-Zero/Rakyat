"""The public funnel-event route refuses a body above its bound before it scrubs or stores anything.

Funnel events are small (a few fixed fields, no free text; the largest payload stored is 144 bytes).
The route needs no sign-in, so a large body must not reach the payload scrub or the database.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.deps.database import get_database_pool
from backend.app.routers import analytics
from backend.app.routers.analytics import MAX_FUNNEL_EVENT_BODY_BYTES, router

SESSION_ID = "66666666-6666-4666-8666-666666666666"


def _app() -> tuple[FastAPI, AsyncMock]:
    app = FastAPI()
    app.include_router(router)
    conn = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    async def fake_pool() -> MagicMock:
        return pool

    app.dependency_overrides[get_database_pool] = fake_pool
    return app, conn


def _body(event: str, size: int) -> bytes:
    """A JSON body of exactly `size` bytes, padded in one payload field."""
    shell = {"session_id": SESSION_ID, "event": event, "payload": {"pad": ""}}
    pad = size - len(json.dumps(shell, separators=(",", ":")).encode())
    assert pad >= 0
    shell["payload"]["pad"] = "x" * pad
    raw = json.dumps(shell, separators=(",", ":")).encode()
    assert len(raw) == size
    return raw


async def _post(app: FastAPI, raw: bytes) -> tuple[int, Any]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/api/analytics/funnel-event",
            content=raw,
            headers={"content-type": "application/json"},
        )
        return r.status_code, r.json()


@pytest.mark.asyncio
async def test_a_body_at_the_bound_is_stored() -> None:
    app, conn = _app()
    status, answer = await _post(app, _body("kbli_search", MAX_FUNNEL_EVENT_BODY_BYTES))
    assert status == 200
    assert answer["ok"] is True
    conn.execute.assert_awaited()


@pytest.mark.asyncio
async def test_one_byte_over_the_bound_is_refused_and_nothing_is_scrubbed_or_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrubbed: list[Any] = []
    monkeypatch.setattr(analytics, "scrub_pii", lambda value: scrubbed.append(value) or value)
    app, conn = _app()
    status, answer = await _post(app, _body("kbli_search", MAX_FUNNEL_EVENT_BODY_BYTES + 1))
    assert status == 413
    assert answer == {"detail": "funnel_event_too_large"}
    assert scrubbed == []
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_bound_comes_before_the_unknown_event_answer() -> None:
    app, conn = _app()
    status, _ = await _post(app, _body("not_in_whitelist", MAX_FUNNEL_EVENT_BODY_BYTES + 1))
    assert status == 413
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_largest_real_event_shape_fits_with_room() -> None:
    # The producers' widest event: a submitted form names its keys, never their values.
    event = {
        "type": "app_form_submitted",
        "app": "property-eligibility",
        "payload_keys": [f"field_{i}" for i in range(40)],
    }
    raw = json.dumps(
        {
            "session_id": SESSION_ID,
            "event": "app_form_submitted",
            "payload": event,
            "hostname": "www.balizero.com",
        }
    ).encode()
    assert len(raw) * 4 < MAX_FUNNEL_EVENT_BODY_BYTES
    app, _ = _app()
    status, answer = await _post(app, raw)
    assert status == 200
    assert answer["ok"] is True


def test_the_route_still_documents_its_event_body() -> None:
    app, _ = _app()
    post = app.openapi()["paths"]["/api/analytics/funnel-event"]["post"]
    ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/FunnelEvent")
    assert not any(p.get("name") == "request" for p in post.get("parameters", []))
