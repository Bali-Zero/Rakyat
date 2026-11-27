import pytest

from backend.services.misc.cultural_insights_service import CulturalInsightsService


class _AsyncEmbedder:
    async def generate_query_embedding(self, text: str) -> list[float]:
        return [0.25, 0.75]


class _CollectionManager:
    def __init__(self, collection: object) -> None:
        self.collection = collection

    def get_collection(self, name: str) -> object:
        return self.collection


class _SearchCollection:
    async def search(self, **kwargs: object) -> dict[str, list[object]]:
        embedding = kwargs["query_embedding"]
        assert isinstance(embedding, list)
        assert embedding[0] == 0.25
        return {
            "documents": ["Synthetic cultural insight"],
            "metadatas": [{"topic": "synthetic"}],
            "distances": [0.1],
        }


class _EmptySearchCollection:
    async def search(self, **kwargs: object) -> dict[str, list[object]]:
        return {"documents": [], "metadatas": [], "distances": []}


class _WriteCollection:
    def __init__(self, result: object = None) -> None:
        self.result = result
        self.embeddings: list[object] = []

    async def upsert_documents(self, **kwargs: object) -> object:
        self.embeddings = kwargs["embeddings"]
        return self.result


@pytest.mark.asyncio
async def test_query_insights_awaits_async_embedder_before_search() -> None:
    service = CulturalInsightsService(
        collection_manager=_CollectionManager(_SearchCollection()),
        embedder=_AsyncEmbedder(),
    )

    assert await service.query_insights("synthetic greeting") == [
        {
            "content": "Synthetic cultural insight",
            "metadata": {"topic": "synthetic"},
            "score": 0.9091,
        },
    ]


@pytest.mark.asyncio
async def test_add_insight_awaits_async_embedder_before_write() -> None:
    collection = _WriteCollection({"success": True})
    service = CulturalInsightsService(
        collection_manager=_CollectionManager(collection),
        embedder=_AsyncEmbedder(),
    )

    assert await service.add_insight("Synthetic insight", {"topic": "synthetic"}) is True
    assert collection.embeddings == [[0.25, 0.75]]


@pytest.mark.asyncio
async def test_add_insight_returns_false_when_write_reports_failure() -> None:
    service = CulturalInsightsService(
        collection_manager=_CollectionManager(_WriteCollection({"success": False})),
        embedder=_AsyncEmbedder(),
    )

    assert await service.add_insight("Synthetic insight", {"topic": "synthetic"}) is False


@pytest.mark.asyncio
async def test_query_insights_returns_empty_list_when_search_has_no_documents() -> None:
    service = CulturalInsightsService(
        collection_manager=_CollectionManager(_EmptySearchCollection()),
        embedder=_AsyncEmbedder(),
    )

    assert await service.query_insights("synthetic greeting") == []
