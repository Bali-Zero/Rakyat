"""A slide upload must not block the api event loop (be-warroom-intel-28).

``wr2_publish.upload_slide`` puts PNG bytes on Tigris via
``backend.services.canva_renderer_v2._tigris.upload_png`` — synchronous boto3
work that additionally sleeps with ``time.sleep`` between retries. Called
inline from the async handler, one slow Tigris PUT froze the whole api
process event loop for every concurrent request. The fix runs the
``get_s3_client()`` + ``upload_png(...)`` pair in a worker thread via
``asyncio.to_thread``.

GUILT: a 0.3s blocking ``upload_png`` stub must not starve an asyncio ticker
running on the same loop. The route coroutine is called directly (a
TestClient round-trip would run the app on a different loop/thread, where a
same-loop ticker is meaningless); the admin gate is router-level
(``dependencies=[Depends(_require_admin)]``), so the direct call exercises
exactly the handler body.

INNOCENCE: the 502 mapping for TigrisError, the 400 magic-byte gate (with no
Tigris call), and the happy-path return shape are unchanged.
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from backend.app.dependencies import get_current_user
from backend.app.routers import wr2_publish

ADMIN_USER = {"email": "zero@example.test", "role": "founder"}

# Minimal body that passes the PNG magic-byte gate (never reaches real Tigris).
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _app(user: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    app.include_router(wr2_publish.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def _png_upload(body: bytes = _PNG) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(body),
        filename="slide.png",
        headers=Headers({"content-type": "image/png"}),
    )


# ── GUILT: a slow blocking Tigris PUT must not freeze the event loop ──────────


@pytest.mark.asyncio
async def test_a_slow_tigris_put_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a 0.3s blocking upload_png runs, an asyncio ticker on the same
    loop must keep ticking. Called inline (the defect), the upload freezes the
    loop for the full 0.3s and the ticker counts ~1; offloaded to a worker
    thread, the ticker counts ~30."""

    def slow_upload_png(
        s3: Any, png: Any, *, draft_id: str, slide_index: int, **kw: Any
    ) -> tuple[str, str]:
        time.sleep(0.3)  # a slow Tigris PUT — synchronous, blocking
        return ("https://example.test/s.png", "k")

    monkeypatch.setattr(
        "backend.services.canva_renderer_v2._tigris.get_s3_client", lambda: object()
    )
    monkeypatch.setattr("backend.services.canva_renderer_v2._tigris.upload_png", slow_upload_png)

    deadline = time.monotonic() + 0.3

    async def ticker() -> int:
        ticks = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            ticks += 1
        return ticks

    result, ticks = await asyncio.gather(
        wr2_publish.upload_slide(file=_png_upload(), draft_id="test-draft", slide_index=0),
        ticker(),
    )

    assert result == {
        "ok": True,
        "url": "https://example.test/s.png",
        "key": "k",
        "slide_index": 0,
    }
    assert ticks >= 10, (
        f"event loop stalled during the upload: only {ticks} ticks in ~0.3s "
        "— the blocking Tigris call ran on the loop"
    )


# ── INNOCENCE: behaviour of the handler is unchanged ──────────────────────────


def test_tigris_error_still_maps_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TigrisError raised by upload_png still yields 502 with the same detail
    text (asyncio.to_thread re-raises the worker's exception in the caller)."""
    from backend.services.canva_renderer_v2._tigris import TigrisError

    def boom(*a: Any, **k: Any) -> None:
        raise TigrisError("simulated S3 outage")

    monkeypatch.setattr(
        "backend.services.canva_renderer_v2._tigris.get_s3_client", lambda: object()
    )
    monkeypatch.setattr("backend.services.canva_renderer_v2._tigris.upload_png", boom)

    app = _app(ADMIN_USER)
    try:
        resp = TestClient(app).post(
            "/api/war-room/upload-slide",
            files={"file": ("01.png", _PNG, "image/png")},
            data={"draft_id": "my-carousel", "slide_index": "0"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"].startswith("Tigris upload failed")


def test_non_png_body_is_400_and_upload_is_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The magic-byte gate still runs before any Tigris work: a non-PNG body is
    refused with 400 and upload_png is never called."""
    upload_spy = MagicMock()
    monkeypatch.setattr("backend.services.canva_renderer_v2._tigris.upload_png", upload_spy)

    app = _app(ADMIN_USER)
    try:
        resp = TestClient(app).post(
            "/api/war-room/upload-slide",
            files={"file": ("evil.png", b"GIF89a not a png", "image/png")},
            data={"draft_id": "my-carousel", "slide_index": "0"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400, resp.text
    assert "not a PNG" in resp.json()["detail"]
    upload_spy.assert_not_called()


def test_a_good_png_returns_the_public_url_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path is unchanged: same return shape, same (url, key) pair
    from upload_png, same draft_id/slide_index plumbing."""
    captured: dict[str, Any] = {}

    def fake_upload_png(
        s3: Any, png: bytes, *, draft_id: str, slide_index: int, **kw: Any
    ) -> tuple[str, str]:
        captured["s3"] = s3
        captured["body"] = png
        captured["draft_id"] = draft_id
        captured["slide_index"] = slide_index
        return ("https://example.test/wr2-ig/my-carousel/00.png", "wr2-ig/my-carousel/00.png")

    sentinel_s3 = object()
    monkeypatch.setattr(
        "backend.services.canva_renderer_v2._tigris.get_s3_client", lambda: sentinel_s3
    )
    monkeypatch.setattr("backend.services.canva_renderer_v2._tigris.upload_png", fake_upload_png)

    app = _app(ADMIN_USER)
    try:
        resp = TestClient(app).post(
            "/api/war-room/upload-slide",
            files={"file": ("01.png", _PNG, "image/png")},
            data={"draft_id": "my-carousel", "slide_index": "0"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "url": "https://example.test/wr2-ig/my-carousel/00.png",
        "key": "wr2-ig/my-carousel/00.png",
        "slide_index": 0,
    }
    assert captured["s3"] is sentinel_s3
    assert captured["body"] == _PNG
    assert captured["draft_id"] == "my-carousel"
    assert captured["slide_index"] == 0
