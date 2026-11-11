"""Unit tests for ``backend.db.schema_audit``.

The audit module is mostly orchestration around two collaborators:

* ``MigrationManager`` — exposes ``get_status()``.
* ``asyncpg.Pool`` — yields connections we query for tracking-table state
  and ``information_schema`` lookups.

We mock both. The tests intentionally avoid spinning up Postgres because
the contract being validated here is "given input X, produce finding Y" —
not "schema_migrations works on PG". Real-PG smoke is covered separately
by ``test_migration_114_115_116_roundtrip.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.db import migration_manager, schema_audit
from backend.db.schema_audit import AuditReport, Finding, run_audit

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAcquireCtx:
    """Async context manager wrapping a single connection mock."""

    def __init__(self, conn: AsyncMock) -> None:
        self.conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self.conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


def _build_conn(
    *,
    legacy_table_exists: bool,
    canonical_table_exists: bool,
    legacy_numbers: list[int],
    canonical_numbers: list[int],
    extra_tables: set[str] | None = None,
    client_email_duplicates: list[dict[str, object]] | None = None,
    conversations_privileges: dict[str, object] | None = None,
) -> AsyncMock:
    """Build a connection that responds to the SQL the audit issues.

    The audit only ever calls:
    - ``fetchval`` for ``information_schema.tables`` existence checks
    - ``fetch`` against the two tracking tables
    - ``fetchrow`` for the conversations runtime-privileges query; ``None``
      (the default) answers like a database without ``backend_rag_v2``

    We dispatch on the SQL string to keep the fake light.
    """
    extra_tables = extra_tables or set()
    client_email_duplicates = client_email_duplicates or []
    conn = AsyncMock()

    async def _fetchval(sql: str, name: str) -> bool:
        if "information_schema.tables" not in sql:
            raise AssertionError(f"unexpected fetchval SQL: {sql!r}")
        if name == "_schema_versions":
            return legacy_table_exists
        if name == "schema_migrations":
            return canonical_table_exists
        return name in extra_tables

    async def _fetch(sql: str) -> list[dict[str, object]]:
        if "_schema_versions" in sql:
            return [{"migration_number": n} for n in legacy_numbers]
        if "schema_migrations" in sql:
            return [{"migration_number": n} for n in canonical_numbers]
        if "FROM clients" in sql:
            return client_email_duplicates
        raise AssertionError(f"unexpected fetch SQL: {sql!r}")

    async def _fetchrow(sql: str, role: str) -> dict[str, object] | None:
        if "has_table_privilege" not in sql or role != "backend_rag_v2":
            raise AssertionError(f"unexpected fetchrow SQL: {sql!r} ({role!r})")
        return conversations_privileges

    conn.fetchval.side_effect = _fetchval
    conn.fetch.side_effect = _fetch
    conn.fetchrow.side_effect = _fetchrow
    return conn


def _patch_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pending_list: list[int],
    conn: AsyncMock,
) -> None:
    """Replace ``MigrationManager`` with a stub returning the desired state."""

    pool = MagicMock()
    pool.acquire = lambda: _FakeAcquireCtx(conn)

    class _StubManager:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            self.pool = pool

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def get_status(self) -> dict[str, Any]:
            return {"pending_list": list(pending_list)}

    monkeypatch.setattr(schema_audit, "MigrationManager", _StubManager)


# ---------------------------------------------------------------------------
# Pending-migrations check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_migrations_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1, 2, 3],
        canonical_numbers=[1, 2, 3],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    assert report.ok
    assert "pending_migrations" not in {f.code for f in report.findings}


@pytest.mark.asyncio
async def test_pending_migrations_present(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1, 2],
        canonical_numbers=[1, 2],
    )
    _patch_manager(monkeypatch, pending_list=[3, 4], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    assert not report.ok
    [finding] = [f for f in report.findings if f.code == "pending_migrations"]
    assert finding.severity == "error"
    assert finding.details["pending"] == [3, 4]


# ---------------------------------------------------------------------------
# Tracking-table divergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracking_tables_in_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1, 2, 3, 114],
        canonical_numbers=[1, 2, 3, 114],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    codes = {f.code for f in report.findings}
    assert "tracking_divergence_legacy_only" not in codes
    assert "tracking_divergence_canonical_only" not in codes


@pytest.mark.asyncio
async def test_tracking_legacy_duplicate_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1, 2, 2, 3],
        canonical_numbers=[1, 2, 3],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    [finding] = [f for f in report.findings if f.code == "tracking_duplicate_legacy"]
    assert finding.severity == "error"
    assert finding.details["duplicates"] == {2: 2}


@pytest.mark.asyncio
async def test_tracking_canonical_duplicate_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1, 2, 3],
        canonical_numbers=[1, 2, 3, 3, 130, 130, 130],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    [finding] = [f for f in report.findings if f.code == "tracking_duplicate_canonical"]
    assert finding.severity == "error"
    assert finding.details["duplicates"] == {3: 2, 130: 3}


@pytest.mark.asyncio
async def test_tracking_legacy_only_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration N exists in _schema_versions but not in schema_migrations.

    This is the exact divergence the runner fix targets: a legacy half-
    success during the transition. The canonical writer never recorded
    it. Audit must flag it.
    """
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1, 2, 3, 4],
        canonical_numbers=[1, 2, 3],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    [finding] = [f for f in report.findings if f.code == "tracking_divergence_legacy_only"]
    assert finding.severity == "error"
    assert finding.details["only_in_legacy"] == [4]


