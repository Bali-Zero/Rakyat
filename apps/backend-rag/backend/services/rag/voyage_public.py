"""Voyage canary for approved public questions and exact public legal passages."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from array import array
from functools import lru_cache
from pathlib import Path

import httpx

BASE_URL = "https://api.voyageai.com/v1/"
DOC_MODEL = "voyage-4-large"
QUERY_MODEL = "voyage-4-lite"
DIMENSIONS = 1024
RERANK_MODELS = {"rerank-3", "rerank-3-lite"}
MAX_FILE = 64 * 1024 * 1024


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit_vector(values: list[float]) -> array:
    if not isinstance(values, list) or len(values) != DIMENSIONS:
        raise ValueError("wrong vector dimension")
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
        raise ValueError("nonfinite vector component")
    norm = math.hypot(*values)
    if not norm or not math.isfinite(norm):
        raise ValueError("unusable vector norm")
    return array("f", (v / norm for v in values))


def ordered_data(
    response: httpx.Response, expected: int, available: int, *, model: str
) -> list[dict]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or ("model" in payload and payload["model"] != model):
        raise ValueError("unexpected provider model")
    rows = payload["data"]
    if not isinstance(rows, list) or len(rows) != expected:
        raise ValueError("unexpected result length")
    indices = [r["index"] for r in rows]
    if (
        any(type(i) is not int or not 0 <= i < available for i in indices)
        or len(set(indices)) != expected
    ):
        raise ValueError("invalid result indices")
    return rows


async def embed(client: httpx.AsyncClient, texts: list[str], *, documents: bool) -> list[array]:
    response = await client.post(
        "embeddings",
        json={
            "model": DOC_MODEL if documents else QUERY_MODEL,
            "input": texts,
            "input_type": "document" if documents else "query",
            "output_dimension": DIMENSIONS,
            "truncation": False,
        },
    )
    rows = ordered_data(
        response, len(texts), len(texts), model=DOC_MODEL if documents else QUERY_MODEL
    )
    return [unit_vector(row["embedding"]) for row in sorted(rows, key=lambda row: row["index"])]


@lru_cache(maxsize=1)
def load_index(path: str, digest: str) -> tuple[dict[str, str], dict[str, array]]:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
        raise ValueError("public index checksum mismatch")
    data = json.loads(raw)
    if (data["version"], data["document_model"], data["dimensions"]) != (1, DOC_MODEL, DIMENSIONS):
        raise ValueError("incompatible embedding space")
    queries, vectors = data["query_sha256"], data["vectors"]
    if not isinstance(queries, dict) or not isinstance(vectors, dict):
        raise ValueError("invalid public index")
    if any(re.fullmatch(r"[a-f0-9]{64}", k) is None for k in (*queries, *vectors)):
        raise ValueError("invalid public digest")
    if any(model not in RERANK_MODELS for model in queries.values()):
        raise ValueError("invalid reranking model")
    return queries, {k: unit_vector(v) for k, v in vectors.items()}
