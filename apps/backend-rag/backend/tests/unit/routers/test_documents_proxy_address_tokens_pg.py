"""The file registries match a document address by its exact Drive id token.

Runs the two registry SQL constants themselves against TEMP tables that shadow every table
they name, with the explicit-id columns empty so only the address branch under test can admit
(one test fills each explicit-id column instead). The last test checks the id shape the folder
walk applies before it asks Drive for a folder's parents.
"""

import os
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers import documents_proxy as mod

X = "abcdefghijK"
Y = "ZYXWVUTSRQp"
Z = "abc_def-ghiK"  # both punctuation characters of the run

# (reader, table, column): G1, G2, G3 for the global reader; P1, P2 for the portal reader.
COLUMNS = [
    ("global", "documents", "google_drive_file_url"),
    ("global", "documents", "file_url"),
    ("global", "company_documents", "google_drive_file_url"),
    ("portal", "documents", "google_drive_file_url"),
    ("portal", "documents", "file_url"),
]
IDS = [f"{r}:{t}.{c}" for r, t, c in COLUMNS]


@pytest_asyncio.fixture
async def conn():
    target = urlsplit(os.environ["TEST_DATABASE_URL"])
    if target.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in target.path:
        pytest.skip("requires a local test database")
    db = await asyncpg.connect(os.environ["TEST_DATABASE_URL"], timeout=3)
    try:
        await db.execute("SET search_path TO pg_temp")
        await db.execute("""
            CREATE TEMP TABLE documents (client_id int, file_id text, google_drive_file_url text,
                file_url text, client_visible boolean DEFAULT true, deleted_at timestamptz,
                is_archived boolean DEFAULT false, status text);
            CREATE TEMP TABLE company_documents (company_id int, google_drive_file_id text,
                google_drive_file_url text);
            CREATE TEMP TABLE invoices (client_id int, drive_file_id text);
            CREATE TEMP TABLE lkpm_receipts (file_drive_id text);
            CREATE TEMP TABLE weekly_cashout (receipt_drive_file_id text);
        """)
        yield db
    finally:
        await db.close()


async def _ask(conn, reader, requested):
    if reader == "global":
        row = await conn.fetchrow(mod._REGISTERED_FILE_SQL, requested)
    else:
        row = await conn.fetchrow(mod._CLIENT_REGISTERED_FILE_SQL, requested, 1)
    return row is not None


async def _store(conn, table, **cols):
    await conn.execute("DELETE FROM documents; DELETE FROM company_documents")
    names = ", ".join(cols)
    marks = ", ".join(f"${i + 1}" for i in range(len(cols)))
    extra = ("client_id", "1") if table == "documents" else ("company_id", "1")
    await conn.execute(
        f"INSERT INTO {table}({extra[0]}, {names}) VALUES ({extra[1]}, {marks})", *cols.values()
    )


async def _admits(conn, case, value, requested):
    reader, table, col = case
    await _store(conn, table, **{col: value})
    return await _ask(conn, reader, requested)


@pytest.mark.parametrize("case", COLUMNS, ids=IDS)
async def test_pa1_exact_token_prefix_and_suffix_in_every_column(conn, case):
    url = f"https://drive.google.com/file/d/{X}/view"
    assert await _admits(conn, case, url, X)
    assert not await _admits(conn, case, url, X[:-1])
    assert not await _admits(conn, case, url, X[1:])
    assert not await _admits(conn, case, None, X)


ADMIT_FORMS = [
    f"https://x.example.test/p?id={X}",
    f"https://x.example.test/p?a=1&id={X}",
    f"https://drive.google.com/file/d/{X}/view",
    f"https://drive.google.com/open?id={X}",
    f"https://drive.google.com/file/d/{X}?usp=sharing",
    # an empty run first: the read side takes the first NON-empty run
    f"https://drive.google.com/file/d//d/{X}/view",
    f"https://x.example.test/p?id=&id={X}",
]
REFUSE_FORMS = [
    f"https://x.example.test/p?ouid={X}",
    f"https://x.example.test/s#gid={X}",
    f"https://x.example.test/p?xid={X}",
    f"https://x.example.test/p?ID={X}",
    f"https://drive.google.com/file/D/{X}/view",
    f"https://x.example.test/%2Fd%2F{X}",
    f"https://x.example.test/abcd/{X}",
    f"https://x.example.test/d/a/d/{X}",
    f"https://x.example.test/open?id=a&id={X}",
]


@pytest.mark.parametrize("case", COLUMNS, ids=IDS)
@pytest.mark.parametrize(
    "url", [f"https://drive.google.com/file/d/{Z}/view", f"https://drive.google.com/open?id={Z}"]
)
async def test_an_id_with_underscore_and_hyphen_is_read_whole(conn, case, url):
    assert await _admits(conn, case, url, Z)
    assert not await _admits(conn, case, url, Z[:3])


@pytest.mark.parametrize("case", COLUMNS, ids=IDS)
async def test_pa2_forms_in_every_column(conn, case):
    for url in ADMIT_FORMS:
        assert await _admits(conn, case, url, X), url
    for url in REFUSE_FORMS:
        assert not await _admits(conn, case, url, X), url


@pytest.mark.parametrize("case", COLUMNS, ids=IDS)
async def test_pa3_first_match_per_column_and_pattern(conn, case):
    base = "https://x.example.test"
    both = f"{base}/d/{X}/edit?id={Y}"
    assert await _admits(conn, case, both, X) and await _admits(conn, case, both, Y)
    path_twice = f"{base}/d/{X}/d/{Y}"
    assert await _admits(conn, case, path_twice, X)
    assert not await _admits(conn, case, path_twice, Y)
    query_twice = f"{base}/p?id={X}&id={Y}"
    assert await _admits(conn, case, query_twice, X)
    assert not await _admits(conn, case, query_twice, Y)


