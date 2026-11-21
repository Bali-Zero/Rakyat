"""Mock-level tests of the bank statement import: refusals, handler, and what never leaks.

The SQL itself (unique index, ON CONFLICT counts, rollback, exact values, history segments)
is proved against a real PostgreSQL in test_accounting_bank_import_integration.py; a
mock cannot show it. Here: the refusals that must happen before any database call
(I2), the handler's answers and its sharing of the preview's upload checks (I8), and
the content-free messages and logs (I9). Every value is invented.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.routers.accounting as accounting_module
from backend.app.dependencies import get_current_user, get_database_pool
from backend.services.accounting import bank_import_service
from backend.services.accounting.bank_import_service import (
    BankImportError,
    ImportResult,
    import_statement,
)
from backend.services.accounting.bank_parser import ParsedStatement  # noqa: F401  (annotation only)
from backend.tests.fixtures.bank_statement_builder import (
    at,
    build_statement,
    statement_text,
)

SENTINEL = "SENTINEL7f3a"
ROWS = [
    (at(2, 9), "DIRECT DEBIT", "100.00", "0.00"),
    (at(3, 10), "REMITTANCE CR - BIFAST", "0.00", "250.50"),
]
URL_IMPORT = "/api/crm/accounting/bank-statement/import"
URL_PREVIEW = "/api/crm/accounting/bank-statement/preview"
ADMIN = {"id": "asya", "email": "Asya@Example.Test", "role": "admin"}
OUTSIDER = {"id": "x", "email": "random@example.test", "role": "user"}


PLANTED = ("700000000001", "PT CONTOH UJI", "desc0", "desc1", SENTINEL, "Statement.CSV", "s.csv")


@pytest.fixture(autouse=True)
def _no_planted_value_in_any_log_record(caplog: pytest.LogCaptureFixture):
    """Every test in this file runs with all log records captured: none may contain a cell, a
    metadata value, the account number or name, or a filename, whatever the answer was."""
    caplog.set_level(logging.DEBUG)
    yield
    # caplog.text is per phase: after the yield it would only hold teardown records
    logged = " ".join(r.getMessage() for phase in ("setup", "call") for r in caplog.get_records(phase))
    leaked = [v for v in PLANTED if v in logged]
    assert not leaked, f"statement content reached a log record: {leaked}"


def _conn() -> MagicMock:
    conn = MagicMock()
    for name in ("execute", "fetch", "fetchrow", "fetchval"):
        setattr(conn, name, AsyncMock())
    conn.transaction = MagicMock()
    return conn


# ── I2: refusals store nothing, and happen before any database call ───────────


async def test_i2_a_failing_balance_chain_is_refused_before_the_database() -> None:
    conn = _conn()
    stmt = build_statement(ROWS, opening="1000.00", tamper_balance_of=0)
    with pytest.raises(BankImportError, match=r"running-balance check \(\d+ row"):
        await import_statement(conn, stmt, uploaded_by="a", source_filename="f.csv")
    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()
    conn.fetch.assert_not_called()


async def test_i2_an_empty_account_number_is_refused_before_the_database() -> None:
    conn = _conn()
    stmt = build_statement(ROWS, opening="1000.00", account=None)
    with pytest.raises(BankImportError, match="no account number"):
        await import_statement(conn, stmt, uploaded_by="a", source_filename="f.csv")
    conn.transaction.assert_not_called()


def _statement_with(transactions: list) -> ParsedStatement:
    from backend.services.accounting.bank_parser import ParsedStatement

    return ParsedStatement(
        account_number="700000000001", account_name="", metadata={}, transactions=transactions
    )


async def test_i2_a_statement_with_no_transactions_is_a_refusal_not_a_crash() -> None:
    conn = _conn()
    with pytest.raises(BankImportError) as info:
        await import_statement(conn, _statement_with([]), uploaded_by="a", source_filename="f.csv")
    assert str(info.value) == "statement has no transactions"
    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()
    conn.fetch.assert_not_called()
    conn.fetchval.assert_not_called()


@pytest.mark.parametrize("missing", ["posted_at", "balance", "debit", "credit"])
async def test_i2_rows_without_an_exact_timestamp_or_amounts_are_refused(missing: str) -> None:
    from datetime import date
    from decimal import Decimal

    from backend.services.accounting.bank_parser import ParsedTxn

    # what the generic (non-CIMB) parser leaves: no exact timestamp, no exact balance
    legacy = ParsedTxn(txn_date=date(2026, 11, 2), description="d", amount_idr=5, direction="debit")
    good = build_statement(ROWS, opening="1000.00").transactions[0]
    if missing != "posted_at" and missing != "balance":
        legacy.posted_at, legacy.balance = good.posted_at, Decimal("1.00")
    setattr(legacy, missing, None)
    conn = _conn()
    with pytest.raises(BankImportError) as info:
        await import_statement(
            conn, _statement_with([good, legacy]), uploaded_by="a", source_filename="f.csv"
        )
    assert str(info.value) == "statement rows lack an exact timestamp or exact amounts"
    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()
    conn.fetchval.assert_not_called()


_FIXED_REFUSALS = {
    "no_transactions": r"statement has no transactions",
    "missing_fields": r"statement rows lack an exact timestamp or exact amounts",
    "balance_check": r"statement failed its running-balance check \(\d+ row\(s\) out of place\)",
    "no_account": r"statement has no account number",
}


def _refusal_statement(kind: str):
    """A statement that triggers one refusal, with the sentinel planted in every free-text field."""
    from datetime import date

    from backend.services.accounting.bank_parser import (
        ParsedStatement,
        ParsedTxn,
        parse_cimb_statement,
    )

    text = statement_text(ROWS, opening="1000.00", account=None if kind == "no_account" else "700000000001")
    text = text.replace("desc0", SENTINEL).replace("desc1", SENTINEL).replace("PT CONTOH UJI", SENTINEL)
    if kind == "balance_check":
        text = statement_text(ROWS, opening="1000.00", tamper_balance_of=0).replace("desc0", SENTINEL)
    if kind == "no_transactions":
        return ParsedStatement("700000000001", SENTINEL, {}, [])
    if kind == "missing_fields":
        legacy = ParsedTxn(txn_date=date(2026, 11, 2), description=SENTINEL, amount_idr=5, direction="debit")
        return ParsedStatement("700000000001", SENTINEL, {SENTINEL: SENTINEL}, [legacy])
    return parse_cimb_statement(text)


@pytest.mark.parametrize("kind", list(_FIXED_REFUSALS))
async def test_i9_every_refusal_is_fixed_text_with_no_statement_content(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    import re

    conn = _conn()
    with caplog.at_level(logging.DEBUG), pytest.raises(BankImportError) as info:
        await import_statement(conn, _refusal_statement(kind), uploaded_by=SENTINEL, source_filename=SENTINEL)
    assert re.fullmatch(_FIXED_REFUSALS[kind], str(info.value)), str(info.value)
    assert SENTINEL not in caplog.text and "PT CONTOH UJI" not in caplog.text
    assert not caplog.records  # the service writes no log record at all


# ── I8: the handler ─────────────────────────────────────────────────────────


def _app(mock_db_pool, user: dict) -> TestClient:
    pool, _ = mock_db_pool
    app = FastAPI()
    app.include_router(accounting_module.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_database_pool] = lambda: pool
    return TestClient(app, raise_server_exceptions=False)


def _post(mock_db_pool, user, url, data: bytes, name="s.csv"):
    return _app(mock_db_pool, user).post(url, files={"file": (name, data, "text/csv")})


GOOD = statement_text(ROWS, opening="1000.00").encode()


def _ok_result() -> ImportResult:
    return ImportResult(statement_id=7, rows_inserted=2, rows_duplicate=0, history_segments=1, nothing_new=False)


def test_i8_success_returns_the_preview_report_plus_the_import_result(mock_db_pool, monkeypatch) -> None:
    fake = AsyncMock(return_value=_ok_result())
    monkeypatch.setattr(bank_import_service, "import_statement", fake)
    resp = _post(mock_db_pool, ADMIN, URL_IMPORT, GOOD, name="Statement.CSV")
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == 2 and body["balance_check"]["ok"] is True  # the preview report
    assert body["import"] == {
        "statement_id": 7, "rows_inserted": 2, "rows_duplicate": 0,
        "history_segments": 1, "nothing_new": False,
    }
    fake.assert_awaited_once()
    kwargs = fake.await_args.kwargs
    assert kwargs["uploaded_by"] == "asya@example.test"  # lower-cased email from the token
    assert kwargs["source_filename"] == "Statement.CSV"
    assert fake.await_args.args[1].account_number == "700000000001"


def test_i8_nothing_new_is_reported_with_200(mock_db_pool, monkeypatch) -> None:
    res = ImportResult(statement_id=None, rows_inserted=0, rows_duplicate=2, history_segments=1, nothing_new=True)
    monkeypatch.setattr(bank_import_service, "import_statement", AsyncMock(return_value=res))
    resp = _post(mock_db_pool, ADMIN, URL_IMPORT, GOOD)
    assert resp.status_code == 200 and resp.json()["import"]["nothing_new"] is True


def test_i8_a_refusal_of_the_service_is_a_422_with_its_fixed_text(mock_db_pool, monkeypatch) -> None:
    msg = "statement disagrees with rows already stored (1 stored row(s) in its period are not in it)"
    monkeypatch.setattr(bank_import_service, "import_statement", AsyncMock(side_effect=BankImportError(msg)))
    resp = _post(mock_db_pool, ADMIN, URL_IMPORT, GOOD)
    assert resp.status_code == 422
    assert resp.json()["detail"] == msg


def test_i8_the_handler_refuses_a_failing_chain_through_the_service(mock_db_pool) -> None:
    # no patching: the real service refuses before touching the (mock) database
    bad = statement_text(ROWS, opening="1000.00", tamper_balance_of=0).encode()
    resp = _post(mock_db_pool, ADMIN, URL_IMPORT, bad)
    assert resp.status_code == 422 and "running-balance check" in resp.json()["detail"]
    pool, conn = mock_db_pool
    conn.execute.assert_not_called()


def _cases():
    big = b"x" * 64
    return [
        ("outsider", OUTSIDER, GOOD, "s.csv", None),
        ("filename", ADMIN, GOOD, "s.txt", None),
        ("empty", ADMIN, b"", "s.csv", None),
        ("not_utf8", ADMIN, b"\xff\xfe\x00bad", "s.csv", None),
        ("unparseable", ADMIN, b"a,b\n1,2\n", "s.csv", None),
        ("too_many_lines", ADMIN, b"\n" * 20_001, "s.csv", None),
        ("oversize", ADMIN, big, "s.csv", 16),
    ]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c[0])
def test_i8_same_checks_same_answers_as_the_preview(mock_db_pool, monkeypatch, case) -> None:
    _, user, data, name, limit = case
    if limit is not None:
        monkeypatch.setattr(accounting_module, "_MAX_BANK_STATEMENT_BYTES", limit)
    monkeypatch.setattr(bank_import_service, "import_statement", AsyncMock(return_value=_ok_result()))
    imp = _post(mock_db_pool, user, URL_IMPORT, data, name)
    prev = _post(mock_db_pool, user, URL_PREVIEW, data, name)
    assert imp.status_code == prev.status_code != 200
    assert imp.json() == prev.json()


def test_i8_the_handlers_only_database_writes_happen_inside_import_statement(
    mock_db_pool, monkeypatch
) -> None:
    """Behaviour, not source: a recording connection that fails any call made while
    `import_statement` is not running proves the handler touches the database only through it."""
    pool, _ = mock_db_pool
    state = {"inside": False, "calls": 0}

    class Recording:
        def __getattr__(self, name):
            async def call(*_a, **_k):
                assert state["inside"], f"handler used the connection directly: {name}"
                state["calls"] += 1
                return [] if name == "fetch" else (False if name == "fetchval" else None)

            return call

        def transaction(self):
            assert state["inside"], "handler opened a transaction itself"

            class Ctx:
                async def __aenter__(self_inner):
                    return None

                async def __aexit__(self_inner, *a):
                    return None

            return Ctx()

    class Ctx:
        async def __aenter__(self):
            return Recording()

        async def __aexit__(self, *a):
            return None

    pool.acquire = MagicMock(return_value=Ctx())
    real = bank_import_service.import_statement

    async def wrapped(conn, statement, **kw):
        state["inside"] = True
        try:
            return await real(conn, statement, **kw)
        finally:
            state["inside"] = False

    monkeypatch.setattr(bank_import_service, "import_statement", wrapped)
    resp = _post(mock_db_pool, ADMIN, URL_IMPORT, GOOD)
    assert resp.status_code == 200
    assert state["calls"] > 0  # the import really did use the connection, and only inside


def test_i9_a_database_error_never_puts_its_text_in_the_body_or_the_log(
    mock_db_pool, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Boom(asyncpg.PostgresError):
        pass

    monkeypatch.setattr(
        bank_import_service, "import_statement", AsyncMock(side_effect=Boom(f"detail {SENTINEL}"))
    )
    with caplog.at_level(logging.DEBUG):
        resp = _post(mock_db_pool, ADMIN, URL_IMPORT, GOOD, name=f"{SENTINEL}.csv")
    assert resp.status_code == 500
    assert SENTINEL not in resp.text
    assert SENTINEL not in caplog.text
    assert any("Boom" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "boom",
    [
        asyncpg.exceptions.DataError(f"invalid input for query argument $9: {SENTINEL}"),
        RuntimeError(SENTINEL),
        asyncpg.PostgresError(SENTINEL),
        asyncpg.exceptions.InterfaceError(SENTINEL),
    ],
    ids=["DataError", "RuntimeError", "PostgresError", "InterfaceError"],
)
def test_i8_i9_any_storing_failure_is_a_500_with_fixed_text_and_no_content(
    mock_db_pool, monkeypatch, caplog: pytest.LogCaptureFixture, boom: Exception
) -> None:
    monkeypatch.setattr(bank_import_service, "import_statement", AsyncMock(side_effect=boom))
    with caplog.at_level(logging.DEBUG):
        resp = _post(mock_db_pool, ADMIN, URL_IMPORT, GOOD, name=f"{SENTINEL}.csv")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "bank statement could not be stored"}
    assert SENTINEL not in resp.text
    assert SENTINEL not in caplog.text
    assert any(type(boom).__name__ in r.getMessage() for r in caplog.records)


def test_i8_a_refusal_is_still_a_422_next_to_the_total_catch(mock_db_pool, monkeypatch) -> None:
    monkeypatch.setattr(
        bank_import_service, "import_statement", AsyncMock(side_effect=BankImportError("fixed text"))
    )
    resp = _post(mock_db_pool, ADMIN, URL_IMPORT, GOOD)
    assert resp.status_code == 422 and resp.json() == {"detail": "fixed text"}


def test_i9_a_refusal_body_does_not_echo_the_filename(mock_db_pool) -> None:
    resp = _post(mock_db_pool, ADMIN, URL_IMPORT, b"\n" * 5, name=f"{SENTINEL}.csv")
    assert resp.status_code == 422 and SENTINEL not in resp.text
