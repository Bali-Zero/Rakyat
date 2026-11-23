"""extract-visa names a document only when it is this client's live record of the file (real PostgreSQL).

Uses the standard ``db_tx`` fixture: everything, DDL included, is rolled back. Synthetic data only.
The predicate is the route's own SQL, `_CLIENT_DOCUMENT_OF_FILE_SQL`, executed as is.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")

from backend.app.routers.crm_enhanced_documents import _CLIENT_DOCUMENT_OF_FILE_SQL  # noqa: E402

pytestmark = pytest.mark.integration

FILE = "F1aaaaaaaaaa"
SCHEMA = """
CREATE TABLE documents (
    id int PRIMARY KEY,
    client_id int,
    file_id text,
    google_drive_file_url text,
    file_url text,
    status text,
    deleted_at timestamptz
);
"""
ROWS = [
    (1, 1, FILE, None, None, "active", None),
    (2, 1, None, f"https://drive.google.com/file/d/{FILE}/view", None, None, None),
    (3, 1, None, None, f"https://drive.google.com/open?id={FILE}", "active", None),
    (4, 2, FILE, None, None, "active", None),
    (5, 1, "OTHERfile123", None, None, "active", None),
    (6, 1, FILE, None, None, "active", "2026-11-01T00:00:00Z"),
    (7, 1, FILE, None, None, "deleted", None),
    (8, 1, None, f"https://drive.google.com/file/d/{FILE}XYZ/view", None, "active", None),
]


@pytest_asyncio.fixture
async def conn(db_tx: asyncpg.Connection) -> asyncpg.Connection:
    # A schema of its own inside the test transaction (rolled back with it): the
    # predicate reads only these columns, and no real row is involved.
    await db_tx.execute("CREATE SCHEMA extract_visa_doc_test")
    await db_tx.execute("SET LOCAL search_path = extract_visa_doc_test")
    await db_tx.execute(SCHEMA)
    await db_tx.executemany(
        "INSERT INTO documents VALUES ($1, $2, $3, $4, $5, $6, $7::text::timestamptz)", ROWS
    )
    return db_tx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc_id",
    [1, 2, 3],
    ids=["file_id", "drive-url-d", "file-url-id"],
)
async def test_the_clients_live_record_of_the_file_is_accepted(conn, doc_id):
    assert await conn.fetchval(_CLIENT_DOCUMENT_OF_FILE_SQL, doc_id, 1, FILE) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc_id",
    [4, 5, 6, 7, 8, 999],
    ids=[
        "another-client",
        "another-file",
        "deleted-by-the-client",
        "deleted-by-the-team",
        "longer-id-in-the-url",
        "no-such-document",
    ],
)
async def test_any_other_document_is_refused(conn, doc_id):
    assert await conn.fetchval(_CLIENT_DOCUMENT_OF_FILE_SQL, doc_id, 1, FILE) is None
