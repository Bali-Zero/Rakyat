"""Run the actual authorization SQL and consumers against synthetic TEMP tables."""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio

from backend.app.routers import documents_proxy as mod


@pytest_asyncio.fixture
async def conn():
    target = urlsplit(os.environ["TEST_DATABASE_URL"])
    if target.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in target.path:
        pytest.skip("requires a local test database")
    db = await asyncpg.connect(os.environ["TEST_DATABASE_URL"], timeout=3)
    try:
        await db.execute("SET search_path TO pg_temp")
        await db.execute("""
            CREATE TEMP TABLE clients (id int, status text, deleted_at timestamptz,
                google_drive_folder_id text, drive_folder_id text);
            CREATE TEMP TABLE companies (id int, google_drive_folder_id text, tax_dept_folder_id text);
            CREATE TEMP TABLE documents (client_id int, file_id text, google_drive_file_url text,
                file_url text, client_visible boolean DEFAULT true, deleted_at timestamptz,
                is_archived boolean DEFAULT false, status text);
            CREATE TEMP TABLE company_documents (company_id int, google_drive_file_id text,
                google_drive_file_url text);
            CREATE TEMP TABLE invoices (client_id int, drive_file_id text);
            CREATE TEMP TABLE practices (client_id int, documents jsonb);
            CREATE TEMP TABLE lkpm_receipts (file_drive_id text);
            CREATE TEMP TABLE weekly_cashout (receipt_drive_file_id text);
            CREATE TEMP TABLE team_members (id text, email text, role text, active boolean,
                portal_access boolean, linked_client_id int);
            INSERT INTO clients VALUES (1, 'active', NULL, 'shared-root', NULL);
            INSERT INTO team_members VALUES
                ('tm-portal-0001', 'person.one@example.test', 'client', true, true, 1);
        """)
        yield db
    finally:
        await db.close()


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def drive(monkeypatch, parents=None):
    token = AsyncMock(return_value="synthetic-token")
    monkeypatch.setattr(mod, "_get_drive_access_token", token)
    real_client = httpx.AsyncClient

    def respond(request):
        if request.url.params.get("alt") == "media" or request.url.host == "thumbnail.invalid":
            return httpx.Response(200, content=b"synthetic-image")
        return httpx.Response(
            200,
            json={
                "parents": parents or [],
                "mimeType": "image/jpeg",
                "thumbnailLink": "https://thumbnail.invalid/image=s400",
            },
        )

    monkeypatch.setattr(
        mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    return token


@pytest.mark.asyncio
@pytest.mark.parametrize("registered_owner", [None, 2])
async def test_shared_root_cannot_authorize_new_or_foreign_file(
    conn, monkeypatch, registered_owner
):
    await conn.execute("INSERT INTO clients VALUES (2, 'archived', NULL, 'shared-root', NULL)")
    if registered_owner:
        await conn.execute(
            "INSERT INTO documents(client_id,file_id) VALUES ($1,'fileA0001Z')", registered_owner
        )
    drive(monkeypatch, ["shared-root"])
    with pytest.raises(mod.HTTPException) as exc:
        await mod.assert_drive_file_belongs_to_client("fileA0001Z", 1, conn)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_archived_owner_denied_before_token_even_for_own_file(conn, monkeypatch):
    await conn.execute("UPDATE clients SET status='archived'")
    await conn.execute("INSERT INTO documents(client_id,file_id) VALUES (1,'fileA0001Z')")
    token = drive(monkeypatch, ["shared-root"])
    with pytest.raises(mod.HTTPException) as exc:
        await mod.assert_drive_file_belongs_to_client("fileA0001Z", 1, conn)
    assert exc.value.status_code == 403
    token.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_registered_file_survives_shared_historical_root(conn, monkeypatch):
    await conn.execute("INSERT INTO clients VALUES (2, 'archived', NULL, 'shared-root', NULL)")
    await conn.execute("INSERT INTO documents(client_id,file_id) VALUES (1,'fileA0001Z')")
    token = drive(monkeypatch)
    await mod.assert_drive_file_belongs_to_client("fileA0001Z", 1, conn)
    token.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_unregistered_file_under_unique_root_allowed(conn, monkeypatch):
    drive(monkeypatch, ["shared-root"])
    await mod.assert_drive_file_belongs_to_client("fileA0001Z", 1, conn)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hidden",
    [
        "client_visible=false",
        "client_visible=NULL",
        "is_archived=true",
        "deleted_at=now()",
        "status='deleted'",
    ],
)
@pytest.mark.parametrize("handler", [mod.proxy_drive_file, mod.get_drive_thumbnail])
async def test_portal_hidden_document_denied_before_token(conn, monkeypatch, hidden, handler):
    await conn.execute("INSERT INTO documents(client_id,file_id) VALUES (1,'fileA0001Z')")
    await conn.execute(f"UPDATE documents SET {hidden}")
    token = drive(monkeypatch)
    with pytest.raises(mod.HTTPException) as exc:
        await handler(
            "fileA0001Z",
            current_user={"role": "client", "user_id": "tm-portal-0001", "client_id": 1},
            db_pool=Pool(conn),
        )
    assert exc.value.status_code == 403
    token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", ["documents", "invoices"])
