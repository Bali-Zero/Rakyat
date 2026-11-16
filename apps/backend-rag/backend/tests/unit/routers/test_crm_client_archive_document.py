"""Archive registration of an existing Drive file: no OCR, no portal message, explicit visibility."""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.routers.crm_enhanced_documents import (
    ClientArchiveDocumentCreate,
    register_archive_document,
)
from backend.services.crm.admin_document_visibility import admin_document_not_deleted_clause

MOD = "backend.app.routers.crm_enhanced_documents"
ADMIN = {"email": "Archive.Admin@Example.test", "role": "admin"}


class _FakeDb:
    """Shared state of a fake Postgres: rows, one asyncio.Lock per advisory key, one call log."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.locks: dict[tuple, asyncio.Lock] = {}
        self.log: list[dict] = []
        self.next_id = 901


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.depth += 1
        self.conn.tx_id += 1
        self.conn.db.log.append({"op": "tx_enter", "tx": self.conn.tx_id})
        return self

    async def __aexit__(self, *a):
        self.conn.db.log.append({"op": "tx_exit", "tx": self.conn.tx_id})
        for lock in self.conn.held:
            lock.release()
        self.conn.held.clear()
        self.conn.depth -= 1
        return False


class _FakeConn:
    def __init__(self, db):
        self.db = db
        self.depth = 0
        self.tx_id = 0
        self.held: list[asyncio.Lock] = []

    def transaction(self):
        return _Tx(self)

    def _record(self, op, sql, args):
        self.db.log.append(
            {
                "op": op,
                "sql": sql,
                "args": args,
                "in_tx": self.depth > 0,
                "tx": self.tx_id if self.depth else None,
            }
        )

    async def execute(self, sql, *args):
        self._record("execute", sql, args)
        match = re.search(r"pg_advisory_xact_lock\((\d+), hashtext", sql)
        if match:
            lock = self.db.locks.setdefault((int(match.group(1)), args[0]), asyncio.Lock())
            await lock.acquire()
            self.held.append(lock)  # released only when the transaction exits

    async def fetchval(self, sql, *args):
        self._record("fetchval", sql, args)
        if "INSERT INTO documents" in sql:
            await asyncio.sleep(0)
            doc_id = self.db.next_id
            self.db.next_id += 1
            self.db.rows.append({"id": doc_id, "client_id": args[0], "file_id": args[4]})
            return doc_id
        found = next(
            (
                r["id"]
                for r in self.db.rows
                if r["client_id"] == args[0] and r["file_id"] == args[1]
            ),
            None,
        )
        await asyncio.sleep(0)  # let a sibling call interleave between probe and insert
        return found


def _pool(existing_id=None, db=None):
    db = db or _FakeDb(
        [{"id": existing_id, "client_id": 7, "file_id": "drive-1"}] if existing_id else []
    )
    pool = MagicMock()
    conns = []

    def _acquire():
        conn = _FakeConn(db)
        conns.append(conn)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool.acquire = MagicMock(side_effect=_acquire)
    pool.conns = conns
    pool.db = db
    return pool, db


def _body(**kw):
    base = {"file_id": "drive-1", "document_type": "passport", "client_visible": False}
    return ClientArchiveDocumentCreate(**{**base, **kw})


@pytest.fixture
def guards():
    with (
        patch(f"{MOD}.verify_client_access", new=AsyncMock()) as access,
        patch(f"{MOD}.assert_drive_file_belongs_to_client", new=AsyncMock()) as owner,
        patch(f"{MOD}.invalidate_cache", new=AsyncMock()) as cache,
        patch(f"{MOD}._dispatch_ocr_by_folder", new=AsyncMock()) as ocr,
        patch(f"{MOD}.spawn") as spawn,
        patch(
            "backend.services.portal.portal_notification_service.PortalNotificationService"
        ) as portal,
    ):
        yield MagicMock(
            access=access, owner=owner, cache=cache, ocr=ocr, spawn=spawn, portal=portal
        )


def _inserts(db):
    return [c for c in db.log if c["op"] == "fetchval" and "INSERT INTO documents" in c["sql"]]


def _sql_calls(db):
    return [c["sql"] for c in db.log if c["op"] in ("execute", "fetchval")]


def _statements(db):
    return [c for c in db.log if c["op"] in ("execute", "fetchval")]


@pytest.mark.asyncio
async def test_non_admin_is_403_before_the_database(guards):
    pool, db = _pool()
    with pytest.raises(HTTPException) as exc:
        await register_archive_document(7, _body(), pool, {"email": "x@y.z", "role": "staff"})
    assert exc.value.status_code == 403
    pool.acquire.assert_not_called()
    assert not db.log


@pytest.mark.asyncio
async def test_new_file_inserts_once_hidden_with_no_side_effects(guards):
    pool, db = _pool()
    result = await register_archive_document(7, _body(), pool, ADMIN)
    conn = pool.conns[0]

    assert result == {"id": 901, "success": True, "deduplicated": False, "ocr_triggered": False}
    inserts = _inserts(db)
    assert len(inserts) == 1
    sql, args = inserts[0]["sql"], inserts[0]["args"]
    assert "'active'" in sql and "'google_drive'" in sql
    assert args[0] == 7 and args[4] == "drive-1"
    assert args[7] == "archive.admin@example.test"
    assert args[8] is False
    guards.access.assert_awaited_once_with(7, ADMIN, conn, allow_assigned=True, write=True)
    guards.owner.assert_awaited_once_with("drive-1", 7, conn)
    guards.ocr.assert_not_called()
    guards.spawn.assert_not_called()
    guards.portal.assert_not_called()
    assert not any("clients" in s.lower().replace("client_id", "") for s in _sql_calls(db))
    assert not any("UPDATE" in s for s in _sql_calls(db))
    assert not any("ocr_status" in s for s in _sql_calls(db))
    executes = [c for c in db.log if c["op"] == "execute"]
    assert len(executes) == 1 and "pg_advisory_xact_lock(742021" in executes[0]["sql"]
    guards.cache.assert_awaited_once_with("zantara:crm_clients_stats:*")


@pytest.mark.asyncio
async def test_lock_probe_and_insert_run_in_order_inside_one_transaction(guards):
    pool, db = _pool()
    await register_archive_document(7, _body(), pool, ADMIN)

    stmts = _statements(db)
    assert "pg_advisory_xact_lock(742021" in stmts[0]["sql"]
    assert stmts[0]["args"] == ("7:drive-1",)
    assert "SELECT d.id FROM documents d" in stmts[1]["sql"]
    assert "INSERT INTO documents" in stmts[2]["sql"]
    assert len(stmts) == 3
    assert all(c["in_tx"] for c in stmts)
    assert len({c["tx"] for c in stmts}) == 1
    assert [c["op"] for c in db.log if c["op"].startswith("tx_")] == ["tx_enter", "tx_exit"]


@pytest.mark.asyncio
async def test_probe_excludes_deleted_documents(guards):
    pool, db = _pool()
    await register_archive_document(7, _body(), pool, ADMIN)

    probe = " ".join(_statements(db)[1]["sql"].split())
    assert " ".join(admin_document_not_deleted_clause("d").split()) in probe
    assert _statements(db)[1]["args"] == (7, "drive-1")


@pytest.mark.asyncio
async def test_explicit_visible_true_is_written_as_true(guards):
    pool, db = _pool()
    await register_archive_document(7, _body(client_visible=True), pool, ADMIN)
    assert _inserts(db)[0]["args"][8] is True


@pytest.mark.asyncio
async def test_existing_row_is_returned_without_insert(guards):
    pool, db = _pool(existing_id=55)
    result = await register_archive_document(7, _body(), pool, ADMIN)

    assert result == {"id": 55, "success": True, "deduplicated": True, "ocr_triggered": False}
    assert not _inserts(db)
    stmts = _statements(db)
    assert len(stmts) == 2
    assert "pg_advisory_xact_lock(742021" in stmts[0]["sql"] and stmts[0]["args"] == ("7:drive-1",)
    assert "SELECT d.id FROM documents d" in stmts[1]["sql"]
    assert all(c["in_tx"] for c in stmts) and len({c["tx"] for c in stmts}) == 1
    guards.cache.assert_not_called()


@pytest.mark.asyncio
async def test_two_concurrent_calls_register_the_file_once(guards):
    pool, db = _pool()
    first, second = await asyncio.wait_for(
        asyncio.gather(
            register_archive_document(7, _body(), pool, ADMIN),
            register_archive_document(7, _body(), pool, ADMIN),
        ),
        timeout=2,
    )

    assert len(_inserts(db)) == 1
    assert len(db.rows) == 1
    assert sorted([first["deduplicated"], second["deduplicated"]]) == [False, True]
    assert first["id"] == second["id"] == 901


@pytest.mark.asyncio
async def test_ownership_guard_403_propagates_without_insert(guards):
    pool, db = _pool()
    guards.owner.side_effect = HTTPException(status_code=403, detail="foreign file")
    with pytest.raises(HTTPException) as exc:
        await register_archive_document(7, _body(), pool, ADMIN)
    assert exc.value.status_code == 403
    assert not db.log


def test_client_visible_is_required():
    with pytest.raises(ValidationError):
        ClientArchiveDocumentCreate(file_id="f", document_type="passport")


@pytest.mark.parametrize("bad", ["false", "true", 0, 1, None])
def test_client_visible_is_strict_bool(bad):
    with pytest.raises(ValidationError):
        ClientArchiveDocumentCreate(file_id="f", document_type="passport", client_visible=bad)