@pytest.mark.asyncio
async def test_tracking_canonical_only_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1, 2],
        canonical_numbers=[1, 2, 3],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    [finding] = [f for f in report.findings if f.code == "tracking_divergence_canonical_only"]
    assert finding.details["only_in_canonical"] == [3]


@pytest.mark.asyncio
async def test_brand_new_db_no_tracking_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh DB, no tracking tables yet. Divergence check must be a no-op.

    The first migration creates these tables; until then the audit
    cannot — and must not — assert anything about their contents.
    """
    conn = _build_conn(
        legacy_table_exists=False,
        canonical_table_exists=False,
        legacy_numbers=[],
        canonical_numbers=[],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    codes = {f.code for f in report.findings}
    assert codes == set()
    assert report.ok


@pytest.mark.asyncio
async def test_canonical_only_table_is_not_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-deprecation state: legacy table dropped, only canonical remains.

    Once Step 3 (backfill) lands and Step 4 drops the legacy table, the
    audit must keep working — `_schema_versions` is treated as empty,
    not as a missing dependency.
    """
    conn = _build_conn(
        legacy_table_exists=False,
        canonical_table_exists=True,
        legacy_numbers=[],
        canonical_numbers=[1, 2, 3, 114, 128],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    assert report.ok
    codes = {f.code for f in report.findings}
    assert "tracking_divergence_legacy_only" not in codes
    assert "tracking_divergence_canonical_only" not in codes


# ---------------------------------------------------------------------------
# Required-tables check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_tables_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
        extra_tables={"clients", "team_members"},
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(
        database_url="postgres://fake",
        required_tables=["clients", "team_members"],
    )

    assert report.ok
    assert "required_table_missing" not in {f.code for f in report.findings}


@pytest.mark.asyncio
async def test_required_tables_some_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
        extra_tables={"clients"},
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(
        database_url="postgres://fake",
        required_tables=["clients", "team_members", "lkpm_reports"],
    )

    [finding] = [f for f in report.findings if f.code == "required_table_missing"]
    assert finding.severity == "error"
    assert sorted(finding.details["missing"]) == ["lkpm_reports", "team_members"]


@pytest.mark.asyncio
async def test_required_tables_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var, no override → check is a no-op."""
    monkeypatch.delenv("SCHEMA_AUDIT_REQUIRED_TABLES", raising=False)
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake")

    codes = {f.code for f in report.findings}
    assert "required_table_missing" not in codes


@pytest.mark.asyncio
async def test_required_tables_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEMA_AUDIT_REQUIRED_TABLES", " clients , conversations ")
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
        extra_tables={"clients"},  # 'conversations' missing
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake")

    [finding] = [f for f in report.findings if f.code == "required_table_missing"]
    assert finding.details["missing"] == ["conversations"]
    assert finding.details["configured_via"] == "SCHEMA_AUDIT_REQUIRED_TABLES"


# ---------------------------------------------------------------------------
# Client email uniqueness check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_email_duplicates_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic_key = "synthetic-duplicate-key"
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
        extra_tables={"clients"},
        client_email_duplicates=[
            {"normalized_email": synthetic_key, "count": 2},
        ],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    [finding] = [f for f in report.findings if f.code == "client_email_duplicates"]
    assert finding.severity == "error"
    assert finding.details["duplicates"] == [{"count": 2}]
    for output in (json.dumps(report.to_dict()), schema_audit._format_human(report)):
        assert synthetic_key not in output
        assert "client_ids" not in output
        assert hashlib.sha256(synthetic_key.encode()).hexdigest() not in output
    sql_calls = [call.args[0] for call in conn.fetch.await_args_list]
    duplicate_query = next(sql for sql in sql_calls if "FROM clients" in sql)
    assert "ARRAY_AGG" not in duplicate_query.upper()


@pytest.mark.asyncio
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("duplicates", [False, True])
async def test_cli_outputs_preserve_counts_without_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    json_output: bool,
    duplicates: bool,
) -> None:
    sentinel = "w4b-audit-sentinel@example.invalid"
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
        extra_tables={"clients"},
        client_email_duplicates=[{"normalized_email": sentinel, "count": 3}] if duplicates else [],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)
    monkeypatch.setattr(schema_audit, "_required_tables_from_env", lambda: [])
    argv = ["--database-url", "postgres://fake"] + (["--json"] if json_output else [])

    code = await schema_audit._amain(argv)

    captured = capsys.readouterr()
    assert code == (1 if duplicates else 0)
    output = captured.out + captured.err + caplog.text
    assert sentinel not in output
    assert hashlib.sha256(sentinel.encode()).hexdigest() not in output
    if json_output:
        report = json.loads(captured.out)
        assert report["ok"] is not duplicates
        if duplicates:
            assert report["findings"][0]["details"] == {"duplicates": [{"count": 3}]}
        else:
            assert report["findings"] == []
    else:
        assert ("Result: FAIL" if duplicates else "Result: OK") in captured.out
        if duplicates:
            assert '"count": 3' in captured.out


@pytest.mark.asyncio
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("failure_stage", ["connect", "check", "close"])
async def test_cli_crashes_keep_type_but_not_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    json_output: bool,
    failure_stage: str,
) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)
    monkeypatch.setattr(schema_audit, "_required_tables_from_env", lambda: [])

    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("W4B_SYNTHETIC_AUDIT_ERROR") from RuntimeError("W4B_SYNTHETIC_AUDIT_CAUSE")

    if failure_stage == "check":
        monkeypatch.setattr(schema_audit, "_check_client_email_uniqueness", fail)
    else:
        monkeypatch.setattr(schema_audit.MigrationManager, failure_stage, fail)
    argv = ["--database-url", "postgres://fake"] + (["--json"] if json_output else [])

    code = await schema_audit._amain(argv)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    output = captured.err + caplog.text
    assert "ValueError" in output
    assert "W4B_SYNTHETIC_AUDIT_ERROR" not in output
    assert "W4B_SYNTHETIC_AUDIT_CAUSE" not in output
    assert "Traceback" not in output
    assert caplog.records
    assert all(record.exc_info is None for record in caplog.records)
    for record in caplog.records:
        assert "W4B_SYNTHETIC" not in record.getMessage()
        assert not any(isinstance(arg, BaseException) for arg in record.args)


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_audit_connection_retry_keeps_diagnostics_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exhausted: bool,
) -> None:
    pool = MagicMock()
    pool.close = AsyncMock()
    error = OSError("W4B_SYNTHETIC_RETRY_ERROR")
    create_pool = AsyncMock(side_effect=error if exhausted else [error, pool])
    sleep = AsyncMock()
    monkeypatch.setattr(migration_manager.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(migration_manager.asyncio, "sleep", sleep)
    manager = migration_manager.MigrationManager(database_url="postgres://fake")

    with caplog.at_level(logging.WARNING):
        if exhausted:
            with pytest.raises(migration_manager.MigrationError) as failure:
                await manager.connect()
            assert failure.value.__cause__ is error
            assert manager.pool is None
        else:
            await manager.connect()
    await manager.close()

    assert create_pool.await_count == (5 if exhausted else 2)
    assert [call.args[0] for call in sleep.await_args_list] == (
        [2.0, 4.0, 8.0, 16.0] if exhausted else [2.0]
    )
    assert pool.close.await_count == (0 if exhausted else 1)
    assert "W4B_SYNTHETIC_RETRY_ERROR" not in caplog.text
    assert "OSError" in caplog.text
    assert "1/5" in caplog.text
    assert "2s" in caplog.text
    for record in caplog.records:
        assert record.exc_info is None
        assert "W4B_SYNTHETIC" not in record.getMessage()
        assert not any(isinstance(arg, BaseException) for arg in record.args)


@pytest.mark.asyncio
@pytest.mark.parametrize("json_output", [False, True])
async def test_cli_real_connection_failure_is_private(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    json_output: bool,
) -> None:
    create_pool = AsyncMock(side_effect=ValueError("W4B_SYNTHETIC_CONNECT_ERROR"))
    monkeypatch.setattr(migration_manager.asyncpg, "create_pool", create_pool)
    argv = ["--database-url", "postgres://fake"] + (["--json"] if json_output else [])

    code = await schema_audit._amain(argv)

    captured = capsys.readouterr()
    assert code == 2
    assert create_pool.await_count == 1
    assert captured.out == ""
    output = captured.err + caplog.text
    assert "MigrationError" in output
    assert "W4B_SYNTHETIC_CONNECT_ERROR" not in output
    assert "Traceback" not in output


@pytest.mark.asyncio
async def test_client_email_check_skips_when_clients_table_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
        extra_tables=set(),
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    assert "client_email_duplicates" not in {finding.code for finding in report.findings}


@pytest.mark.asyncio
async def test_conversations_runtime_privileges_check_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    assert "conversations_runtime_privileges" in report.checks_run
    conn.fetchrow.assert_awaited_once()
    assert report.ok


@pytest.mark.asyncio
async def test_conversations_runtime_privileges_missing_insert_fails_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _build_conn(
        legacy_table_exists=True,
        canonical_table_exists=True,
        legacy_numbers=[1],
        canonical_numbers=[1],
        conversations_privileges={
            "is_superuser": False,
            "table_exists": True,
            "can_select": True,
            "can_insert": False,
            "can_update": True,
            "sequence_name": "public.conversations_id_seq",
            "can_use_sequence": True,
        },
    )
    _patch_manager(monkeypatch, pending_list=[], conn=conn)

    report = await run_audit(database_url="postgres://fake", required_tables=[])

    [finding] = [f for f in report.findings if f.code == "conversations_runtime_privilege_missing"]
    assert finding.severity == "error" and not report.ok
    assert finding.details["missing"] == ["INSERT on public.conversations"]


# ---------------------------------------------------------------------------
# Report serialisation
# ---------------------------------------------------------------------------


def test_report_to_dict_round_trips() -> None:
    report = AuditReport(
        checks_run=["pending_migrations"],
        findings=[
            Finding(
                code="pending_migrations",
                severity="error",
                message="2 pending",
                details={"pending": [3, 4]},
            ),
        ],
    )
    payload = report.to_dict()
    assert payload["ok"] is False
    assert payload["checks_run"] == ["pending_migrations"]
    assert payload["findings"][0]["details"] == {"pending": [3, 4]}


def test_report_ok_when_only_warnings() -> None:
    report = AuditReport(
        checks_run=["x"],
        findings=[Finding(code="x", severity="warning", message="heads-up")],
    )
    assert report.ok is True


@pytest.mark.asyncio
async def test_cli_prints_human_report_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _fake_run_audit(*, database_url: str | None = None) -> AuditReport:
        assert database_url == "postgres://fake"
        return AuditReport(
            checks_run=["pending_migrations"],
            findings=[
                Finding(
                    code="pending_migrations",
                    severity="error",
                    message="1 pending",
                    details={"pending": [202]},
                ),
            ],
        )

    monkeypatch.setattr(schema_audit, "run_audit", _fake_run_audit)

    code = await schema_audit._amain(["--database-url", "postgres://fake"])

    captured = capsys.readouterr()
    assert code == 1
    assert "SCHEMA AUDIT" in captured.out
    assert "[ERROR] pending_migrations" in captured.out
    assert '"pending": [202]' in captured.out


@pytest.mark.asyncio
async def test_cli_prints_json_report_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _fake_run_audit(*, database_url: str | None = None) -> AuditReport:
        assert database_url == "postgres://fake"
        return AuditReport(
            checks_run=["tracking_divergence"],
            findings=[
                Finding(
                    code="migration_tracking_missing_in_canonical",
                    severity="error",
                    message="tracking mismatch",
                    details={"missing_in_canonical": [201]},
                ),
            ],
        )

    monkeypatch.setattr(schema_audit, "run_audit", _fake_run_audit)

    code = await schema_audit._amain(["--database-url", "postgres://fake", "--json"])

    captured = capsys.readouterr()
    assert code == 1
    assert '"ok": false' in captured.out
    assert '"missing_in_canonical": [201]' in captured.out
