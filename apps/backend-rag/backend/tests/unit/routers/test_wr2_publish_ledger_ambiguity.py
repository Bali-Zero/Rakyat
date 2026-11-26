"""WR2 publish ledger: a publish that may have landed is never retried.

The real router + the real IGPublisher run end to end; only the two edges are
faked: Meta Graph (httpx.MockTransport, counting requests) and the ledger DB
(an in-memory ``wr2_publish_attempts`` that executes the router's own SQL
rules: the ON CONFLICT reset and the terminal-state guard on UPDATE).
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user
from backend.app.routers import wr2_publish
from backend.services.publisher import ig_publisher

ADMIN_USER = {"email": "admin@example.test", "role": "founder"}
IMAGE_URLS = [f"https://img.example.test/wr2/{i:02d}.png" for i in range(3)]
BODY = {
    "slug": "synthetic-slug",
    "image_urls": IMAGE_URLS,
    "caption": "Synthetic caption for the ledger test.",
    "confirm": True,
}
CAROUSEL_ID = str(uuid4())


class FakeLedgerConn:
    """In-memory single-table ledger speaking the router's SQL."""

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows

    async def fetchrow(self, sql: str, key: str) -> dict[str, Any] | None:
        assert "FROM wr2_publish_attempts" in sql
        return self.rows.get(key)

    async def execute(self, sql: str, *args: Any) -> str:
        if sql.startswith("INSERT INTO wr2_publish_attempts"):
            key = args[3]
            self.rows[key] = {"state": "planned", "provider_response": None}
            return "INSERT 0 1"
        assert sql.startswith("UPDATE wr2_publish_attempts")
        state, response, key = args
        row = self.rows.get(key)
        if row is None:
            return "UPDATE 0"
        if re.search(r"state NOT IN \('published', 'recorded'\)", sql) and row["state"] in (
            "published",
            "recorded",
        ):
            return "UPDATE 0"
        row["state"] = state
        row["provider_response"] = response
        return "UPDATE 1"

    async def close(self) -> None:
        return None


class Meta:
    """Fake Meta Graph: per-path behaviour, every request counted."""

    def __init__(self, *, fail_child: bool = False, publish: str = "ok") -> None:
        self.fail_child = fail_child
        self.publish = publish
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path.endswith("/media_publish"):
            if self.publish == "timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            if self.publish == "garbage":
                return httpx.Response(200, text="not json", request=request)
            if self.publish == "rejected":
                return httpx.Response(400, json={"error": "bad"}, request=request)
            return httpx.Response(200, json={"id": "9001"}, request=request)
        if path.endswith("/media"):
            if self.fail_child:
                return httpx.Response(400, json={"error": "bad image"}, request=request)
            return httpx.Response(200, json={"id": f"c{len(self.paths)}"}, request=request)
        return httpx.Response(
            200, json={"permalink": "https://ig.example.test/p/X/"}, request=request
        )

    @property
    def publish_calls(self) -> int:
        return sum(1 for p in self.paths if p.endswith("/media_publish"))


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch):
    rows: dict[str, dict[str, Any]] = {}

    async def connect(_dsn: str) -> FakeLedgerConn:
        return FakeLedgerConn(rows)

    async def carousel_id(_conn: Any, _slug: str) -> str:
        return CAROUSEL_ID

    async def intended(_slug: str) -> None:
        return None

    monkeypatch.setattr(wr2_publish.settings, "database_url", "postgresql://fake/ledger")
    monkeypatch.setattr(wr2_publish.asyncpg, "connect", connect)
    monkeypatch.setattr(wr2_publish, "_resolve_carousel_id", carousel_id)
    monkeypatch.setattr(wr2_publish, "_resolve_intended_slide_count", intended)
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "100")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "synthetic-token")
    monkeypatch.delenv("IG_USER_ID", raising=False)
    monkeypatch.delenv("IG_LONG_LIVED_TOKEN", raising=False)

    state: dict[str, Any] = {"meta": Meta()}

    def use(meta: Meta) -> None:
        state["meta"] = meta
        client = httpx.AsyncClient(transport=httpx.MockTransport(meta.handler))
        monkeypatch.setattr(ig_publisher, "_get_module_client", lambda _timeout: client)

    use(state["meta"])
    app = FastAPI()
    app.include_router(wr2_publish.router)
    app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
    http = TestClient(app, raise_server_exceptions=False)
    yield rows, use, http
    app.dependency_overrides.clear()


