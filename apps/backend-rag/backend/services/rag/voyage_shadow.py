"""Immutable, independent Qdrant collections for public Voyage retrieval experiments."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import QdrantClient, models

from backend.core.bm25_vectorizer import BM25Vectorizer
from backend.services.rag.voyage_public import (
    DIMENSIONS,
    DOC_MODEL,
    MAX_FILE,
    RERANK_MODELS,
    digest_text,
    embed,
    load_index,
    unit_vector,
)

SPARSE = "legal_bm25_v1"
DENSE = "voyage4_public_dense"
CONTRACT = "voyage4_large_1024_cosine_legal_bm25_v1_idf"
BATCH = 32

# These are lexical retrieval features, not a legal-identity resolver.
ARTICLE = re.compile(r"(?<!\w)pasal\s+([0-9]{1,4}[a-z]{0,3}|[ivx]{1,6})(?!\w)")
PARAGRAPH = re.compile(r"(?<!\w)ayat\s*(?:\(([0-9]{1,3}[a-z]?)\)|([0-9]{1,3}[a-z]?))(?!\w)")
REGULATION = re.compile(
    r"(?<!\w)(peraturan\s+presiden|perpres|perppu|uu|pp|pmk)"
    r"(?:\s*-\s*|\s+)(?:(?:nomor|no\.?)\s*)?([0-9]{1,4}[a-z]?)"
    r"(?:\s*/\s*|\s*-?\s*tahun\s+|\s*-\s*)((?:18|19|20)[0-9]{2})(?![\w/])"
)
ROMAN = re.compile(r"x{0,3}(?:ix|iv|v?i{0,3})")


class LegalSparseV1(BM25Vectorizer):
    def __init__(self):
        super().__init__(vocab_size=1_000_000)

    def tokenize(self, text: str) -> list[str]:
        tokens = super().tokenize(text)
        value = text.lower()
        for label in ARTICLE.findall(value):
            if label[0].isdigit() or ROMAN.fullmatch(label):
                tokens.append("pasal_" + label)
        tokens.extend("ayat_" + (a or b) for a, b in PARAGRAPH.findall(value))
        for kind, number, year in REGULATION.findall(value):
            prefix = "perpres" if kind.startswith("peraturan") else kind
            tokens.append(f"{prefix}_{number}_{year}")
        return tokens


@dataclass(frozen=True)
class PublicSnapshot:
    manifest_digest: str
    index_digest: str
    passages: dict
    queries: dict

    @classmethod
    def read_manifest(
        cls, manifest: Path, manifest_digest: str, index_digest: str
    ) -> PublicSnapshot:
        if re.fullmatch(r"[a-f0-9]{64}", index_digest) is None:
            raise ValueError("invalid pinned vector index digest")
        with manifest.open("rb") as f:
            raw = f.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), manifest_digest
        ):
            raise ValueError("public manifest checksum mismatch")
        data = json.loads(raw)
        docs = data["public_documents"]
        questions = data["public_queries"]
        if (
            not isinstance(docs, list)
            or not docs
            or not isinstance(questions, list)
            or not questions
        ):
            raise ValueError("invalid public manifest")
        passages = {digest_text(text): text for text in docs}
        queries = {digest_text(q["text"]): q["rerank_model"] for q in questions}
        if len(questions) != len(queries) or any(m not in RERANK_MODELS for m in queries.values()):
            raise ValueError("invalid public questions")
        return PublicSnapshot(manifest_digest, index_digest, passages, queries)

    @property
    def collection(self) -> str:
        return "newrag_shadow_" + digest_text(
            f"{CONTRACT}/{self.manifest_digest}/{self.index_digest}"
        )

    def marker(self, kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "contract": CONTRACT,
            "model": DOC_MODEL,
            "manifest_sha256": self.manifest_digest,
            "index_sha256": self.index_digest,
            "passage_count": len(self.passages),
        }


@dataclass(frozen=True)
class PublicRelease(PublicSnapshot):
    vectors: dict

    @classmethod
    def load(
        cls, manifest: Path, manifest_digest: str, index: Path, index_digest: str
    ) -> PublicRelease:
        snapshot = PublicSnapshot.read_manifest(manifest, manifest_digest, index_digest)
        queries, vectors = load_index(str(index), index_digest)
        if snapshot.queries != queries or set(snapshot.passages) != set(vectors):
            raise ValueError("public manifest and vector index do not match")
        return cls(manifest_digest, index_digest, snapshot.passages, queries, vectors)


def check_collection(db: QdrantClient, release: PublicSnapshot, *, create: bool) -> None:
    name = release.collection
    if any(alias.alias_name == name for alias in db.get_aliases().aliases):
        raise ValueError("shadow name is an alias")
    if not db.collection_exists(name):
        if not create:
            raise ValueError("shadow collection does not exist")
        try:
            db.create_collection(
                name,
                vectors_config={
                    DENSE: models.VectorParams(size=DIMENSIONS, distance=models.Distance.COSINE)
                },
                sparse_vectors_config={
                    SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)
                },
            )
        except Exception:
            # A concurrent builder may already have created this exact release.
            if not db.collection_exists(name):
                raise
    params = db.get_collection(name).config.params
    if not isinstance(params.vectors, dict) or set(params.vectors) != {DENSE}:
        raise ValueError("incompatible shadow dense names")
    dense = params.vectors[DENSE]
    sparse = params.sparse_vectors or {}
    if (
        dense.size != DIMENSIONS
        or dense.distance != models.Distance.COSINE
        or dense.multivector_config
        or dense.datatype not in (None, models.Datatype.FLOAT32)
        or set(sparse) != {SPARSE}
        or sparse[SPARSE].modifier != models.Modifier.IDF
    ):
        raise ValueError("incompatible shadow vector configuration")


def marker(db: QdrantClient, release: PublicSnapshot, number: int, kind: str) -> bool:
    rows = db.retrieve(release.collection, [number], with_payload=True, with_vectors=False)
    if rows and rows[0].payload != release.marker(kind):
        raise ValueError("foreign release marker")
    return bool(rows)


def passage_point(release: PublicRelease, tokenizer: LegalSparseV1, sha: str) -> models.PointStruct:
    text = release.passages[sha]
    sparse = tokenizer.generate_sparse_vector(text)
    vectors = {DENSE: list(release.vectors[sha])}
    if sparse["indices"]:
        vectors[SPARSE] = models.SparseVector(**sparse)
    return models.PointStruct(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, "newrag-public:" + sha)),
        vector=vectors,
        payload={"kind": "passage", "text": text, "sha256": sha},
    )


def same_point(saved: models.Record, expected: models.PointStruct) -> bool:
    if (
        saved.payload != expected.payload
        or not isinstance(saved.vector, dict)
        or saved.vector.keys() != expected.vector.keys()
    ):
        return False
    dense, wanted = saved.vector[DENSE], expected.vector[DENSE]
    if len(dense) != len(wanted) or not all(
        math.isclose(x, y, abs_tol=1e-6, rel_tol=1e-5) for x, y in zip(dense, wanted, strict=True)
    ):
        return False
    if SPARSE in saved.vector:
        sparse, wanted_sparse = saved.vector[SPARSE], expected.vector[SPARSE]
        return (
            sparse.indices == wanted_sparse.indices
            and len(sparse.values) == len(wanted_sparse.values)
            and all(
                math.isclose(x, y, rel_tol=1e-5, abs_tol=1e-6)
                for x, y in zip(sparse.values, wanted_sparse.values, strict=True)
            )
        )
    return True


def write_release(db: QdrantClient, release: PublicRelease) -> int:
    """Resume a content-addressed collection and return the number of new passages written."""
    check_collection(db, release, create=True)
    name = release.collection
    if "kind" not in (db.get_collection(name).payload_schema or {}):
        db.create_payload_index(
            name,
            field_name="kind",
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )
    if not marker(db, release, 1, "build"):
        if db.count(name, exact=True).count and not marker(db, release, 1, "build"):
            raise ValueError("nonempty unowned shadow collection")
        db.upsert(
            name, [models.PointStruct(id=1, vector={}, payload=release.marker("build"))], wait=True
        )
    ready = marker(db, release, 0, "ready")
    hashes = sorted(release.passages)
    tokenizer = LegalSparseV1()
    added = 0
    for start in range(0, len(hashes), BATCH):
        points = [passage_point(release, tokenizer, key) for key in hashes[start : start + BATCH]]
        persisted = {
            str(p.id): p
            for p in db.retrieve(name, [p.id for p in points], with_payload=True, with_vectors=True)
        }
        missing = []
        for p in points:
            existing = persisted.get(p.id)
            if existing is None:
                missing.append(p)
            elif not same_point(existing, p):
                raise ValueError("persisted point disagrees with the pinned public release")
        if ready and missing:
            raise ValueError("published shadow collection is incomplete")
        if missing:
            db.upsert(name, missing, wait=True)
            added += len(missing)
    count = db.count(name, exact=True).count
    if count != len(hashes) + 1 and not (
        count == len(hashes) + 2 and marker(db, release, 0, "ready")
    ):
        raise ValueError("unexpected points in shadow collection")
    if not ready:
        db.upsert(
            name, [models.PointStruct(id=0, vector={}, payload=release.marker("ready"))], wait=True
        )
    return added


def ensure_ready(db: QdrantClient, release: PublicSnapshot) -> None:
    check_collection(db, release, create=False)
    if (
        not marker(db, release, 0, "ready")
        or not marker(db, release, 1, "build")
        or db.count(release.collection, exact=True).count != len(release.passages) + 2
    ):
        raise ValueError("shadow release is not complete")


def _retrieve_by_vector(
    db: QdrantClient, release: PublicSnapshot, question: str, query_vector: list[float], limit: int
):
    if not 0 < limit <= 100 or digest_text(question) not in release.queries:
        raise ValueError("unauthorized public shadow query")
    ensure_ready(db, release)
    dense = list(unit_vector(query_vector))
    sparse = LegalSparseV1().generate_query_sparse_vector(question)
    eligible = models.Filter(
        must=[models.FieldCondition(key="kind", match=models.MatchValue(value="passage"))]
    )
    if sparse["indices"]:
        prefetch = [
            models.Prefetch(query=dense, using=DENSE, limit=3 * limit, filter=eligible),
            models.Prefetch(
                query=models.SparseVector(**sparse), using=SPARSE, limit=3 * limit, filter=eligible
            ),
        ]
        request = {"prefetch": prefetch, "query": models.FusionQuery(fusion=models.Fusion.RRF)}
    else:
        request = {"query": dense, "using": DENSE}
    hits = db.query_points(
        release.collection, **request, query_filter=eligible, limit=limit, with_payload=True
    ).points
    for hit in hits:
        payload = hit.payload or {}
        text = payload.get("text")
        if (
            not isinstance(text, str)
            or payload.get("kind") != "passage"
            or release.passages.get(payload.get("sha256")) != text
        ):
            raise ValueError("unverified public shadow hit")
    return hits


async def query_release(
    db: QdrantClient, release: PublicSnapshot, question: str, limit: int, api: httpx.AsyncClient
):
    """Offline comparison entry point. Always use the large-compatible Voyage query model."""
    if not 0 < limit <= 100 or digest_text(question) not in release.queries:
        raise ValueError("unauthorized public shadow query")
    ensure_ready(db, release)
    (vector,) = await embed(api, [question], documents=False)
    return _retrieve_by_vector(db, release, question, list(vector), limit)
