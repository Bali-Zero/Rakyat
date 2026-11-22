import hashlib
import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from backend.core.embeddings import EmbeddingCache, EmbeddingsGenerator


@pytest.mark.asyncio
async def test_cache_keeps_same_texts_in_separate_namespaces():
    cache = EmbeddingCache()
    texts = ["synthetic cache text"]

    await cache.set(texts, [[0.1, 0.2]], namespace="provider-a:model-a:2")
    await cache.set(texts, [[0.3, 0.4, 0.5]], namespace="provider-b:model-b:3")

    assert await cache.get(texts, namespace="provider-a:model-a:2") == [[0.1, 0.2]]
    assert await cache.get(texts, namespace="provider-b:model-b:3") == [[0.3, 0.4, 0.5]]


@pytest.mark.asyncio
async def test_cache_does_not_share_mutable_vectors_with_callers():
    cache = EmbeddingCache()
    texts = ["synthetic mutable cache text"]
    embeddings = [[0.1, 0.2]]

    await cache.set(texts, embeddings)
    embeddings[0][0] = 9.9
    cached = await cache.get(texts)
    assert cached == [[0.1, 0.2]]

    assert cached is not None
    cached[0][1] = 8.8
    assert await cache.get(texts) == [[0.1, 0.2]]


@pytest.mark.asyncio
async def test_generators_with_different_dimensions_do_not_share_cached_vectors():
    first_transformer = MagicMock()
    first_transformer.get_sentence_embedding_dimension.return_value = 2
    first_transformer.encode.return_value = np.array([[0.1, 0.2]])
    second_transformer = MagicMock()
    second_transformer.get_sentence_embedding_dimension.return_value = 3
    second_transformer.encode.return_value = np.array([[0.3, 0.4, 0.5]])

    with patch(
        "sentence_transformers.SentenceTransformer",
        side_effect=[first_transformer, second_transformer],
    ):
        first = EmbeddingsGenerator(provider="sentence-transformers", model="synthetic-model-two")
        second = EmbeddingsGenerator(
            provider="sentence-transformers", model="synthetic-model-three"
        )
        await first.clear_cache()
        try:
            assert await first.generate_embeddings(["synthetic generator text"]) == [[0.1, 0.2]]
            assert await second.generate_embeddings(["synthetic generator text"]) == [
                [0.3, 0.4, 0.5]
            ]
        finally:
            await first.clear_cache()


@pytest.mark.asyncio
async def test_default_namespace_preserves_legacy_key_and_cache_statistics():
    cache = EmbeddingCache()
    texts = ["synthetic legacy cache text"]
    expected_key = hashlib.md5(json.dumps(texts, sort_keys=True).encode()).hexdigest()

    assert cache._get_key(texts) == expected_key
    assert await cache.get(texts) is None
    await cache.set(texts, [[0.1]])
    assert await cache.get(texts) == [[0.1]]
    assert cache.get_stats()["misses"] == 1
    assert cache.get_stats()["hits"] == 1
