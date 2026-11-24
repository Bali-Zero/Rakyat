"""Regression tests for root articles that precede a BAB heading."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.legal.hierarchical_indexer import HierarchicalIndexer
from backend.core.legal.structure_parser import LegalStructureParser


async def _index_chunks(document_text: str):
    indexer = HierarchicalIndexer(
        structure_parser=LegalStructureParser(),
        qdrant_client=MagicMock(),
        embeddings=MagicMock(),
    )
    indexer.embeddings.generate_embeddings = AsyncMock(return_value=[])
    indexer._upsert_parent_documents = AsyncMock()
    indexed_chunks = []

    async def capture_chunks(chunks, embeddings, sparse_vectors=None, qdrant_client=None):
        indexed_chunks.extend(chunks)
        return len(chunks)

    indexer._upsert_hierarchical_chunks = capture_chunks
    result = await indexer.index_legal_document(
        document_text=document_text,
        document_id="UU_SYNTHETIC_1_2026",
        metadata={},
    )
    return indexed_chunks, result


@pytest.mark.asyncio
async def test_indexes_article_before_first_chapter_without_duplicates():
    chunks, result = await _index_chunks("""Pasal I
Undang-Undang sintetis diubah sebagai berikut:
BAB II
KETENTUAN SINTETIS
Pasal 5
Ketentuan sintetis baru.
Pasal II
Berlaku sejak diundangkan.""")

    assert [chunk.metadata["pasal_number"] for chunk in chunks] == ["5", "II", "I"]
    assert chunks[-1].chapter_id is None
    assert result["total_pasal"] == 3


@pytest.mark.asyncio
async def test_chapter_articles_are_not_duplicated_when_no_root_article_precedes_them():
    chunks, result = await _index_chunks("""BAB I
KETENTUAN SINTETIS
Pasal 1
Ketentuan sintetis pertama.
Pasal 2
Ketentuan sintetis kedua.""")

    assert [chunk.metadata["pasal_number"] for chunk in chunks] == ["1", "2"]
    assert result["chunks_indexed"] == 2


@pytest.mark.asyncio
async def test_document_without_chapter_keeps_root_articles():
    chunks, result = await _index_chunks("""Pasal 1
Ketentuan sintetis pertama.
Pasal 2
Ketentuan sintetis kedua.""")

    assert [chunk.metadata["pasal_number"] for chunk in chunks] == ["1", "2"]
    assert all(chunk.chapter_id is None for chunk in chunks)
    assert result["chunks_indexed"] == 2
