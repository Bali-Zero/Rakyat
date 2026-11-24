"""Build or query an isolated public Qdrant shadow from two pinned artifacts.

Example: python -m scripts.voyage_qdrant_shadow build --local-dir /data/newrag-lab
--manifest public.json --manifest-sha256 <digest> --index vectors.json --index-sha256 <digest>
Use the query command with --question to run an approved question against the finished release.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from qdrant_client import QdrantClient

from backend.services.rag.voyage_public import BASE_URL, digest_text
from backend.services.rag.voyage_shadow import PublicRelease, query_release, write_release


def open_database(local_dir, remote):
    if not remote:
        return QdrantClient(path=str(local_dir))
    url = os.environ.get("QDRANT_URL", "")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("remote shadow requires an HTTPS QDRANT_URL without embedded credentials")
    key = os.environ.get("QDRANT_API_KEY")
    if not key:
        raise ValueError("remote shadow requires QDRANT_API_KEY")
    return QdrantClient(url=url, api_key=key, timeout=15)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "query"))
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--local-dir", type=Path)
    destination.add_argument(
        "--remote",
        action="store_true",
        help="Use the configured Qdrant deployment. Only the digest-named shadow is addressed.",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--index-sha256", required=True)
    parser.add_argument("--question")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    release = PublicRelease.load(args.manifest, args.manifest_sha256, args.index, args.index_sha256)
    if args.command == "query" and (
        not args.question
        or not 0 < args.limit <= 100
        or digest_text(args.question) not in release.queries
    ):
        parser.error("query requires an approved question and a limit from 1 to 100")
    db = open_database(args.local_dir, args.remote)
    try:
        if args.command == "build":
            print(
                json.dumps(
                    {
                        "collection": release.collection,
                        "added": write_release(db, release),
                        "documents": len(release.passages),
                    }
                )
            )
        else:
            async with httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"Authorization": "Bearer " + os.environ["VOYAGE_API_KEY"]},
                timeout=10,
                follow_redirects=False,
            ) as api:
                hits = await query_release(db, release, args.question, args.limit, api)
            print(
                json.dumps(
                    {
                        "collection": release.collection,
                        "candidates": [
                            {"sha256": h.payload["sha256"], "score": h.score} for h in hits
                        ],
                    }
                )
            )
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
