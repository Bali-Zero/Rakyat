"""Measure shadow overlap for exact, approved public questions after final source selection."""

from __future__ import annotations

import asyncio
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import lru_cache
from pathlib import Path

import httpx
from qdrant_client import QdrantClient

from backend.core.collection_registry import canonicalize_collection_name
from backend.services.rag.agentic.voyage_scope import reserve_observation
from backend.services.rag.voyage_public import (
    BASE_URL,
    QUERY_MODEL,
    digest_text,
    embed,
    ordered_data,
)
from backend.services.rag.voyage_shadow import PublicSnapshot, _retrieve_by_vector, ensure_ready

# Diagnostic probes never queue behind other probes.
_SLOTS = threading.BoundedSemaphore(2)
_IO_SLOTS = threading.BoundedSemaphore(2)
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voyage-shadow")
# A cold Qdrant TLS connection plus both provider calls exceeds the old ceiling.
OBSERVATION_TIMEOUT_SECONDS = 10.0
# A cold Qdrant TLS connection can take almost one second before the query itself.
QDRANT_TIMEOUT_SECONDS = 3
# Voyage embedding and reranking calls need room for observed public-network variance.
VOYAGE_TIMEOUT_SECONDS = 3.0


async def _io(func, *args):
    # A sync HTTP read can outlive the caller's timeout. Keep its slot until
    # the actual worker exits and avoid the application's shared executor.
    if not _IO_SLOTS.acquire(blocking=False):
        raise RuntimeError("shadow workers busy")
    try:
        future = _POOL.submit(func, *args)
    except BaseException:
        _IO_SLOTS.release()
        raise
    future.add_done_callback(lambda _: _IO_SLOTS.release())
    return await asyncio.wrap_future(future)


@lru_cache(maxsize=1)
def _release(manifest, manifest_sha, index_sha):
    return PublicSnapshot.read_manifest(Path(manifest), manifest_sha, index_sha)


def _reader(cfg):
    return closing(
        QdrantClient(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key,
            timeout=QDRANT_TIMEOUT_SECONDS,
            check_compatibility=False,
        )
    )


def _provider(cfg):
    return httpx.AsyncClient(  # golden-rule-10-exempt: every caller enters this factory result with async with
        base_url=BASE_URL,
        headers={"Authorization": "Bearer " + cfg.voyage_api_key.get_secret_value()},
        timeout=VOYAGE_TIMEOUT_SECONDS,
        follow_redirects=False,
    )


def _ready(release, cfg):
    with _reader(cfg) as db:
        ensure_ready(db, release)


def _hits(release, cfg, question, vector, depth):
    with _reader(cfg) as db:
        return _retrieve_by_vector(db, release, question, list(vector), depth)


async def observe_legal_search(question, targets, served, top_k, cfg):
    """Return aggregate measurements only. The answer path owns all served passages and scores."""
    if not (
        cfg.voyage_public_manifest
        and cfg.voyage_public_manifest_sha256
        and cfg.voyage_public_index_sha256
        and cfg.voyage_api_key
        and 0 < top_k <= 100
    ):
        return None
    if not any(canonicalize_collection_name(target) == "legal_unified" for target in targets):
        return None
    if not reserve_observation(question) or not _SLOTS.acquire(blocking=False):
        return None
    approved = False
    try:
        async with asyncio.timeout(OBSERVATION_TIMEOUT_SECONDS):
            release = await _io(
                _release,
                cfg.voyage_public_manifest,
                cfg.voyage_public_manifest_sha256,
                cfg.voyage_public_index_sha256,
            )
            model = release.queries.get(digest_text(question))
            if model is None:
                return None
            approved = True
            await _io(_ready, release, cfg)
            async with _provider(cfg) as api:
                (vector,) = await embed(api, [question], documents=False)
                hits = await _io(_hits, release, cfg, question, vector, min(top_k * 2, 100))
                hit_ids = [h.payload["sha256"] for h in hits]
                if hits:
                    # Read the text from the approved local manifest, never from the served answer.
                    ranked = await api.post(
                        "rerank",
                        json={
                            "model": model,
                            "query": question,
                            "documents": [release.passages[i] for i in hit_ids],
                            "top_k": min(top_k, len(hits)),
                            "return_documents": False,
                            "truncation": False,
                        },
                    )
                    rows = ordered_data(ranked, min(top_k, len(hits)), len(hits), model=model)
                    if any(
                        type(r.get("relevance_score")) not in (float, int)
                        or not math.isfinite(r["relevance_score"])
                        or not 0 <= r["relevance_score"] <= 1
                        for r in rows
                    ):
                        raise ValueError("unusable reranker response")
                    selected = [hit_ids[r["index"]] for r in rows]
                else:
                    selected = []
            baseline = [
                digest_text(c["text"])
                for c in served
                if isinstance(c, dict)
                and canonicalize_collection_name(c.get("_source_collection", "")) == "legal_unified"
                and isinstance(c.get("text"), str)
            ]
            return {
                "mode": "shadow",
                "status": "ok",
                "query_model": QUERY_MODEL,
                "rerank_model": model,
                "index_sha256": release.index_digest,
                "collection": release.collection,
                "served_legal": len(baseline),
                "shadow_count": len(selected),
                "overlap": len(set(baseline).intersection(selected)),
                "top1_same": (baseline[0] == selected[0]) if baseline and selected else None,
            }
    except Exception:
        # The side experiment cannot turn an available baseline answer into an error.
        # CancelledError propagates so owner cancellation is still honored.
        return {"mode": "shadow", "status": "unavailable"} if approved else None
    finally:
        _SLOTS.release()
