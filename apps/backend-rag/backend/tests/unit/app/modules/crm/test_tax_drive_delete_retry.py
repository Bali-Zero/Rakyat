"""DELETE of a tax document: a failed CRM sync after the Drive trash is retryable.

Finding be-crm-84: the Drive trash ran first, the sync was two autocommit UPDATEs, and a
retry met a trashed file and got 404, so the registrations were never retired.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.app.modules.crm import tax_drive_documents
from backend.tests.unit.app.modules.crm.test_tax_drive_documents import (
    COMPANY_ID,
    FILE_FOREIGN,
    FILE_PERSONAL,
    FILE_TRASHED,
    TAX,
    _FakeConn,
    _FakeDrive,
    _make_client,
    _user,
)

URL = "/api/crm/clients/7/tax-documents/{}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.utils import client_pools

    monkeypatch.setattr(client_pools, "_VALIDATED", ())
    monkeypatch.setattr(client_pools, "_LAST_ATTEMPT", None)
    monkeypatch.setattr(tax_drive_documents, "invalidate_cache", AsyncMock())


class _StatefulDrive(_FakeDrive):
    """Like the Drive: trashing flips the metadata's `trashed` flag."""

    async def trash_file(self, file_id: str) -> None:
        await super().trash_file(file_id)
        self.nodes[file_id]["trashed"] = True


def _fail_company_update(monkeypatch: pytest.MonkeyPatch, remaining: list[int]) -> None:
    """Make the company_documents UPDATE raise `remaining[0]` more times."""
    original = _FakeConn.execute

    async def execute(self: _FakeConn, sql: str, *args: Any) -> str:
        if "UPDATE company_documents" in sql and remaining[0] > 0:
            remaining[0] -= 1
            self.tx_log.append(("exec", sql))
            raise RuntimeError("synthetic company_documents failure")
        return await original(self, sql, *args)

    monkeypatch.setattr(_FakeConn, "execute", execute)


def _updates(pool: Any, table: str) -> list[str]:
    return [sql for sql, _ in pool.conn.executed if f"UPDATE {table}" in sql]


def test_retry_after_failed_sync_retires_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    drive = _StatefulDrive()
    client, drive, _, pool = _make_client(user=_user(TAX, "Tax Consultant"), drive=drive)
    _fail_company_update(monkeypatch, [1])

    first = client.delete(URL.format(FILE_PERSONAL))
    assert first.status_code == 500
    assert drive.trashed == [FILE_PERSONAL]
    assert drive.nodes[FILE_PERSONAL]["trashed"] is True

    second = client.delete(URL.format(FILE_PERSONAL))
    assert second.status_code == 200
    assert second.json() == {"success": True, "file_id": FILE_PERSONAL, "trashed": True}
    # Same success shape; the Drive trash is not repeated, the sync is.
    assert drive.trashed == [FILE_PERSONAL]
    assert len(_updates(pool, "documents")) == 2  # first (rolled back) + retry
    comp = [a for s, a in pool.conn.executed if "UPDATE company_documents" in s]
    assert comp == [([COMPANY_ID], FILE_PERSONAL)]
    assert pool.conn.tx_log[-1] == ("exit", "None")


def test_both_updates_share_one_transaction_and_roll_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, drive, _, pool = _make_client(user=_user(TAX, "Tax Consultant"))
    _fail_company_update(monkeypatch, [1])

    assert client.delete(URL.format(FILE_PERSONAL)).status_code == 500

    kinds = [e[0] for e in pool.conn.tx_log]
    assert kinds == ["enter", "exec", "exec", "exit"]
    assert "UPDATE documents" in pool.conn.tx_log[1][1]
    assert "UPDATE company_documents" in pool.conn.tx_log[2][1]
    # The transaction was left by the exception, so Postgres rolls the documents UPDATE back.
    assert pool.conn.tx_log[3] == ("exit", "RuntimeError")


def test_normal_delete_trashes_once_and_syncs_once() -> None:
    client, drive, _, pool = _make_client(user=_user(TAX, "Tax Consultant"))

    res = client.delete(URL.format(FILE_PERSONAL))

    assert res.status_code == 200
    assert res.json() == {"success": True, "file_id": FILE_PERSONAL, "trashed": True}
    assert drive.trashed == [FILE_PERSONAL]
    assert len(_updates(pool, "documents")) == 1
    assert len(_updates(pool, "company_documents")) == 1
    assert [e[0] for e in pool.conn.tx_log] == ["enter", "exec", "exec", "exit"]
    assert pool.conn.tx_log[-1] == ("exit", "None")


def test_trashed_file_outside_the_client_scope_is_still_refused() -> None:
    drive = _StatefulDrive()
    drive.nodes[FILE_FOREIGN]["trashed"] = True
    client, drive, _, pool = _make_client(user=_user(TAX, "Tax Consultant"), drive=drive)

    res = client.delete(URL.format(FILE_FOREIGN))

    assert res.status_code == 403
    assert drive.trashed == []
    assert pool.conn.executed == []


def test_replace_and_rename_still_refuse_a_trashed_file() -> None:
    client, drive, _, pool = _make_client(user=_user(TAX, "Tax Consultant"))

    assert client.patch(URL.format(FILE_TRASHED), json={"name": "x.pdf"}).status_code == 404
    res = client.put(
        URL.format(FILE_TRASHED) + "/content",
        files={"file": ("x.pdf", b"%PDF-1.4 synthetic", "application/pdf")},
    )
    assert res.status_code == 404
    assert drive.renamed == []
    assert drive.replaced == []
    assert pool.conn.executed == []
