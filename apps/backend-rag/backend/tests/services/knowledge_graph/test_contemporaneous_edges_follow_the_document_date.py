"""PostgreSQL coverage for CONTEMPORANEOUS document-date matching."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.knowledge_graph import mediated_edges_builder

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


@pytest_asyncio.fixture
async def kg_db():
    conn = await asyncpg.connect(_DB_URL)
    schema = f"kg_contemporaneous_{uuid4().hex}"
    await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        await conn.execute(f"SET search_path TO {schema}")
        await conn.execute(
            """
            CREATE TABLE documents (
                file_id TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE crm_kg_nodes (
                entity_id UUID PRIMARY KEY,
                entity_type TEXT NOT NULL CHECK (entity_type IN ('crm_client', 'crm_document')),
                name TEXT NOT NULL,
                file_id TEXT UNIQUE,
                deleted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE crm_kg_edges (
                source_entity_id UUID NOT NULL REFERENCES crm_kg_nodes(entity_id),
                target_entity_id UUID NOT NULL REFERENCES crm_kg_nodes(entity_id),
                relationship_type TEXT NOT NULL,
                properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                edge_tier TEXT NOT NULL CHECK (edge_tier IN ('direct', 'mediated', 'thematic')),
                confidence FLOAT NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                UNIQUE (source_entity_id, target_entity_id, relationship_type)
            );
            """
        )
        yield conn
    finally:
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


async def _add_document(
    conn: asyncpg.Connection,
    *,
    client_id: str,
    file_id: str,
    document_date: datetime,
    node_date: datetime,
    deleted: bool = False,
    document_id: str | None = None,
) -> tuple[str, str]:
    document_id = document_id or str(uuid4())
    await conn.execute(
        "INSERT INTO documents (file_id, created_at) VALUES ($1, $2)",
        file_id,
        document_date,
    )
    await conn.execute(
        """
        INSERT INTO crm_kg_nodes
            (entity_id, entity_type, name, file_id, deleted_at, created_at)
        VALUES ($1, 'crm_document', $2, $3, $4, $5)
        """,
        document_id,
        f"Synthetic {file_id}",
        file_id,
        node_date if deleted else None,
        node_date,
    )
    await conn.execute(
        """
        INSERT INTO crm_kg_edges
            (source_entity_id, target_entity_id, relationship_type, edge_tier, confidence)
        VALUES ($1, $2, 'BELONGS_TO', 'direct', 1.0)
        ON CONFLICT DO NOTHING
        """,
        document_id,
        client_id,
    )
    return document_id, client_id


async def _add_client(conn: asyncpg.Connection, name: str) -> str:
    client_id = str(uuid4())
    await conn.execute(
        "INSERT INTO crm_kg_nodes (entity_id, entity_type, name) VALUES ($1, 'crm_client', $2)",
        client_id,
        name,
    )
    return client_id


async def _contemporaneous_count(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM crm_kg_edges WHERE relationship_type = 'CONTEMPORANEOUS'"
    )


@pytest.mark.asyncio
async def test_documents_six_months_apart_are_not_contemporaneous_when_backfilled(kg_db):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    client = await _add_client(kg_db, "Synthetic Client A")
    await _add_document(
        kg_db,
        client_id=client,
        file_id="synthetic-a",
        document_date=base,
        node_date=base + timedelta(days=365),
    )
    await _add_document(
        kg_db,
        client_id=client,
        file_id="synthetic-b",
        document_date=base + timedelta(days=183),
        node_date=base + timedelta(days=365, minutes=1),
    )

    assert await mediated_edges_builder._emit_contemporaneous(kg_db) == 0
    assert await _contemporaneous_count(kg_db) == 0


@pytest.mark.asyncio
async def test_documents_two_days_apart_are_contemporaneous_despite_node_write_gap(kg_db):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    client = await _add_client(kg_db, "Synthetic Client B")
    await _add_document(
        kg_db,
        client_id=client,
        file_id="synthetic-c",
        document_date=base,
        node_date=base,
    )
    await _add_document(
        kg_db,
        client_id=client,
        file_id="synthetic-d",
        document_date=base + timedelta(days=2),
        node_date=base + timedelta(days=180),
    )

    assert await mediated_edges_builder._emit_contemporaneous(kg_db) == 1
    assert await _contemporaneous_count(kg_db) == 1


@pytest.mark.asyncio
async def test_existing_pair_does_not_spend_contemporaneous_pass_limit(kg_db, monkeypatch):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    first_client = await _add_client(kg_db, "Synthetic Client C")
    first, _ = await _add_document(
        kg_db,
        client_id=first_client,
        file_id="synthetic-e",
        document_date=base,
        node_date=base,
        document_id="00000000-0000-0000-0000-000000000001",
    )
    second, _ = await _add_document(
        kg_db,
        client_id=first_client,
        file_id="synthetic-f",
        document_date=base,
        node_date=base,
        document_id="00000000-0000-0000-0000-000000000002",
    )
    new_client = await _add_client(kg_db, "Synthetic Client D")
    third, _ = await _add_document(
        kg_db,
        client_id=new_client,
        file_id="synthetic-g",
        document_date=base,
        node_date=base,
        document_id="00000000-0000-0000-0000-000000000003",
    )
    fourth, _ = await _add_document(
        kg_db,
        client_id=new_client,
        file_id="synthetic-h",
        document_date=base,
        node_date=base,
        document_id="00000000-0000-0000-0000-000000000004",
    )
    old_source, old_target = sorted((first, second))
    await kg_db.execute(
        """
        INSERT INTO crm_kg_edges
            (source_entity_id, target_entity_id, relationship_type, edge_tier, confidence)
        VALUES ($1, $2, 'CONTEMPORANEOUS', 'mediated', 0.85)
        """,
        old_source,
        old_target,
    )
    monkeypatch.setattr(mediated_edges_builder, "_MAX_EDGES_PER_PASS", 1)
    await kg_db.execute("SET enable_hashagg TO off")

    assert await mediated_edges_builder._emit_contemporaneous(kg_db) == 1
    new_source, new_target = sorted((third, fourth))
    assert (
        await kg_db.fetchval(
            """
        SELECT count(*) FROM crm_kg_edges
        WHERE source_entity_id = $1 AND target_entity_id = $2
          AND relationship_type = 'CONTEMPORANEOUS'
        """,
            new_source,
            new_target,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_only_live_documents_of_the_same_client_are_paired_and_repeat_is_noop(kg_db):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    first_client = await _add_client(kg_db, "Synthetic Client E")
    second_client = await _add_client(kg_db, "Synthetic Client F")
    await _add_document(
        kg_db,
        client_id=first_client,
        file_id="synthetic-i",
        document_date=base,
        node_date=base,
    )
    await _add_document(
        kg_db,
        client_id=second_client,
        file_id="synthetic-j",
        document_date=base,
        node_date=base,
    )
    await _add_document(
        kg_db,
        client_id=first_client,
        file_id="synthetic-k",
        document_date=base,
        node_date=base,
        deleted=True,
    )

    assert await mediated_edges_builder._emit_contemporaneous(kg_db) == 0
    assert await _contemporaneous_count(kg_db) == 0

    await _add_document(
        kg_db,
        client_id=first_client,
        file_id="synthetic-l",
        document_date=base,
        node_date=base,
    )
    assert await mediated_edges_builder._emit_contemporaneous(kg_db) == 1
    assert await mediated_edges_builder._emit_contemporaneous(kg_db) == 0
    assert await _contemporaneous_count(kg_db) == 1


@pytest.mark.asyncio
async def test_a_node_whose_file_has_no_document_row_is_never_paired(kg_db):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    client = await _add_client(kg_db, "Synthetic Client E")
    await _add_document(
        kg_db,
        client_id=client,
        file_id="synthetic-with-row",
        document_date=base,
        node_date=base,
    )
    orphan, _ = await _add_document(
        kg_db,
        client_id=client,
        file_id="synthetic-without-row",
        document_date=base,
        node_date=base,
    )
    await kg_db.execute("DELETE FROM documents WHERE file_id = 'synthetic-without-row'")

    assert await mediated_edges_builder._emit_contemporaneous(kg_db) == 0
    assert await _contemporaneous_count(kg_db) == 0
    assert orphan
