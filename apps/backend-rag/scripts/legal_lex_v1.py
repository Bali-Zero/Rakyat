"""Build, inspect, query and measure the pilot legal index `legal_lex_v1`.

Example: python -m scripts.legal_lex_v1 build --remote --chunks out/chunks_v1.jsonl --sha256 <hex>
`eval` prints one JSON object to stdout: per-case rows (no question text) and aggregates.
The Voyage key comes from VOYAGE_API_KEY or the application settings and is never printed.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

import httpx
from qdrant_client import QdrantClient

from backend.services.rag import lex_v1
from backend.services.rag.voyage_public import BASE_URL
from scripts.voyage_qdrant_shadow import open_database

BASELINE_COLLECTION = "legal_unified"


def voyage_client() -> httpx.AsyncClient:
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        from backend.app.core.config import settings

        secret = settings.voyage_api_key
        key = secret.get_secret_value() if secret else ""
    if not key:
        raise ValueError("no Voyage API key configured")
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": "Bearer " + key},
        timeout=60,
        follow_redirects=False,
    )


def status(db: QdrantClient) -> dict:
    if not db.collection_exists(lex_v1.COLLECTION):
        return {"collection": lex_v1.COLLECTION, "exists": False}
    rows = db.retrieve(lex_v1.COLLECTION, [0, 1], with_payload=True, with_vectors=False)
    marks = {r.id: r.payload for r in rows}
    return {
        "collection": lex_v1.COLLECTION,
        "exists": True,
        "count": db.count(lex_v1.COLLECTION, exact=True).count,
        "ready": 0 in marks,
        "build_marker": marks.get(1),
        "ready_marker": marks.get(0),
    }


def load_json(path: Path, sha256: str):
    return json.loads(lex_v1.read_pinned(path, sha256))


def load_cases(path: Path, sha256: str) -> list[dict]:
    """Parse the pinned cases file: {"cases": [{id, domain, question, ready_norms, ready_pasal}]}."""
    data = load_json(path, sha256)
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases file must be an object with a non-empty 'cases' list")
    for n, case in enumerate(cases, 1):
        ok = (
            isinstance(case, dict)
            and all(isinstance(case.get(k), str) and case[k] for k in ("id", "domain", "question"))
            and isinstance(case.get("ready_norms"), list)
            and case["ready_norms"]
            and all(isinstance(x, str) for x in case["ready_norms"])
            and isinstance(case.get("ready_pasal"), list)
            and all(
                isinstance(x, list) and len(x) == 2 and all(isinstance(y, str) for y in x)
                for x in case["ready_pasal"]
            )
        )
        if not ok:
            raise ValueError(f"cases file: case {n} has an unexpected shape")
    return cases


def load_supersedes(path: Path, sha256: str) -> dict[str, list[str]]:
    data = load_json(path, sha256)
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)
        for k, v in data.items()
    ):
        raise ValueError("supersedes file must map canonical id to a list of old ids")
    return data


def old_to_canonical(supersedes: dict) -> dict[str, str]:
    return {old: canonical for canonical, olds in supersedes.items() for old in olds}


def pasal_pairs(case: dict) -> set[tuple[str, str]]:
    return {(norm, pasal) for norm, pasal in case["ready_pasal"]}


def score(case: dict, found: set[str], located: set[tuple[str, str]]) -> dict:
    ready = set(case["ready_norms"])
    recall = len(ready & found) / len(ready) if ready else 0.0
    wanted = pasal_pairs(case)
    return {"recall": recall, "hit": recall > 0, "pasal_hit": bool(wanted & located)}


def aggregate(rows: list[dict], path: str) -> dict:
    def summary(items: list[dict]) -> dict:
        n = len(items)
        return {
            "cases": n,
            "recall": sum(r[path]["recall"] for r in items) / n if n else 0.0,
            "hit_rate": sum(r[path]["hit"] for r in items) / n if n else 0.0,
            "pasal_hit": sum(r[path]["pasal_hit"] for r in items),
        }

    domains = sorted({r["domain"] for r in rows})
    return {
        "all": summary(rows),
        "by_domain": {d: summary([r for r in rows if r["domain"] == d]) for d in domains},
    }


def baseline_service():
    """Build the SearchService the way the app does: service_initializer._init_search_service
    (backend/app/setup/service_initializer.py:187-217), minus app state, registry and the
    Qdrant probe. VectorSearchTool is created with user_level=3 (agentic/__init__.py:176).
    """
    from backend.app.core.config import settings
    from backend.core.embeddings import create_embeddings_generator
    from backend.services.ingestion.collection_manager import CollectionManager
    from backend.services.misc.cultural_insights_service import CulturalInsightsService
    from backend.services.routing.conflict_resolver import ConflictResolver
    from backend.services.routing.query_router_integration import QueryRouterIntegration
    from backend.services.routing.surface_router import SurfaceRouter
    from backend.services.search.search_service import SearchService

    collection_manager = CollectionManager(qdrant_url=settings.qdrant_url)
    service = SearchService(
        collection_manager=collection_manager,
        conflict_resolver=ConflictResolver(),
        cultural_insights=CulturalInsightsService(
            collection_manager=collection_manager, embedder=create_embeddings_generator()
        ),
        query_router=QueryRouterIntegration(),
        surface_router=SurfaceRouter(),
    )
    if settings.reranker_backend == "cross-encoder":
        from backend.services.rag.reranker_integration import add_cross_encoder_reranking

        add_cross_encoder_reranking(service)
    return service


async def baseline_hits(service, question: str, k: int, level: int) -> list[dict]:
    result = await service.search_with_reranking(
        query=question, user_level=level, limit=k, collection_override=BASELINE_COLLECTION
    )
    return result.get("results", [])


async def evaluate(db: QdrantClient, args, api: httpx.AsyncClient, service) -> dict:
    cases = load_cases(args.cases, args.sha256)
    mapping = {}
    if args.supersedes:
        mapping = old_to_canonical(load_supersedes(args.supersedes, args.supersedes_sha256))
    today, rows = date.today(), []
    for case in cases:
        found = await lex_v1.search(db, case["question"], args.k, api, on=today)
        new_ids = {h["metadata"]["document_id"] for h in found}
        new_loc = {(h["metadata"]["document_id"], str(h["metadata"].get("pasal"))) for h in found}
        old_ids, old_loc = set(), set()
        for chunk in await baseline_hits(service, case["question"], args.k, args.user_level):
            meta = chunk.get("metadata", {}) if isinstance(chunk, dict) else {}
            raw = meta.get("chapter_id") or meta.get("document_id") or meta.get("id", "")
            canonical = mapping.get(raw, raw)
            old_ids.add(canonical)
            old_loc.add((canonical, str(meta.get("pasal"))))
        rows.append(
            {
                "id": case["id"],
                "domain": case.get("domain", ""),
                "lex_v1": score(case, new_ids, new_loc),
                "legal_unified": score(case, old_ids, old_loc),
            }
        )
    return {
        "k": args.k,
        "rows": rows,
        "lex_v1": aggregate(rows, "lex_v1"),
        "legal_unified": aggregate(rows, "legal_unified"),
    }


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)

    def common(p):
        where = p.add_mutually_exclusive_group(required=True)
        where.add_argument("--local-dir", type=Path)
        where.add_argument("--remote", action="store_true")

    build = sub.add_parser("build")
    common(build)
    build.add_argument("--chunks", type=Path, required=True)
    build.add_argument("--sha256", required=True)
    common(sub.add_parser("status"))
    query = sub.add_parser("query")
    common(query)
    query.add_argument("--question", required=True)
    query.add_argument("--limit", type=int, default=5)
    ev = sub.add_parser("eval")
    common(ev)
    ev.add_argument("--cases", type=Path, required=True)
    ev.add_argument("--sha256", required=True)
    ev.add_argument("--k", type=int, default=10)
    ev.add_argument("--user-level", type=int, default=3)
    ev.add_argument("--supersedes", type=Path)
    ev.add_argument("--supersedes-sha256")
    return top


async def run(args) -> dict:
    if args.command == "eval" and bool(args.supersedes) != bool(args.supersedes_sha256):
        raise ValueError("--supersedes and --supersedes-sha256 go together")
    release = lex_v1.Release.load(args.chunks, args.sha256) if args.command == "build" else None
    db = open_database(args.local_dir, args.remote)
    try:
        if args.command == "status":
            return status(db)
        async with voyage_client() as api:
            if args.command == "build":
                added = await lex_v1.write_release(db, release, api)
                return {
                    "collection": lex_v1.COLLECTION,
                    "added": added,
                    "chunks": len(release.records),
                }
            if args.command == "query":
                hits = await lex_v1.search(db, args.question, args.limit, api, on=date.today())
                return {"collection": lex_v1.COLLECTION, "hits": hits}
            return await evaluate(db, args, api, baseline_service())
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"error: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
