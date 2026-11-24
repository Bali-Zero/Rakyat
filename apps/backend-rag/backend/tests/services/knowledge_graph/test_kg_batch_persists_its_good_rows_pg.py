"""Real-PostgreSQL coverage for per-row KG persistence savepoints."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from backend.services.knowledge_graph.extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
)
from backend.services.knowledge_graph.ontology import EntityType, RelationType
from backend.services.knowledge_graph.pipeline import KGPipeline, PipelineStats

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = pytest.mark.skipif(
    not _is_safe_test_database(_DB_URL), reason="needs a local test database"
)


class _Acquire:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> asyncpg.Connection:
        return self.connection

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Pool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Validator:
    def __init__(self) -> None:
        self.result = type(
            "Result", (), {"summary": lambda _: {"valid": 1, "invalid": 0, "total": 1}}
        )()

    def check(self, **kwargs: Any) -> bool:
        return True


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    assert _DB_URL is not None
    schema = f"kg_batch_{uuid.uuid4().hex[:8]}"
    connection = await asyncpg.connect(_DB_URL)
    created = False
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        await connection.execute(f'SET search_path TO "{schema}"')
        await connection.execute(
            """
            CREATE TABLE kg_nodes (
                entity_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                properties JSONB NOT NULL,
                confidence DOUBLE PRECISION NOT NULL,
                source_collection TEXT NOT NULL,
                source_chunk_ids TEXT[] NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        await connection.execute(
            """
            CREATE TABLE kg_edges (
                relationship_id TEXT PRIMARY KEY,
                source_entity_id TEXT NOT NULL REFERENCES kg_nodes(entity_id),
                target_entity_id TEXT NOT NULL REFERENCES kg_nodes(entity_id),
                relationship_type TEXT NOT NULL,
                properties JSONB NOT NULL,
                confidence DOUBLE PRECISION NOT NULL,
                source_collection TEXT NOT NULL,
                source_chunk_ids TEXT[] NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        yield connection
    finally:
        try:
            if created:
                await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await connection.close()


@pytest.fixture(autouse=True)
def validator(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.services.knowledge_graph import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "SchemaValidator", _Validator)


def _pipeline(connection: asyncpg.Connection) -> KGPipeline:
    pipeline = object.__new__(KGPipeline)
    pipeline._db_pool = _Pool(connection)
    pipeline.stats = PipelineStats()
    pipeline.relation_registry = set()
    return pipeline


def _entity(entity_id: str, name: str) -> ExtractedEntity:
    return ExtractedEntity(entity_id, EntityType.KITAS, name, name)


def _relation(source_id: str, target_id: str) -> ExtractedRelation:
    return ExtractedRelation(source_id, target_id, RelationType.REQUIRES, "requires")


@pytest.mark.asyncio
async def test_persist_results_keeps_good_entities_after_a_bad_entity(
    conn: asyncpg.Connection,
) -> None:
    pipeline = _pipeline(conn)
    result = ExtractionResult(
        chunk_id="X0000001",
        entities=[
            _entity("900001", "Person One"),
            _entity("900002", "Person\x00Two"),
            _entity("900003", "Person Three"),
        ],
    )

    await pipeline.persist_results([result])

    assert await conn.fetchval("SELECT count(*) FROM kg_nodes") == 2
    assert pipeline.stats.entities_persisted == 2


@pytest.mark.asyncio
async def test_persist_results_retries_relation_after_its_failed_insert(
    conn: asyncpg.Connection,
) -> None:
    pipeline = _pipeline(conn)
    relation = _relation("900001", "900002")

    await pipeline.persist_results(
        [
            ExtractionResult(
                "X0000001", entities=[_entity("900001", "Person One")], relations=[relation]
            )
        ]
    )

    relation_id = f"900001_{RelationType.REQUIRES.value}_900002"
    assert relation_id not in pipeline.relation_registry
    await conn.execute(
        """
        INSERT INTO kg_nodes (
            entity_id, entity_type, name, description, properties, confidence,
            source_collection, source_chunk_ids, created_at, updated_at
        ) VALUES ('900002', 'kitas', 'Person Two', 'Person Two', '{}', 0.8,
                  'legal_unified_hybrid', ARRAY['X0000001'], NOW(), NOW())
        """
    )

    await pipeline.persist_results([ExtractionResult("X0000001", relations=[relation])])

    assert await conn.fetchval("SELECT count(*) FROM kg_edges") == 1
    assert relation_id in pipeline.relation_registry


@pytest.mark.asyncio
async def test_persist_results_does_not_bump_version_when_every_write_fails(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services.knowledge_graph import pipeline as pipeline_module

    bumps: list[bool] = []
    monkeypatch.setattr(pipeline_module, "increment_kg_version", lambda: bumps.append(True))
    pipeline = _pipeline(conn)

    await pipeline.persist_results(
        [ExtractionResult("X0000001", entities=[_entity("900001", "Person\x00One")])]
    )

    assert await conn.fetchval("SELECT count(*) FROM kg_nodes") == 0
    assert pipeline.stats.entities_persisted == 0
    assert bumps == []


@pytest.mark.asyncio
async def test_persist_results_clean_batch_keeps_existing_counts_and_bumps_once(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services.knowledge_graph import pipeline as pipeline_module

    bumps: list[bool] = []
    monkeypatch.setattr(pipeline_module, "increment_kg_version", lambda: bumps.append(True))
    pipeline = _pipeline(conn)

    await pipeline.persist_results(
        [
            ExtractionResult(
                "X0000001",
                entities=[_entity("900001", "Person One"), _entity("900002", "Person Two")],
                relations=[_relation("900001", "900002")],
            )
        ]
    )

    assert await conn.fetchval("SELECT count(*) FROM kg_nodes") == 2
    assert await conn.fetchval("SELECT count(*) FROM kg_edges") == 1
    assert pipeline.stats.entities_persisted == 2
    assert pipeline.stats.relations_persisted == 1
    assert bumps == [True]
