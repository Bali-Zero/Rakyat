#!/usr/bin/env python3
"""Retired execution entrypoint for historical migration 121.

The historical migration definition remains available for compatibility tests.
Schema execution belongs to the canonical SQL v2 executor (migration 347).
"""

import asyncio
import sys

from backend.db.migration_base import MigrationError

RETIRED_MESSAGE = (
    "Legacy migration 121 entrypoint is retired; "
    "use python -m backend.db.migrate apply-all "
    "(SQL v2 migration 347)."
)


async def main() -> None:
    raise MigrationError(RETIRED_MESSAGE)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except MigrationError as exc:
        sys.stderr.write(str(exc) + "\n")
        raise SystemExit(1) from None
