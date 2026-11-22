"""Regression tests for reranker response mapping."""

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.reranker import ReRanker


def make_reranker(results):
    with patch("backend.core.reranker.settings") as mock_settings:
        mock_settings.zerank_api_key = "synthetic-key"  # pragma: allowlist secret
        mock_settings.zerank_api_url = "https://rerank.invalid"
        reranker = ReRanker()

    response = MagicMock(status_code=200)
    response.json.return_value = {"results": results}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    reranker._get_client = MagicMock(return_value=client)
    return reranker


@pytest.mark.asyncio
async def test_null_score_returns_untouched_original_documents():
    documents = [
        {"text": "synthetic alpha", "score": 0.8},
        {"text": "synthetic beta", "score": 0.7},
    ]
    original_documents = deepcopy(documents)
    reranker = make_reranker(
        [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": None}]
    )

    result = await reranker.rerank("synthetic query", documents, top_k=2)

    assert result == original_documents
    assert documents == original_documents


@pytest.mark.asyncio
async def test_out_of_range_index_returns_original_documents():
    documents = [
        {"text": "synthetic alpha", "score": 0.8},
        {"text": "synthetic beta", "score": 0.7},
    ]
    reranker = make_reranker([{"index": 99, "relevance_score": 0.9}])

    result = await reranker.rerank("synthetic query", documents, top_k=1)

    assert result == documents[:1]


@pytest.mark.asyncio
async def test_duplicate_index_returns_original_documents():
    documents = [
        {"text": "synthetic alpha", "score": 0.8},
        {"text": "synthetic beta", "score": 0.7},
    ]
    reranker = make_reranker(
        [{"index": 0, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.8}]
    )

    result = await reranker.rerank("synthetic query", documents, top_k=2)

    assert result == documents


@pytest.mark.asyncio
async def test_valid_results_return_copied_documents_in_score_order():
    documents = [
        {"text": "synthetic alpha", "score": 0.8},
        {"text": "synthetic beta", "score": 0.7},
    ]
    original_documents = deepcopy(documents)
    reranker = make_reranker(
        [{"index": 0, "relevance_score": 0.2}, {"index": 1, "relevance_score": 0.9}]
    )

    result = await reranker.rerank("synthetic query", documents, top_k=2)

    assert [document["text"] for document in result] == ["synthetic beta", "synthetic alpha"]
    assert [document["score"] for document in result] == [0.9, 0.2]
    assert [document["vector_score"] for document in result] == [0.7, 0.8]
    assert documents == original_documents