@pytest.mark.parametrize("reader", ["global", "portal"])
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (f"https://x.example.test/d/{X}", f"https://x.example.test/d/{Y}"),
        (f"https://x.example.test/d/{X}", f"https://x.example.test/p?id={Y}"),
    ],
)
async def test_pa3_two_documents_columns_of_one_row(conn, reader, first, second):
    await conn.execute("DELETE FROM documents; DELETE FROM company_documents")
    await conn.execute(
        "INSERT INTO documents(client_id, google_drive_file_url, file_url) VALUES (1, $1, $2)",
        first,
        second,
    )
    assert await _ask(conn, reader, X) and await _ask(conn, reader, Y)


@pytest.mark.parametrize("case", COLUMNS, ids=IDS)
async def test_pa4_all_digit_run_is_read_like_any_run(conn, case):
    assert await _admits(conn, case, "https://x.example.test/p?id=12345678901", "12345678901")
    assert await _admits(conn, case, "https://x.example.test/d/12345678901", "12345678901")


@pytest.mark.parametrize(
    "form", ["https://x.example.test/d/{}", "https://x.example.test/open?id={}"], ids=["d", "id"]
)
@pytest.mark.parametrize("case", COLUMNS, ids=IDS)
async def test_pa5_stored_lengths(conn, case, form):
    ten, full, long = "a" * 10, "b" * 256, "c" * 257
    assert await _admits(conn, case, form.format(ten), ten)
    assert await _admits(conn, case, form.format(full), full)
    assert not await _admits(conn, case, form.format(long), long[:256])
    assert not await _admits(conn, case, form.format(long), long[:10])


@pytest.mark.parametrize("case", COLUMNS, ids=IDS)
@pytest.mark.parametrize("tail", [".pdf", "/", '"', " ", "#h"])
async def test_pa6_termination(conn, case, tail):
    assert await _admits(conn, case, f"https://x.example.test/d/{X}{tail}", X)


@pytest.mark.parametrize("case", COLUMNS, ids=IDS)
async def test_pa6_termination_query(conn, case):
    assert await _admits(conn, case, f"https://x.example.test/p?id={X}&usp=1", X)


class _MustNotBeTouched:
    def acquire(self):
        raise AssertionError("the database was touched before the id was refused")


@pytest.mark.parametrize("bad", ["a" * 9, "a" * 257, f"{X}\n", "", "a/b" + "c" * 10])
def test_pa5_pa7_shape_refuses_before_any_query(bad):
    with pytest.raises(mod.HTTPException) as exc:
        mod._assert_file_id_shape(bad)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["a" * 9, "a" * 257, f"{X}\n"])
@pytest.mark.parametrize("handler", [mod.proxy_drive_file, mod.get_drive_thumbnail])
async def test_pa5_pa7_routes_answer_400_before_any_query(handler, bad):
    with pytest.raises(mod.HTTPException) as exc:
        await handler(bad, current_user={"role": "admin"}, db_pool=_MustNotBeTouched())
    assert exc.value.status_code == 400


@pytest.mark.parametrize("ok", ["a" * 10, "a" * 256, X, "12345678901"])  # D4: digits too
def test_pa5_shape_boundaries_pass(ok):
    mod._assert_file_id_shape(ok)


EXPLICIT = [
    ("global", "documents", "file_id", "client_id"),
    ("portal", "documents", "file_id", "client_id"),
    ("global", "company_documents", "google_drive_file_id", "company_id"),
    ("global", "invoices", "drive_file_id", "client_id"),
    ("portal", "invoices", "drive_file_id", "client_id"),
    ("global", "lkpm_receipts", "file_drive_id", None),
    ("global", "weekly_cashout", "receipt_drive_file_id", None),
]


@pytest.mark.parametrize("stored", [X, "12345678901"], ids=["letters", "digits"])
@pytest.mark.parametrize("case", EXPLICIT, ids=[f"{r}:{t}.{c}" for r, t, c, _ in EXPLICIT])
async def test_an_explicit_id_column_is_read_as_the_exact_id(conn, case, stored):
    reader, table, col, owner = case
    await conn.execute(
        "DELETE FROM documents; DELETE FROM company_documents; DELETE FROM invoices;"
        " DELETE FROM lkpm_receipts; DELETE FROM weekly_cashout"
    )
    if owner:
        await conn.execute(f"INSERT INTO {table}({owner}, {col}) VALUES (1, $1)", stored)
    else:
        await conn.execute(f"INSERT INTO {table}({col}) VALUES ($1)", stored)
    assert await _ask(conn, reader, stored)
    assert not await _ask(conn, reader, Y)


FOLDER_BAD = ["a" * 9, "a" * 257, "a" * 10 + "\n", "x/" + "a" * 10, ""]
FOLDER_OK = ["a" * 10, "a" * 256, "12345678901"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "folder_id",
    FOLDER_BAD + FOLDER_OK,
    ids=["nine", "257", "newline", "slash", "empty", "ten", "256", "digits"],
)
async def test_pa5_pa7_the_folder_walk_asks_drive_only_for_a_well_formed_id(folder_id):
    response = MagicMock(status_code=200)
    response.json.return_value = {"parents": ["p"]}
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    got = await mod._drive_parent_fetcher(client, "token")(folder_id)
    if folder_id in FOLDER_OK:
        assert got == ["p"]
        client.get.assert_awaited_once()
    else:
        assert got == []
        client.get.assert_not_awaited()