@pytest.mark.parametrize("handler", [mod.proxy_drive_file, mod.get_drive_thumbnail])
async def test_portal_visible_document_and_invoice_allowed(conn, monkeypatch, registry, handler):
    column = "file_id" if registry == "documents" else "drive_file_id"
    await conn.execute(f"INSERT INTO {registry}(client_id,{column}) VALUES (1,'fileA0001Z')")
    token = drive(monkeypatch)
    response = await handler(
        "fileA0001Z",
        current_user={"role": "client", "user_id": "tm-portal-0001", "client_id": 1},
        db_pool=Pool(conn),
    )
    assert response.status_code == 200
    assert response.body == b"synthetic-image"
    token.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [mod.proxy_drive_file, mod.get_drive_thumbnail])
async def test_staff_global_read_still_allows_hidden_foreign_document(conn, monkeypatch, handler):
    await conn.execute(
        "INSERT INTO documents(client_id,file_id,client_visible,is_archived) VALUES (2,'fileA0001Z',false,true)"
    )
    drive(monkeypatch)
    response = await handler(
        "fileA0001Z", current_user={"role": "admin", "client_id": 1}, db_pool=Pool(conn)
    )
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registration",
    [
        "INSERT INTO documents(client_id,file_id) VALUES (2,'fileA0001Z')",
        "INSERT INTO documents(client_id,google_drive_file_url) VALUES (2,'https://drive.invalid/file/d/fileA0001Z/view')",
        "INSERT INTO documents(client_id,file_url) VALUES (2,'https://drive.invalid/open?id=fileA0001Z')",
        "INSERT INTO invoices(client_id,drive_file_id) VALUES (2,'fileA0001Z')",
        'INSERT INTO practices(client_id,documents) VALUES (2,\'{"invoice":{"invoice_drive_id":"fileA0001Z"}}\')',
        "INSERT INTO company_documents(company_id,google_drive_file_id) VALUES (3,'fileA0001Z')",
        "INSERT INTO lkpm_receipts(file_drive_id) VALUES ('fileA0001Z')",
        "INSERT INTO weekly_cashout(receipt_drive_file_id) VALUES ('fileA0001Z')",
    ],
)
async def test_conflicting_registry_denied_even_when_own_registry_and_root_match(
    conn, monkeypatch, registration
):
    await conn.execute("INSERT INTO documents(client_id,file_id) VALUES (1,'fileA0001Z')")
    await conn.execute(registration)
    token = drive(monkeypatch, ["shared-root"])
    with pytest.raises(mod.HTTPException) as exc:
        await mod.assert_drive_file_belongs_to_client("fileA0001Z", 1, conn)
    assert exc.value.status_code == 403
    token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://drive.invalid/file/d/fileA0001Z/view",
        "https://drive.invalid/open?id=fileA0001Z&export=1",
    ],
)
async def test_own_url_registry_requires_exact_id(conn, monkeypatch, url):
    await conn.execute("INSERT INTO documents(client_id,file_url) VALUES (1,$1)", url)
    token = drive(monkeypatch)
    await mod.assert_drive_file_belongs_to_client("fileA0001Z", 1, conn)
    token.assert_not_awaited()
    await conn.execute(
        "UPDATE documents SET file_url=replace(file_url,'fileA0001Z','fileA0001Zsuffix')"
    )
    with pytest.raises(mod.HTTPException) as exc:
        await mod.assert_drive_file_belongs_to_client("fileA0001Z", 1, conn)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_company_root_reference_prevents_individual_ancestry_grant(conn, monkeypatch):
    await conn.execute("INSERT INTO companies VALUES (5,'shared-root',NULL)")
    drive(monkeypatch, ["shared-root"])
    with pytest.raises(mod.HTTPException) as exc:
        await mod.assert_drive_file_belongs_to_client("fileA0001Z", 1, conn)
    assert exc.value.status_code == 403
