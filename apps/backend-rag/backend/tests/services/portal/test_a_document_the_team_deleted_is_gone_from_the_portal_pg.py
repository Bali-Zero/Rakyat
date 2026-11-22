"""A document the team deleted is gone from the client's portal.

The team's delete route writes ``status = 'deleted'`` and nothing else. The vault's own methods
run here against a real PostgreSQL table carrying the four flags a document can be hidden by.
Invented rows only.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.portal._document_visibility import document_visibility_clause
from backend.services.portal._mixins import documents as documents_module
from backend.services.portal.portal_service import PortalService

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_CLIENT = 900001
_USER = {"client_id": _CLIENT, "email": "person.one@example.test"}


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

_TABLES = """
CREATE TABLE practice_types (id serial PRIMARY KEY, name text);
CREATE TABLE practices (id serial PRIMARY KEY, practice_type_id integer);
CREATE TABLE documents (
    id serial PRIMARY KEY,
    client_id integer NOT NULL,
    practice_id integer,
    document_type text,
    document_purpose text,
    file_name text,
    file_id text,
    file_url text,
    file_size_kb integer,
    mime_type text,
    storage_type text,
    status text,
    expiry_date date,
    client_visible boolean NOT NULL DEFAULT true,
    is_archived boolean,
    deleted_at timestamptz,
    deleted_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


@pytest_asyncio.fixture
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    admin = await asyncpg.connect(_DB_URL)
    schema = "portal_team_deleted_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=3, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as c:
            await c.execute(_TABLES)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.fixture
def service(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> PortalService:
    made = PortalService(pool)
    monkeypatch.setattr(made, "_record_timeline_event", AsyncMock())
    fetched = SimpleNamespace(
        content=b"synthetic", file_name="doc.pdf", mime_type="application/pdf"
    )
    monkeypatch.setattr(documents_module, "fetch_drive_file", AsyncMock(return_value=fetched))
    return made


async def _document(pool: asyncpg.Pool, **columns: Any) -> int:
    row: dict[str, Any] = {
        "client_id": _CLIENT,
        "document_type": "passport",
        "file_name": "doc.pdf",
        "file_id": "synthetic-file-id",
    }
    row.update(columns)
    names = ", ".join(row)
    marks = ", ".join(f"${i}" for i in range(1, len(row) + 1))
    async with pool.acquire() as c:
        return await c.fetchval(
            f"INSERT INTO documents ({names}) VALUES ({marks}) RETURNING id", *row.values()
        )


async def _listed(service: PortalService) -> set[int]:
    return {d["id"] for d in await service.get_documents(_CLIENT, current_user=_USER)}


@pytest.mark.asyncio
async def test_a_team_deleted_document_is_not_listed(
    service: PortalService, pool: asyncpg.Pool
) -> None:
    kept = await _document(pool, status="verified")
    deleted = await _document(pool, status="deleted")

    assert await _listed(service) == {kept}, f"document {deleted} is still in the vault"


@pytest.mark.asyncio
async def test_a_team_deleted_document_cannot_be_downloaded(
    service: PortalService, pool: asyncpg.Pool
) -> None:
    deleted = await _document(pool, status="deleted")

    assert await service.download_document(_CLIENT, deleted, current_user=_USER) is None


@pytest.mark.asyncio
async def test_a_team_deleted_document_cannot_be_restored_from_the_clients_trash(
    service: PortalService, pool: asyncpg.Pool
) -> None:
    """The client trashed it first; the team deleted it afterwards."""
    deleted = await _document(pool, status="deleted")
    async with pool.acquire() as c:
        await c.execute("UPDATE documents SET deleted_at = now() WHERE id = $1", deleted)

    assert await service.restore_document(_CLIENT, deleted, current_user=_USER) is None
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT deleted_at FROM documents WHERE id = $1", deleted)
    assert await _listed(service) == set()


@pytest.mark.asyncio
async def test_a_team_deleted_document_cannot_be_trashed_by_the_client(
    service: PortalService, pool: asyncpg.Pool
) -> None:
    deleted = await _document(pool, status="deleted")

    assert await service.soft_delete_document(_CLIENT, deleted, current_user=_USER) is None
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT deleted_at FROM documents WHERE id = $1", deleted) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "", "verified", "pending_review", "DELETED"])
async def test_every_other_status_reads_as_before(
    service: PortalService, pool: asyncpg.Pool, status: str | None
) -> None:
    """Only the exact value the team's delete route writes hides a document."""
    document = await _document(pool, status=status)

    assert await _listed(service) == {document}
    downloaded = await service.download_document(_CLIENT, document, current_user=_USER)
    assert downloaded is not None and downloaded["content"] == b"synthetic"


@pytest.mark.asyncio
async def test_a_document_the_client_trashed_is_restored_as_before(
    service: PortalService, pool: asyncpg.Pool
) -> None:
    document = await _document(pool, status="verified")

    assert await service.soft_delete_document(_CLIENT, document, current_user=_USER) is not None
    assert await _listed(service) == set()
    assert await service.restore_document(_CLIENT, document, current_user=_USER) is not None
    assert await _listed(service) == {document}


@pytest.mark.asyncio
async def test_the_other_three_flags_hide_a_document_as_before(
    service: PortalService, pool: asyncpg.Pool
) -> None:
    await _document(pool, client_visible=False)
    await _document(pool, is_archived=True)
    trashed = await _document(pool)
    async with pool.acquire() as c:
        await c.execute("UPDATE documents SET deleted_at = now() WHERE id = $1", trashed)

    assert await _listed(service) == set()


@pytest.mark.parametrize("alias", ["", "d"])
def test_the_shared_predicate_carries_the_condition_with_its_alias(alias: str) -> None:
    prefix = f"{alias}." if alias else ""

    clause = document_visibility_clause(alias)

    assert f"COALESCE({prefix}status, '') <> 'deleted'" in clause
    assert f"{prefix}deleted_at IS NULL" in clause
    assert f"{prefix}client_visible = true" in clause
    assert f"COALESCE({prefix}is_archived, FALSE) = FALSE" in clause
