"""Real-PostgreSQL coverage for relinking soft-deleted graph nodes."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from backend.services.knowledge_graph.document_linker import _upsert_node

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    # The suite gives every xdist worker its own database, `<base>_gw<N>`.
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = pytest.mark.skipif(
    not _is_safe_test_database(_DB_URL),
    reason="TEST_DATABASE_URL must be a localhost database named test or ending in _test",
)


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    assert _DB_URL is not None
    schema = f"document_linker_relinks_{uuid.uuid4().hex[:8]}"
    connection = await asyncpg.connect(_DB_URL)
    created = False
    try:
        await connection.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        await connection.execute(f'SET search_path TO "{schema}"')
        await connection.execute(
            """
            CREATE TABLE crm_kg_nodes (
                entity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                entity_type TEXT NOT NULL CHECK (entity_type IN (
                    'crm_client', 'crm_document', 'crm_practice', 'crm_person',
                    'crm_company', 'crm_property'
                )),
                name TEXT NOT NULL,
                properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                file_id TEXT,
                client_id INTEGER,
                practice_id INTEGER,
                person_uid UUID,
                company_uid UUID,
                confidence FLOAT NOT NULL DEFAULT 1.0
                    CHECK (confidence >= 0.0 AND confidence <= 1.0),
                deleted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE UNIQUE INDEX idx_crm_kg_nodes_file_id ON crm_kg_nodes (file_id)
                WHERE file_id IS NOT NULL;
            CREATE UNIQUE INDEX idx_crm_kg_nodes_client_id ON crm_kg_nodes (client_id)
                WHERE client_id IS NOT NULL;
            CREATE UNIQUE INDEX idx_crm_kg_nodes_practice_id ON crm_kg_nodes (practice_id)
                WHERE practice_id IS NOT NULL;
            CREATE UNIQUE INDEX idx_crm_kg_nodes_person_uid ON crm_kg_nodes (person_uid)
                WHERE person_uid IS NOT NULL;
            CREATE UNIQUE INDEX idx_crm_kg_nodes_company_uid ON crm_kg_nodes (company_uid)
                WHERE company_uid IS NOT NULL;
            """
        )
        yield connection
    finally:
        if created:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.asyncio
async def test_upsert_node_resurrects_a_soft_deleted_document_node(
    conn: asyncpg.Connection,
) -> None:
    entity_id = await _upsert_node(
        conn,
        entity_type="crm_document",
        name="Person One",
        properties={"source": "old", "preserved": "value"},
        file_id="file-900001",
    )
    await conn.execute("UPDATE crm_kg_nodes SET deleted_at = NOW() WHERE entity_id = $1", entity_id)

    relinked_id = await _upsert_node(
        conn,
        entity_type="crm_document",
        name="Person One restored",
        properties={"source": "new"},
        file_id="file-900001",
    )

    row = await conn.fetchrow(
        "SELECT deleted_at, properties FROM crm_kg_nodes WHERE entity_id = $1", entity_id
    )
    assert relinked_id == entity_id
    assert row is not None
    assert row["deleted_at"] is None
    assert row["properties"] == {"source": "new", "preserved": "value"}


@pytest.mark.asyncio
async def test_upsert_node_resurrects_a_soft_deleted_client_node(
    conn: asyncpg.Connection,
) -> None:
    entity_id = await _upsert_node(
        conn,
        entity_type="crm_client",
        name="Person One",
        properties={"status": "archived", "preserved": "value"},
        client_id=900001,
    )
    await conn.execute("UPDATE crm_kg_nodes SET deleted_at = NOW() WHERE entity_id = $1", entity_id)

    relinked_id = await _upsert_node(
        conn,
        entity_type="crm_client",
        name="Person One restored",
        properties={"status": "active"},
        client_id=900001,
    )

    row = await conn.fetchrow(
        "SELECT deleted_at, properties FROM crm_kg_nodes WHERE entity_id = $1", entity_id
    )
    assert relinked_id == entity_id
    assert row is not None
    assert row["deleted_at"] is None
    assert row["properties"] == {"status": "active", "preserved": "value"}


@pytest.mark.asyncio
async def test_upsert_node_inserts_new_key_and_updates_live_node_in_place(
    conn: asyncpg.Connection,
) -> None:
    entity_id = await _upsert_node(
        conn,
        entity_type="crm_document",
        name="Person One",
        properties={"source": "first"},
        file_id="file-900002",
    )
    updated_id = await _upsert_node(
        conn,
        entity_type="crm_document",
        name="Person One updated",
        properties={"status": "verified"},
        file_id="file-900002",
    )

    assert updated_id == entity_id
    assert await conn.fetchval("SELECT count(*) FROM crm_kg_nodes") == 1
    row = await conn.fetchrow(
        "SELECT deleted_at, properties FROM crm_kg_nodes WHERE entity_id = $1", entity_id
    )
    assert row is not None
    assert row["deleted_at"] is None
    assert row["properties"] == {"source": "first", "status": "verified"}
