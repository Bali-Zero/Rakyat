#!/usr/bin/env python3
"""Re-enqueue Drive files by id: read again what the changes feed never delivered.

Before the Drive drain kept its cursor (cursor discipline), a saturated tick moved the cursor past
changes it had not handled, and nothing emits those changes again. This CLI takes the Drive file ids
of such files, reads each one with files.get, runs it through the drain's own skips and scope walk and
then either only COUNTS it (dry run, the default: no download, no enqueue) or downloads and enqueues it
(``--apply``; ``enqueue`` is idempotent on ``intake_key``, so running it twice adds nothing). It never
touches the drain's cursor. See ``backend/services/intake/drive_adapter.py::reenqueue_drive_files``.

Usage (on the Pro, with the drain's environment: same service account, database and blob root):
  python scripts/intake_drive_reenqueue.py --ids-file IDS.tsv            # dry run
  python scripts/intake_drive_reenqueue.py --ids-file IDS.tsv --apply    # download + enqueue

Input: a TSV whose FIRST column is a Drive file id (``[A-Za-z0-9_-]{10,100}``); blank lines are skipped,
the other columns are ignored, a repeated id counts once. Any other first column (a header included),
no id at all or more than 500 of them is bad input: exit 2 before any Drive or database call, and the
message names a line NUMBER, never what the line says.

Output: ONE JSON line of counts on stdout (``apply`` 0/1, ``duplicate_ids``, then the counters of the
run: ``requested``, ``would_enqueue_new``, ``enqueued_new``, ``already_present``, ``skipped_*``,
``missing``, ``errors``, ``not_attempted``, ``disk_hold``). Logs go to stderr and carry Drive ids and
counts only, never a file name or a path (``source_path`` goes to the queue row, as in the drain).

Exit: 0 ok; 2 bad input or configuration (nothing was touched); 3 the run had errors, stopped at the
disk floor (``disk_hold=1``) or could not start (database or Drive service).

Env (as the drain): LOCAL_DATABASE_URL (default postgresql://nuzantara@127.0.0.1:5432/nuzantara_dev),
INTAKE_DRIVE_SCOPE_FOLDER_ID (REQUIRED), INTAKE_BLOB_ROOT, INTAKE_DRIVE_MIN_FREE_GIB (default 20: an apply
below it downloads nothing), and the service-account credentials the Drive client reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

BACKEND_RAG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_RAG))

import asyncpg

from backend.services.intake.drive_adapter import (
    _FILE_ID_RE,
    _SCOPE_ENV,
    _failure_summary,
    reenqueue_drive_files,
)

logger = logging.getLogger("intake_drive_reenqueue")

MAX_IDS = 500
MAX_FILE_BYTES = 4 * 1024 * 1024
EXIT_OK, EXIT_BAD_INPUT, EXIT_ERRORS = 0, 2, 3
DEFAULT_DSN = "postgresql://nuzantara@127.0.0.1:5432/nuzantara_dev"


def read_ids(path: Path) -> tuple[list[str], int, str | None]:
    """(ids, repeated ids, problem): the first TSV column of every non-blank line.

    ``problem`` is None when the file is usable, else a sentence that names a line number or a
    count and nothing the file says.
    """
    if not path.is_file():
        return [], 0, "the ids file is not a readable file"
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return [], 0, f"the ids file is larger than {MAX_FILE_BYTES} bytes"
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], 0, "the ids file cannot be read as UTF-8 text"
    ids: list[str] = []
    seen: set[str] = set()
    repeated = 0
    non_blank = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        non_blank += 1
        if non_blank > MAX_IDS:
            return [], 0, f"more than {MAX_IDS} ids"
        first = line.split("\t", 1)[0].strip()
        if _FILE_ID_RE.fullmatch(first) is None:
            return [], 0, f"line {number} does not start with a Drive file id"
        if first in seen:
            repeated += 1
            continue
        seen.add(first)
        ids.append(first)
    if not ids:
        return [], 0, "the ids file has no ids"
    return ids, repeated, None


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--ids-file", required=True, help="TSV whose first column is a Drive file id")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="download and enqueue (default: dry run, counts only)",
    )
    return ap


async def _run(ids: list[str], repeated: int, scope_id: str, *, apply: bool) -> int:
    db_url = os.environ.get("LOCAL_DATABASE_URL", DEFAULT_DSN)
    try:
        pool = await asyncpg.create_pool(dsn=db_url, min_size=1, max_size=2)
    except Exception as exc:
        why = _failure_summary(exc)
        logger.error("cannot open the intake database (%s)", why)
        return EXIT_ERRORS
    try:
        try:
            from backend.services.integrations.service_account_drive_service import (
                ServiceAccountDriveService,
            )

            drive_service = ServiceAccountDriveService()
        except Exception as exc:
            why = _failure_summary(exc)
            logger.error("Drive service unavailable (%s)", why)
            return EXIT_ERRORS
        try:
            counters = await reenqueue_drive_files(pool, drive_service, scope_id, ids, apply=apply)
        except Exception as exc:
            # The run logs the faults it handles; one that gets past it would end in the interpreter's
            # traceback, whose message can hold a file or folder name.
            why = _failure_summary(exc)
            logger.error("re-enqueue failed (%s)", why)
            return EXIT_ERRORS
    finally:
        await pool.close()
    print(json.dumps({"apply": int(apply), "duplicate_ids": repeated, **counters}, sort_keys=True))
    return EXIT_ERRORS if counters["errors"] or counters["disk_hold"] else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ids, repeated, problem = read_ids(Path(args.ids_file).expanduser())
    if problem is not None:
        print(f"intake_drive_reenqueue: {problem}", file=sys.stderr)
        return EXIT_BAD_INPUT
    scope_id = os.environ.get(_SCOPE_ENV, "").strip()
    if not scope_id:
        print(f"intake_drive_reenqueue: {_SCOPE_ENV} is not set", file=sys.stderr)
        return EXIT_BAD_INPUT
    return asyncio.run(_run(ids, repeated, scope_id, apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
