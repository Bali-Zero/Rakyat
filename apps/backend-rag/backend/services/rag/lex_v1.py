"""Pilot legal index `legal_lex_v1`: pinned chunks file -> Voyage dense + legal BM25 sparse, RRF.

Inert: nothing served imports this module. The module never builds a Qdrant or Voyage client;
its caller passes both. The release is the sha256 of the chunks file, given by the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import QdrantClient, models

from backend.services.rag.voyage_public import DIMENSIONS, embed
from backend.services.rag.voyage_shadow import LegalSparseV1


class LexError(ValueError):
    """Every refusal this module raises; its message is safe to print."""


COLLECTION = "legal_lex_v1"
DENSE = "voyage4_large_dense"
SPARSE = "legal_bm25_v1"
CONTRACT = "legal_lex_v1/voyage4_large_1024_cosine/legal_bm25_v1_idf"
MAX_TEXT = 4500
MAX_FILE = 256 * 1024 * 1024
BATCH_TEXTS = 64
BATCH_TOKENS = 100_000
CHARS_PER_TOKEN = 2
TRIES = 6
BACKOFF_SECONDS = 1.0
STATUSES = ("berlaku", "diubah")
INDEXED = ("kind", "document_id", "domain", "part", "status_vigensi")

_STR = "canonical_id chunk_key citation domain header id jenis issuer language nomor official_url"
_STR += " part source_sha256 status status_scope structure text title authority_tier body_sha256"
STR_FIELDS = tuple(_STR.split())
OPT_STR_FIELDS = ("ayat_from", "ayat_to", "bab", "pasal", "valid_to")
OPT_INT_FIELDS = ("page_from", "page_to")
INT_FIELDS = ("seq", "tahun")
LIST_FIELDS = ("amended_by", "repealed_by")
FIELDS = frozenset(
    STR_FIELDS + OPT_STR_FIELDS + OPT_INT_FIELDS + INT_FIELDS + LIST_FIELDS + ("valid_from",)
)
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_pinned(path: Path, sha256: str) -> bytes:
    """Stream a file, refuse an oversize one or a digest mismatch, return its bytes."""
    digest, parts, size = hashlib.sha256(), [], 0
    with Path(path).open("rb") as stream:
        while block := stream.read(1 << 20):
            size += len(block)
            if size > MAX_FILE:
                raise LexError("file exceeds the size limit")
            digest.update(block)
            parts.append(block)
    if not hmac.compare_digest(digest.hexdigest(), sha256.lower()):
        raise LexError("file checksum mismatch")
    return b"".join(parts)


def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or ISO_DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_record(record: Any, line: int) -> None:
    """Raise LexError naming the line and the failed check; never echoes record text."""

    def bad(why: str) -> LexError:
        return LexError(f"line {line}: {why}")

    if not isinstance(record, dict) or set(record) != FIELDS:
        raise bad("fields differ from the record schema")
    for name in STR_FIELDS:
        if not isinstance(record[name], str):
            raise bad(f"{name} is not a string")
    for name in (*OPT_STR_FIELDS, "valid_from"):
        if not (isinstance(record[name], str) or (record[name] is None and name != "valid_from")):
            raise bad(f"{name} has the wrong type")
    for name in INT_FIELDS:
        if type(record[name]) is not int:
            raise bad(f"{name} is not an integer")
    for name in OPT_INT_FIELDS:
        if record[name] is not None and type(record[name]) is not int:
            raise bad(f"{name} has the wrong type")
    for name in LIST_FIELDS:
        if not isinstance(record[name], list) or not all(isinstance(x, str) for x in record[name]):
            raise bad(f"{name} is not a list of strings")
    pasal = record["pasal"] if record["pasal"] not in (None, "") else "-"
    expected = f"{record['canonical_id']}#{record['part']}#{pasal}#{record['seq']:04d}"
    if record["chunk_key"] != expected:
        raise bad("chunk_key disagrees with the record fields")
    text, header = record["text"], record["header"]
    if not 0 < len(text) <= MAX_TEXT:
        raise bad("text length out of bounds")
    if not text.startswith(header + "\n"):
        raise bad("text does not start with its header")
    if not text[len(header) + 1 :].strip():
        raise bad("text has no body")
    if record["body_sha256"] != _sha(text[len(header) + 1 :]):
        raise bad("body_sha256 mismatch")
    if record["id"] != str(
        uuid.uuid5(uuid.NAMESPACE_URL, "legal-lex-v1:" + record["chunk_key"] + ":" + _sha(text))
    ):
        raise bad("id mismatch")
    if not record["official_url"].startswith("https://"):
        raise bad("official_url is not https")
    if record["status"] not in STATUSES:
        raise bad("status is not accepted")
    if not _is_date(record["valid_from"]):
        raise bad("valid_from is not an ISO date")
    if record["valid_to"] is not None and not _is_date(record["valid_to"]):
        raise bad("valid_to is not an ISO date")


@dataclass(frozen=True)
class Release:
    sha256: str
    records: tuple[dict, ...]

    @classmethod
    def load(cls, path: Path, sha256: str) -> Release:
        raw = read_pinned(path, sha256)
        records, ids, keys = [], set(), set()
        for number, line in enumerate(raw.decode("utf-8").split("\n"), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as error:
                raise LexError(f"line {number}: not JSON") from error
            check_record(record, number)
            if record["id"] in ids or record["chunk_key"] in keys:
                raise LexError(f"line {number}: duplicate id or chunk_key")
            ids.add(record["id"])
            keys.add(record["chunk_key"])
            records.append(record)
        if not records:
            raise LexError("no records")
        return cls(sha256.lower(), tuple(records))

    def marker(self, kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "contract": CONTRACT,
            "release": self.sha256,
            "chunk_count": len(self.records),
        }

    def payload(self, record: dict) -> dict[str, Any]:
        out = {k: v for k, v in record.items() if k not in ("id", "title")}
        out.update(
            kind="chunk",
            title=f"{record['citation']} — {record['title']}",
            norm_title=record["title"],
            url=record["official_url"],
            document_id=record["canonical_id"],
            status_vigensi=record["status"],
            retrieval_scope="current",
            release=self.sha256,
            contract=CONTRACT,
        )
        return out


def ensure_collection(db: QdrantClient, create: bool) -> None:
    if any(alias.alias_name == COLLECTION for alias in db.get_aliases().aliases):
        raise LexError("collection name is an alias")
    if not db.collection_exists(COLLECTION):
        if not create:
            raise LexError("collection does not exist")
        db.create_collection(
            COLLECTION,
            vectors_config={
                DENSE: models.VectorParams(size=DIMENSIONS, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
    params = db.get_collection(COLLECTION).config.params
    if not isinstance(params.vectors, dict) or set(params.vectors) != {DENSE}:
        raise LexError("incompatible dense vector names")
    dense, sparse = params.vectors[DENSE], params.sparse_vectors or {}
    if (
        dense.size != DIMENSIONS
        or dense.distance != models.Distance.COSINE
        or dense.multivector_config
        or dense.datatype not in (None, models.Datatype.FLOAT32)
        or set(sparse) != {SPARSE}
        or sparse[SPARSE].modifier != models.Modifier.IDF
    ):
        raise LexError("incompatible vector configuration")


def ensure_indexes(db: QdrantClient) -> None:
    """Create the payload indexes; call only once the collection is verified to be ours."""
    have = db.get_collection(COLLECTION).payload_schema or {}
    for field in INDEXED:
        if field not in have:
            db.create_payload_index(
                COLLECTION, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
            )
    if "valid_to" not in have:
        db.create_payload_index(
            COLLECTION, field_name="valid_to", field_schema=models.PayloadSchemaType.DATETIME
        )


def read_marker(db: QdrantClient, release: Release | None, number: int) -> dict | None:
    rows = db.retrieve(COLLECTION, [number], with_payload=True, with_vectors=False)
    if not rows:
        return None
    kind = "build" if number == 1 else "ready"
    if release is not None and rows[0].payload != release.marker(kind):
        raise LexError("foreign release marker")
    return rows[0].payload


def batches(records: tuple[dict, ...]) -> list[list[dict]]:
    """Split into groups of at most BATCH_TEXTS texts and BATCH_TOKENS estimated tokens."""
    out, current, tokens = [], [], 0
    for record in records:
        cost = len(record["text"]) / CHARS_PER_TOKEN
        if current and (len(current) >= BATCH_TEXTS or tokens + cost > BATCH_TOKENS):
            out.append(current)
            current, tokens = [], 0
        current.append(record)
        tokens += cost
    if current:
        out.append(current)
    return out


async def embed_retry(api: httpx.AsyncClient, texts: list[str], *, documents: bool) -> list:
    for attempt in range(TRIES):
        try:
            return await embed(api, texts, documents=documents)
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if (status != 429 and status < 500) or attempt == TRIES - 1:
                raise
        await asyncio.sleep(BACKOFF_SECONDS * 2**attempt)
    raise AssertionError("unreachable")


async def embed_split(api: httpx.AsyncClient, records: list[dict]) -> list:
    """Embed documents; on an HTTP 400 for several texts, halve the group and retry each half."""
    try:
        return await embed_retry(api, [r["text"] for r in records], documents=True)
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 400:
            raise
        if len(records) == 1:
            raise LexError(f"voyage refused chunk {records[0]['chunk_key']}") from error
    middle = len(records) // 2
    return [*await embed_split(api, records[:middle]), *await embed_split(api, records[middle:])]


def _sparse(tokenizer: LegalSparseV1, text: str) -> models.SparseVector | None:
    sparse = tokenizer.generate_sparse_vector(text)
    return models.SparseVector(**sparse) if sparse["indices"] else None


async def write_release(db: QdrantClient, release: Release, api: httpx.AsyncClient) -> int:
    """Resume a content-addressed build; return the number of chunk points written."""
    ensure_collection(db, create=True)
    if read_marker(db, release, 1) is None:
        if db.count(COLLECTION, exact=True).count:
            raise LexError("nonempty collection without this release's build marker")
        db.upsert(
            COLLECTION,
            [models.PointStruct(id=1, vector={}, payload=release.marker("build"))],
            wait=True,
        )
    ensure_indexes(db)
    ready = read_marker(db, release, 0) is not None
    tokenizer, added = LegalSparseV1(), 0
    for group in batches(release.records):
        saved = {
            str(p.id): p
            for p in db.retrieve(
                COLLECTION, [r["id"] for r in group], with_payload=True, with_vectors=False
            )
        }
        missing = []
        for record in group:
            existing = saved.get(record["id"])
            if existing is None:
                missing.append(record)
            elif existing.payload != release.payload(record):
                raise LexError("persisted payload disagrees with the pinned release")
        if not missing:
            continue
        if ready:
            raise LexError("published collection is incomplete")
        vectors = await embed_split(api, missing)
        points = []
        for record, vector in zip(missing, vectors, strict=True):
            named: dict[str, Any] = {DENSE: list(vector)}
            sparse = _sparse(tokenizer, record["text"])
            if sparse is not None:
                named[SPARSE] = sparse
            points.append(
                models.PointStruct(id=record["id"], vector=named, payload=release.payload(record))
            )
        db.upsert(COLLECTION, points, wait=True)
        added += len(points)
    count = db.count(COLLECTION, exact=True).count
    if count != len(release.records) + (2 if ready else 1):
        raise LexError("unexpected points in collection")
    if not ready:
        db.upsert(
            COLLECTION,
            [models.PointStruct(id=0, vector={}, payload=release.marker("ready"))],
            wait=True,
        )
    return added


def eligible(on: date) -> models.Filter:
    expired = models.DatetimeRange(lt=datetime.combine(on, time.min, tzinfo=UTC))
    return models.Filter(
        must=[models.FieldCondition(key="kind", match=models.MatchValue(value="chunk"))],
        must_not=[
            models.FieldCondition(key="status_vigensi", match=models.MatchValue(value="dicabut")),
            models.FieldCondition(key="valid_to", range=expired),
        ],
    )


async def search(
    db: QdrantClient, question: str, limit: int, api: httpx.AsyncClient, *, on: date
) -> list[dict]:
    if not 0 < limit <= 100:
        raise LexError("limit must be from 1 to 100")
    ensure_collection(db, create=False)
    ready = read_marker(db, None, 0)
    if ready is None or ready.get("kind") != "ready" or ready.get("contract") != CONTRACT:
        raise LexError("collection is not ready")
    (vector,) = await embed_retry(api, [question], documents=False)
    dense = list(vector)
    sparse = LegalSparseV1().generate_query_sparse_vector(question)
    where = eligible(on)
    kind = "LEX_V1_RRF" if sparse["indices"] else "LEX_V1_DENSE"
    if sparse["indices"]:
        request: dict[str, Any] = {
            "prefetch": [
                models.Prefetch(query=dense, using=DENSE, limit=3 * limit, filter=where),
                models.Prefetch(
                    query=models.SparseVector(**sparse), using=SPARSE, limit=3 * limit, filter=where
                ),
            ],
            "query": models.FusionQuery(fusion=models.Fusion.RRF),
        }
    else:
        request = {"query": dense, "using": DENSE}
    hits = db.query_points(
        COLLECTION, **request, query_filter=where, limit=limit, with_payload=True
    ).points
    return [
        {
            "text": (hit.payload or {}).get("text", ""),
            "metadata": {k: v for k, v in (hit.payload or {}).items() if k != "text"},
            "score": hit.score,
            "score_kind": kind,
        }
        for hit in hits
    ]


__all__ = [
    "COLLECTION",
    "CONTRACT",
    "DENSE",
    "SPARSE",
    "LexError",
    "Release",
    "ensure_collection",
    "search",
    "write_release",
]
