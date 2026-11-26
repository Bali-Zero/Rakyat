"""`apply_all_pending` must stop at the first failed migration.

Before the fix the loop recorded a failure and kept going, so migration N+k
committed after N failed: an out-of-order gap in `_schema_versions`, and later
migrations ran against a schema missing N. Fakes only; no database involved.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from backend.db.migration_base import BaseMigration
from backend.db.migration_manager import MigrationManager

NUMBERS = [101, 102, 103]


def _manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcomes: dict[int, Any],
    *,
    pending: list[int] | None = None,
    mismatches: list[int] | None = None,
) -> tuple[MigrationManager, list[int]]:
    """Manager with fake discovery; `outcomes[n]` is True, False or an Exception."""
    # BaseMigration insists the SQL file exists under its MIGRATIONS_DIR.
    for n in pending or NUMBERS:
        (tmp_path / f"{n}_synthetic.sql").write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(BaseMigration, "MIGRATIONS_DIR", tmp_path)
    mgr = MigrationManager(database_url="postgresql://fake/fake")
    attempted: list[int] = []
    discovered = [{"number": n, "file": f"{n}_synthetic.sql"} for n in (pending or NUMBERS)]

    async def fake_discover() -> list[dict]:
        return discovered

    async def fake_applied() -> list[dict]:
        return []

    async def fake_apply(migration: BaseMigration) -> bool:
        attempted.append(migration.migration_number)
        outcome = outcomes[migration.migration_number]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mgr, "discover_migrations", fake_discover)
    monkeypatch.setattr(mgr, "get_applied_migrations", fake_applied)
    monkeypatch.setattr(mgr, "apply_migration", fake_apply)
    monkeypatch.setattr(
        mgr, "_applied_checksum_findings", lambda *_a, **_kw: (mismatches or [], [])
    )
    return mgr, attempted


@pytest.mark.asyncio
async def test_returned_false_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mgr, attempted = _manager(monkeypatch, tmp_path, {101: False, 102: True, 103: True})
    with caplog.at_level(logging.ERROR):
        result = await mgr._apply_all_pending_locked()  # noqa: SLF001
    assert attempted == [101]
    assert [f["number"] for f in result["failed"]] == [101]
    assert result["applied"] == []
    assert "Not attempted: [102, 103]" in caplog.text


@pytest.mark.asyncio
async def test_raised_exception_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mgr, attempted = _manager(
        monkeypatch, tmp_path, {101: RuntimeError("synthetic boom"), 102: True, 103: True}
    )
    result = await mgr._apply_all_pending_locked()  # noqa: SLF001
    assert attempted == [101]
    assert result["failed"] == [{"number": 101, "error": "synthetic boom"}]
    assert result["applied"] == []


@pytest.mark.asyncio
async def test_failure_in_the_middle_keeps_earlier_and_skips_later(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mgr, attempted = _manager(monkeypatch, tmp_path, {101: True, 102: False, 103: True})
    result = await mgr._apply_all_pending_locked()  # noqa: SLF001
    assert attempted == [101, 102]
    assert result["applied"] == [101]
    assert [f["number"] for f in result["failed"]] == [102]


@pytest.mark.asyncio
async def test_unsorted_discovery_still_stops_in_number_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mgr, attempted = _manager(
        monkeypatch, tmp_path, {101: True, 102: False, 103: True}, pending=[103, 101, 102]
    )
    result = await mgr._apply_all_pending_locked()  # noqa: SLF001
    assert attempted == [101, 102]
    assert [f["number"] for f in result["failed"]] == [102]


@pytest.mark.asyncio
async def test_all_success_applies_everything_in_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mgr, attempted = _manager(monkeypatch, tmp_path, {101: True, 102: True, 103: True})
    result = await mgr._apply_all_pending_locked()  # noqa: SLF001
    assert attempted == [101, 102, 103]
    assert result == {"applied": [101, 102, 103], "skipped": [], "failed": []}


@pytest.mark.asyncio
async def test_checksum_refusal_is_unchanged_and_attempts_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Refusals return before the loop (they were never part of it): untouched."""
    mgr, attempted = _manager(
        monkeypatch, tmp_path, {101: True, 102: True, 103: True}, mismatches=[100]
    )
    result = await mgr._apply_all_pending_locked()  # noqa: SLF001
    assert attempted == []
    assert result == {
        "applied": [],
        "skipped": [],
        "failed": [],
        "refused": {"code": "checksum_mismatch", "numbers": [100]},
    }