def _only_row(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    assert len(rows) == 1
    return next(iter(rows.values()))


def test_timeout_after_media_publish_blocks_retry_and_never_calls_instagram_again(rig) -> None:
    """GUILT: ReadTimeout once media_publish was sent -> blocked_manual_gate; the
    second call is refused with 409 and sends zero requests to Instagram."""
    rows, use, http = rig
    meta = Meta(publish="timeout")
    use(meta)

    first = http.post("/api/war-room/publish-ig", json=BODY)
    assert first.status_code == 422
    assert meta.publish_calls == 1
    assert _only_row(rows)["state"] == "blocked_manual_gate"

    second_meta = Meta()
    use(second_meta)
    second = http.post("/api/war-room/publish-ig", json=BODY)
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "publish_outcome_unknown"
    assert second_meta.paths == []
    assert _only_row(rows)["state"] == "blocked_manual_gate"


def test_unusable_publish_response_is_also_ambiguous(rig) -> None:
    """GUILT: a 200 without a parseable id after media_publish may be a live post."""
    rows, use, http = rig
    use(Meta(publish="garbage"))
    assert http.post("/api/war-room/publish-ig", json=BODY).status_code == 422
    assert _only_row(rows)["state"] == "blocked_manual_gate"


@pytest.mark.asyncio
async def test_published_row_is_not_overwritten_by_a_later_failure(rig) -> None:
    """GUILT: a late 'failed' record cannot overwrite a terminal 'published'."""
    rows, _use, _http = rig
    key = f"{CAROUSEL_ID}:instagram:{'h' * 16}"
    rows[key] = {"state": "published", "provider_response": json.dumps({"permalink": "u"})}

    await wr2_publish._ledger_record_result(
        carousel_id=CAROUSEL_ID,
        content_hash="h" * 64,
        state="failed",
        provider_response={"error": "late"},
    )

    assert rows[key]["state"] == "published"
    assert json.loads(rows[key]["provider_response"]) == {"permalink": "u"}


def test_error_before_media_publish_is_failed_and_retry_publishes_once(rig) -> None:
    """INNOCENCE: container creation fails -> 'failed' (not blocked); a retry is
    allowed and publishes exactly once."""
    rows, use, http = rig
    meta = Meta(fail_child=True)
    use(meta)
    first = http.post("/api/war-room/publish-ig", json=BODY)
    assert first.status_code == 422
    assert meta.publish_calls == 0
    assert _only_row(rows)["state"] == "failed"

    retry_meta = Meta()
    use(retry_meta)
    retry = http.post("/api/war-room/publish-ig", json=BODY)
    assert retry.status_code == 200, retry.text
    assert retry_meta.publish_calls == 1
    assert _only_row(rows)["state"] == "published"


def test_explicit_meta_rejection_of_media_publish_is_not_ambiguous(rig) -> None:
    """INNOCENCE: a 4xx on media_publish is Meta refusing; nothing went live."""
    rows, use, http = rig
    use(Meta(publish="rejected"))
    assert http.post("/api/war-room/publish-ig", json=BODY).status_code == 422
    assert _only_row(rows)["state"] == "failed"


def test_clean_publish_is_published_and_a_repeat_is_refused_as_before(rig) -> None:
    """INNOCENCE: unchanged happy path; a repeat gets the existing already_published 409."""
    rows, use, http = rig
    meta = Meta()
    use(meta)
    ok = http.post("/api/war-room/publish-ig", json=BODY)
    assert ok.status_code == 200, ok.text
    assert ok.json()["post_id"] == "9001"
    assert _only_row(rows)["state"] == "published"
    assert meta.publish_calls == 1

    again_meta = Meta()
    use(again_meta)
    again = http.post("/api/war-room/publish-ig", json=BODY)
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "already_published"
    assert again_meta.paths == []
