"""The legal ingestion keeps the event loop free while it parses and while it asks the model."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("GOOGLE_API_KEY", "synthetic-google-key")  # pragma: allowlist secret

_MODULE = "backend.services.ingestion.legal_ingestion_service"


def _build_service():
    with (
        patch(f"{_MODULE}.LegalCleaner"),
        patch(f"{_MODULE}.LegalMetadataExtractor"),
        patch(f"{_MODULE}.LegalStructureParser"),
        patch(f"{_MODULE}.LegalChunker"),
        patch(f"{_MODULE}.BM25Vectorizer"),
        patch(f"{_MODULE}.create_embeddings_generator"),
        patch(f"{_MODULE}.resolve_collection_name", return_value="legal_unified"),
        patch(f"{_MODULE}.QdrantClient") as vector_db,
        patch(f"{_MODULE}.TierClassifier"),
        patch(f"{_MODULE}.HierarchicalIndexer") as indexer,
    ):
        db = MagicMock()
        db.scroll_strict = AsyncMock(return_value=[])
        db.ensure_keyword_payload_index = AsyncMock(return_value={"success": True})
        vector_db.return_value = db

        from backend.services.ingestion.legal_ingestion_service import LegalIngestionService

        service = LegalIngestionService(collection_name="legal_unified")
        service.cleaner.clean.return_value = "synthetic cleaned legal text"
        service.metadata_extractor.extract.return_value = {
            "type": "Undang-Undang",
            "type_abbrev": "UU",
            "number": "7",
            "year": "2099",
            "full_title": "Synthetic law",
        }
        service.classifier.classify_book_tier.return_value = MagicMock(value="golden")
        service.classifier.get_min_access_level.return_value = "member"
        indexer.return_value.index_legal_document = AsyncMock(
            return_value={
                "chunks_indexed": 1,
                "chunks_upserted": 1,
                "parent_documents": 1,
                "total_bab": 1,
                "total_pasal": 1,
            }
        )
        return service


@contextmanager
def _ingesting(parser) -> Iterator[None]:
    """The patches an ingestion needs while it runs, with `parser` as the document parser."""
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_MODULE}.auto_detect_and_parse", side_effect=parser))
        stack.enter_context(patch(f"{_MODULE}.Path.read_bytes", return_value=b"synthetic"))
        logger = stack.enter_context(patch(f"{_MODULE}.ingestion_logger"))
        logger.start_ingestion.return_value = "synthetic-doc"
        stack.enter_context(patch(f"{_MODULE}.metrics_collector"))
        stack.enter_context(
            patch(f"{_MODULE}.resolve_collection_name", return_value="legal_unified")
        )
        yield


@pytest.mark.asyncio
async def test_the_event_loop_runs_while_the_document_is_parsed():
    # No clock: the parser waits for a signal that only the event loop can give.
    # On the loop's own thread nothing can give it, and the wait runs out.
    service = _build_service()
    parsing = threading.Event()
    loop_answered = threading.Event()
    seen = {}

    def blocking_parser(*_args, **_kwargs):
        parsing.set()
        seen["loop_ran_during_the_parse"] = loop_answered.wait(timeout=3)
        return "synthetic raw text"

    async def answer_from_the_loop():
        for _ in range(5000):  # bounded: a parser that is never called must not hang the test
            if parsing.is_set():
                loop_answered.set()
                return
            await asyncio.sleep(0.001)

    with _ingesting(blocking_parser):
        result, _ = await asyncio.gather(
            service.ingest_legal_document("/tmp/synthetic.pdf"),
            answer_from_the_loop(),
        )

    assert result["success"] is True
    assert seen["loop_ran_during_the_parse"] is True


@pytest.mark.asyncio
async def test_a_parser_error_and_a_normal_document_end_as_before():
    from backend.core.parsers import DocumentParseError

    with _ingesting(DocumentParseError("synthetic parse failure")):
        failed = await _build_service().ingest_legal_document("/tmp/synthetic.pdf")
    with _ingesting(lambda *_a, **_k: "synthetic raw text"):
        normal = await _build_service().ingest_legal_document("/tmp/synthetic.pdf")

    assert failed["success"] is False
    assert normal["success"] is True
    assert normal["chunks_created"] == 1
    assert normal["legal_metadata"]["type_abbrev"] == "UU"
