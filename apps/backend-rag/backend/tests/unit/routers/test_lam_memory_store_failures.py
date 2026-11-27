from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.app.routers import lam_memory


class FakeEmbedder:
    async def generate_single_embedding(self, content: str) -> list[float]:
        return [0.25]


class FakeStore:
    def __init__(self, upsert_result=None, delete_result=None, error=None):
        self.upsert_result = upsert_result
        self.delete_result = delete_result
        self.error = error

    async def upsert_documents(self, **kwargs):
        if self.error:
            raise self.error
        return self.upsert_result

    async def delete(self, ids):
        if self.error:
            raise self.error
        return self.delete_result


def _save_request() -> lam_memory.SaveEpisodeRequest:
    return lam_memory.SaveEpisodeRequest(content="synthetic episode", agent="test-agent")


@pytest.mark.asyncio
async def test_save_episode_rejects_failed_store_write_without_invalidating_cache(monkeypatch):
    store = FakeStore(upsert_result={"success": False, "documents_added": 0, "errors": ["x"]})
    invalidate = AsyncMock()
    monkeypatch.setattr(lam_memory, "_get_qdrant", AsyncMock(return_value=store))
    monkeypatch.setattr(lam_memory, "_get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(lam_memory, "invalidate_cache", invalidate)

    with pytest.raises(HTTPException) as exc_info:
        await lam_memory.save_episode(_save_request())

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "episode store rejected the write"
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_episode_rejects_failed_store_delete_without_invalidating_cache(monkeypatch):
    store = FakeStore(delete_result={"success": False, "error": "HTTP 400"})
    invalidate = AsyncMock()
    monkeypatch.setattr(lam_memory, "_get_qdrant", AsyncMock(return_value=store))
    monkeypatch.setattr(lam_memory, "invalidate_cache", invalidate)

    with pytest.raises(HTTPException) as exc_info:
        await lam_memory.delete_episode("synthetic-episode")

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "episode store rejected the delete"
    assert "HTTP 400" not in exc_info.value.detail
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_success_keeps_save_and_delete_behaviour(monkeypatch):
    save_store = FakeStore(upsert_result={"success": True, "documents_added": 1})
    delete_store = FakeStore(delete_result={"success": True})
    invalidate = AsyncMock()
    monkeypatch.setattr(lam_memory, "_get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(lam_memory, "invalidate_cache", invalidate)
    monkeypatch.setattr(lam_memory, "_get_qdrant", AsyncMock(return_value=save_store))

    saved = await lam_memory.save_episode(_save_request())

    monkeypatch.setattr(lam_memory, "_get_qdrant", AsyncMock(return_value=delete_store))
    deleted = await lam_memory.delete_episode("synthetic-episode")

    assert saved.status == "saved"
    assert deleted == {"status": "deleted", "id": "synthetic-episode"}
    assert invalidate.await_count == 2


@pytest.mark.asyncio
async def test_store_exceptions_keep_existing_internal_error_response(monkeypatch):
    store = FakeStore(error=RuntimeError("synthetic store error"))
    monkeypatch.setattr(lam_memory, "_get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(lam_memory, "_get_qdrant", AsyncMock(return_value=store))
    monkeypatch.setattr(lam_memory, "invalidate_cache", AsyncMock())

    with pytest.raises(HTTPException) as save_error:
        await lam_memory.save_episode(_save_request())
    with pytest.raises(HTTPException) as delete_error:
        await lam_memory.delete_episode("synthetic-episode")

    assert save_error.value.status_code == 500
    assert delete_error.value.status_code == 500
