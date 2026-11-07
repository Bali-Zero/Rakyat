"""Fail-closed compatibility import for the retired Python migration runner.

Schema changes are executed only through python -m backend.db.migrate.
The public MigrationRunner name remains temporarily importable so an old
consumer receives a deterministic migration error instead of silently falling
back to an independent executor.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, NoReturn

from backend.db.migration_base import MigrationError


class MigrationRunner:
    """Retired runner surface preserved only for import compatibility."""

    RETIRED_MESSAGE = (
        "MigrationRunner is retired; use python -m backend.db.migrate "
        "apply-all (or MigrationManager through the canonical CLI)."
    )

    def __init__(self, migrations_dir: Path | None = None) -> None:
        self.migrations_dir = Path(migrations_dir) if migrations_dir is not None else None
        warnings.warn(
            self.RETIRED_MESSAGE,
            DeprecationWarning,
            stacklevel=2,
        )

    @classmethod
    def _retired(cls) -> NoReturn:
        raise MigrationError(cls.RETIRED_MESSAGE)

    async def initialize(self) -> None:
        self._retired()

    async def close(self) -> None:
        """No resources are opened by the compatibility shim."""

    async def __aenter__(self) -> NoReturn:
        self._retired()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def discover_migrations(self) -> NoReturn:
        self._retired()

    def resolve_dependencies(self, migrations: dict[int, type[Any]]) -> NoReturn:
        del migrations
        self._retired()

    async def get_applied_migrations(self) -> NoReturn:
        self._retired()

    async def get_pending_migrations(self, dry_run: bool = False) -> NoReturn:
        del dry_run
        self._retired()

    async def apply_all(
        self,
        dry_run: bool = False,
        stop_on_error: bool = True,
    ) -> NoReturn:
        del dry_run, stop_on_error
        self._retired()

    async def status(self) -> NoReturn:
        self._retired()
