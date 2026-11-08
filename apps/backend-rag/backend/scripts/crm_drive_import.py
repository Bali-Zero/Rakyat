"""Operator-only hidden registration; default read-only plan, no Drive writes.

Run with the backend on PYTHONPATH. DATABASE_URL supplies the operator connection.
Only an explicit --apply --expected-plan-sha256 from a reviewed dry run writes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import warnings
from pathlib import Path


class CLIRejected(ValueError):
    """Public CLI failure code without imported application configuration."""


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        raise CLIRejected("CLI_ARGUMENTS")


async def execute(args):
    import asyncpg

    # Parse --help before application imports; configuration errors must not
    # print validation payloads or provider details outside the safe-code path.
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore")
        from backend.app.core.config import settings
        from backend.services.crm.drive_document_repair import (
            ReadOnlyDrive,
            RepairRejected,
            run_repair,
            validate_manifest,
        )
        from backend.services.integrations.service_account_drive_service import (
            ServiceAccountDriveService,
        )

    path = Path(args.manifest)
    if path.stat().st_size > 131072:
        raise CLIRejected("MANIFEST_SIZE")
    manifest = json.loads(path.read_text())
    try:
        validate_manifest(manifest)
    except RepairRejected as exc:
        raise CLIRejected(str(exc)) from None
    if args.apply and not args.expected_plan_sha256:
        raise CLIRejected("PLAN_DIGEST_REQUIRED")
    conn = await asyncpg.connect(
        os.environ.get("DATABASE_URL") or settings.database_url, timeout=10
    )
    try:
        async with asyncio.timeout(120):
            return await run_repair(
                conn,
                ReadOnlyDrive(ServiceAccountDriveService()),
                manifest,
                apply=args.apply,
                expected_plan_sha256=args.expected_plan_sha256,
                keep_in_place=args.keep_in_place,
            )
    except RepairRejected as exc:
        raise CLIRejected(str(exc)) from None
    finally:
        await conn.close()


def main(argv=None):
    # Provider and SQL exceptions may carry private values. Emit only safe codes.
    logging.disable(logging.CRITICAL)
    parser = SafeParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument(
        "--keep-in-place",
        action="store_true",
        help="Register hidden at its existing location; no future move is planned",
    )
    try:
        args = parser.parse_args(argv)
        result = asyncio.run(execute(args))
    except CLIRejected as exc:
        sys.stdout.write(json.dumps({"status": "rejected", "code": str(exc)}) + "\n")
        return 2
    except Exception:
        sys.stdout.write(json.dumps({"status": "rejected", "code": "OPERATION_FAILED"}) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
