"""The retired Python migration runner remains importable but cannot execute."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.db.migration_base import MigrationError
from backend.services.misc import MigrationRunner as ExportedMigrationRunner
from backend.services.misc.migration_runner import MigrationRunner


def _runner(tmp_path: Path) -> MigrationRunner:
    with pytest.warns(DeprecationWarning, match="MigrationRunner is retired"):
        return MigrationRunner(tmp_path)


def test_package_export_preserves_import_compatibility() -> None:
    assert ExportedMigrationRunner is MigrationRunner


def test_sync_execution_surfaces_fail_closed(tmp_path: Path) -> None:
    runner = _runner(tmp_path)

    for call in (
        runner.discover_migrations,
        lambda: runner.resolve_dependencies({}),
    ):
        with pytest.raises(MigrationError, match="python -m backend.db.migrate apply-all"):
            call()


@pytest.mark.asyncio
async def test_async_execution_surfaces_fail_closed(tmp_path: Path) -> None:
    runner = _runner(tmp_path)

    for call in (
        runner.initialize,
        runner.get_applied_migrations,
        runner.get_pending_migrations,
        runner.apply_all,
        runner.status,
        runner.__aenter__,
    ):
        with pytest.raises(MigrationError, match="python -m backend.db.migrate apply-all"):
            await call()

    await runner.close()
    await runner.__aexit__(None, None, None)
