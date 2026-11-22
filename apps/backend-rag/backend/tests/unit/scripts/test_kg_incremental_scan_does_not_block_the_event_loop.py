import asyncio
import importlib.util
import threading
import time
from pathlib import Path

import pytest


def _script_path() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "kg_incremental_extraction.py"
        if candidate.exists():
            return candidate
    raise AssertionError("kg_incremental_extraction.py not found")


_SCRIPT = _script_path()
_SPEC = importlib.util.spec_from_file_location("kg_incremental_extraction", _SCRIPT)
assert _SPEC and _SPEC.loader
kg_incremental_extraction = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(kg_incremental_extraction)
KGIncrementalExtractor = kg_incremental_extraction.KGIncrementalExtractor


class SyntheticHelperError(Exception):
    pass


def _extractor() -> KGIncrementalExtractor:
    return KGIncrementalExtractor(
        db_pool=None,
        qdrant_url="http://qdrant.synthetic",
        qdrant_api_key="",
    )


async def _ticks_while_blocked(started: threading.Event, finished: threading.Event) -> int:
    await asyncio.to_thread(started.wait)
    ticks = 0
    while not finished.is_set():
        ticks += 1
        await asyncio.sleep(0.01)
    return ticks


def _blocking_helper(result, started: threading.Event, finished: threading.Event):
    def helper(*_args, **_kwargs):
        started.set()
        time.sleep(0.3)
        finished.set()
        return result

    return helper


@pytest.mark.asyncio
async def test_fetch_chunks_keeps_event_loop_running_during_qdrant_request():
    extractor = _extractor()
    started, finished = threading.Event(), threading.Event()
    extractor._qdrant_request = _blocking_helper(
        {
            "result": {
                "points": [{"id": "synthetic-1", "payload": {"text": "synthetic text"}}],
                "next_page_offset": None,
            }
        },
        started,
        finished,
    )

    fetch_task = asyncio.create_task(extractor._fetch_chunks_from_collection("synthetic"))
    ticker_task = asyncio.create_task(_ticks_while_blocked(started, finished))
    chunks = await fetch_task

    assert await ticker_task >= 3
    assert chunks == [
        {
            "id": "synthetic-1",
            "text": "synthetic text",
            "collection": "synthetic",
            "metadata": {"text": "synthetic text"},
        }
    ]


@pytest.mark.asyncio
async def test_fetch_chunks_propagates_qdrant_request_exception():
    extractor = _extractor()

    def raise_error(*_args, **_kwargs):
        raise SyntheticHelperError("synthetic qdrant failure")

    extractor._qdrant_request = raise_error

    with pytest.raises(SyntheticHelperError, match="synthetic qdrant failure"):
        await extractor._fetch_chunks_from_collection("synthetic")


@pytest.mark.asyncio
async def test_run_keeps_event_loop_running_during_collection_discovery():
    extractor = _extractor()
    started, finished = threading.Event(), threading.Event()

    async def processed_ids():
        return set()

    extractor.get_processed_chunk_ids = processed_ids
    extractor.get_qdrant_collections = _blocking_helper(["synthetic"], started, finished)
    extractor.get_collection_chunks = lambda *_args, **_kwargs: []

    run_task = asyncio.create_task(extractor.run(dry_run=True))
    ticker_task = asyncio.create_task(_ticks_while_blocked(started, finished))

    assert await run_task == extractor.stats
    assert await ticker_task >= 3


@pytest.mark.asyncio
async def test_run_keeps_event_loop_running_during_collection_scan():
    extractor = _extractor()
    started, finished = threading.Event(), threading.Event()

    async def processed_ids():
        return set()

    extractor.get_processed_chunk_ids = processed_ids
    extractor.get_collection_chunks = _blocking_helper(
        [
            {
                "id": "synthetic-2",
                "text": "synthetic text",
                "collection": "synthetic",
                "metadata": {},
            }
        ],
        started,
        finished,
    )

    run_task = asyncio.create_task(extractor.run(collections=["synthetic"], dry_run=True))
    ticker_task = asyncio.create_task(_ticks_while_blocked(started, finished))

    assert await run_task == extractor.stats
    assert await ticker_task >= 3
