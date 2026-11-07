#!/usr/bin/env python3
"""Re-enqueue Drive files by id: read again what the changes feed never delivered.

Before the Drive drain kept its cursor (cursor discipline), a saturated tick moved the cursor past
changes it had not handled, and nothing emits those changes again. This CLI takes the Drive file ids
of such files, reads each one with files.get, runs it through the drain's own skips and scope walk and
then either only COUNTS it (dry run, the default: no download, no enqueue) or downloads and enqueues it
(``--apply``; ``enqueue`` is idempotent on ``intake_key``, so running it twice adds nothing). It never
touches the drain's cursor. See ``backend/services/intake/drive_adapter.py::reenqueue_drive_files``.

The dry run is an estimate: it asks the queue by ``source_ref`` (``drive:<file id>``), while the apply
decides by ``intake_key`` (source, ref, blob hash, pipeline version). The two agree for a file that has no
row yet. A file that was queued once and has changed in Drive since reads ``already_present`` in a dry run
and still gets a new row on apply.

Before an apply, read the dry run's JSON line. Its keys are of two kinds. The OUTCOME keys say what became
of each id: ``would_enqueue_new``, ``enqueued_new``, ``already_present``, ``skipped_removed``,
``skipped_native``, ``skipped_out_of_scope``, ``missing``, ``errors`` and ``not_attempted`` (a dry run fills
``would_enqueue_new`` and an apply ``enqueued_new``; every id lands in exactly one of the nine). The
METADATA keys describe the run and are never outcomes: ``apply``, ``duplicate_ids``, ``requested`` and
``disk_hold``. Go on to ``--apply`` only when ``errors`` and ``skipped_out_of_scope`` are both 0 and the
OUTCOME keys add up to ``requested``, the number of distinct ids read (a repeated id is counted once, in
``duplicate_ids``); otherwise stop and look. ``already_present`` is not a reason to stop: an apply adds no
second row for a file whose content has not changed (``intake_key``). Nor are ``missing``,
``skipped_removed`` and ``skipped_native``, since a file can be trashed, converted or deleted between two
looks: each one only means one id fewer for ``would_enqueue_new``.

Usage (on the Pro, with the drain's environment: same service account, database and blob root):
  python scripts/intake_drive_reenqueue.py --ids-file IDS.tsv            # dry run
  python scripts/intake_drive_reenqueue.py --ids-file IDS.tsv --apply    # download + enqueue
``drive-intake-reenqueue-run.sh``, in the repo's root ``scripts`` directory, is the drain wrapper's twin: it
reads the service account from its 0600 file, moves to apps/backend-rag and passes every argument on.

Input: a TSV whose FIRST column is a Drive file id (``[A-Za-z0-9_-]{10,100}``), checked exactly as it is
written: nothing is trimmed, so an id padded with a space, a tab or any other whitespace is bad input. A
line of only whitespace is blank and skipped, the other columns are ignored, a repeated id counts once.
Any other first column (a header included), no id at all or more than 500 of them is bad input: exit 2
before any Drive or database call, and the message names a line NUMBER, never what the line says.

Output: ONE JSON line of counts on stdout (``apply`` 0/1, ``duplicate_ids``, then the counters of the
run: ``requested``, ``would_enqueue_new``, ``enqueued_new``, ``already_present``, ``skipped_*``,
``missing``, ``errors``, ``not_attempted``, ``disk_hold``). Logs go to stderr and carry Drive ids,
counts and exception class names only, never a file name, a path, a Workspace address or the text of an
exception (``source_path`` goes to the queue row, as in the drain). That holds where each record is
created: a name never enters a record, so no handler, this one or any that other code attaches anywhere,
can print one. This CLI and the Drive adapter log that way, and so does the Drive client since #9236. The
one handler this CLI installs is terminal hygiene only: it prints the lines of this CLI and of the Drive
adapter and keeps the noise of every other logger off the terminal (below WARNING a record is dropped,
from WARNING up it becomes a line that names the logger and the exception class).

Exit: 0 ok; 2 bad input or configuration (nothing was touched); 3 the run had errors, stopped at the
disk floor (``disk_hold=1``), could not start (database or Drive service) or could not close the database.

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
import re
import sys
import time
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

# Names never enter a log record on this path: this CLI and the Drive adapter log Drive ids, counts and
# exception class names, and the Drive client does the same since #9236 (it used to put the folder names and
# the raw text of a failed check in its start-up lines). That is the guarantee, held where a record is
# created: a handler cannot hold it, because any code can attach one anywhere. The handler below is terminal
# hygiene: it prints the lines of the two loggers named here and keeps every other logger's noise off the
# terminal.
_SAFE_LOGGERS = frozenset({"intake_drive_reenqueue", "backend.services.intake.drive_adapter"})
_HANDLER_NAME = "intake_drive_reenqueue.safe_stderr"
_PLAIN = re.compile(r"[A-Za-z0-9_.]{1,100}")


def _plain(word: object) -> str:
    """A logger, level or class name when it is a plain identifier, else a question mark."""
    return word if isinstance(word, str) and _PLAIN.fullmatch(word) else "?"


class _SafeStderr(logging.Handler):
    """What this process prints on stderr as a log line: terminal hygiene, not a privacy guarantee.

    It does not keep names out of the logs (they never enter a record: see the comment above) and it is not
    the only way a record leaves this process: a handler attached anywhere else is handed every record, as
    it always was. The two loggers of _SAFE_LOGGERS are printed as they are, without any traceback. A record
    of any other logger (the Drive client, Google's libraries, the database driver) is dropped below
    WARNING, and at WARNING or above is replaced by a constant line that names the logger and, when the
    record carried one, the exception CLASS: never its message, its text or its traceback.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            said = self._said(record)
            if said is not None:
                sys.stderr.write(said + "\n")
        except Exception:
            return  # no log line is worth a crash, and nothing may be written about why

    @staticmethod
    def _said(record: logging.LogRecord) -> str | None:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        head = f"{when},{int(record.msecs):03d} {_plain(record.levelname)}"
        if record.name in _SAFE_LOGGERS:
            return f"{head} {record.name} {record.getMessage()}"
        if record.levelno < logging.WARNING:
            return None
        kind = ""
        if record.exc_info and record.exc_info[0] is not None:
            kind = f" ({_plain(record.exc_info[0].__name__)})"
        return f"{head} {_plain(record.name)} a record of this logger was withheld{kind}"


def _install_safe_logging() -> None:
    """Put _SafeStderr on the root and take the other terminal handlers of the root off it.

    Hygiene: a third-party record is then printed once, as a constant line, and not also whole by a handler
    somebody left on the root. Handlers anywhere else are left alone.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        stream = getattr(handler, "stream", None)
        to_the_terminal = isinstance(handler, logging.StreamHandler) and stream in (
            sys.stdout,
            sys.stderr,
            sys.__stdout__,
            sys.__stderr__,
        )
        if to_the_terminal or handler.get_name() == _HANDLER_NAME:
            root.removeHandler(handler)
    safe = _SafeStderr()
    safe.set_name(_HANDLER_NAME)
    root.addHandler(safe)
    for name in _SAFE_LOGGERS:
        speaker = logging.getLogger(name)
        if speaker.getEffectiveLevel() > logging.INFO:
            speaker.setLevel(logging.INFO)


def read_ids(path: Path) -> tuple[list[str], int, str | None]:
    """(ids, repeated ids, problem): the first TSV column of every non-blank line, exactly as written.

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
        first = line.split("\t", 1)[0]  # as written: a padded id is not an id
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


async def _close(pool: asyncpg.Pool) -> bool:
    """Close the pool. A close that fails fails the run, said by class and place and never by its message."""
    try:
        await pool.close()
    except Exception as exc:
        why = _failure_summary(exc)
        logger.error("closing the intake database failed (%s)", why)
        return False
    return True


async def _re_enqueue(
    pool: asyncpg.Pool, ids: list[str], scope_id: str, *, apply: bool
) -> dict[str, int] | None:
    """The counters of the run, or None when it could not be done (the reason is logged by class and place)."""
    try:
        from backend.services.integrations.service_account_drive_service import (
            ServiceAccountDriveService,
        )

        drive_service = ServiceAccountDriveService()
    except Exception as exc:
        why = _failure_summary(exc)
        logger.error("Drive service unavailable (%s)", why)
        return None
    try:
        return await reenqueue_drive_files(pool, drive_service, scope_id, ids, apply=apply)
    except Exception as exc:
        # The run logs the faults it handles; one that gets past it would end in the interpreter's
        # traceback, whose message can hold a file or folder name.
        why = _failure_summary(exc)
        logger.error("re-enqueue failed (%s)", why)
        return None


async def _run(ids: list[str], repeated: int, scope_id: str, *, apply: bool) -> int:
    db_url = os.environ.get("LOCAL_DATABASE_URL", DEFAULT_DSN)
    try:
        pool = await asyncpg.create_pool(dsn=db_url, min_size=1, max_size=2)
    except Exception as exc:
        why = _failure_summary(exc)
        logger.error("cannot open the intake database (%s)", why)
        return EXIT_ERRORS
    try:
        counters = await _re_enqueue(pool, ids, scope_id, apply=apply)
    finally:
        closed = await _close(pool)
    if counters is None:
        return EXIT_ERRORS
    print(json.dumps({"apply": int(apply), "duplicate_ids": repeated, **counters}, sort_keys=True))
    return EXIT_ERRORS if counters["errors"] or counters["disk_hold"] or not closed else EXIT_OK


def _main(args: argparse.Namespace) -> int:
    ids, repeated, problem = read_ids(Path(args.ids_file).expanduser())
    if problem is not None:
        print(f"intake_drive_reenqueue: {problem}", file=sys.stderr)
        return EXIT_BAD_INPUT
    scope_id = os.environ.get(_SCOPE_ENV, "").strip()
    if not scope_id:
        print(f"intake_drive_reenqueue: {_SCOPE_ENV} is not set", file=sys.stderr)
        return EXIT_BAD_INPUT
    return asyncio.run(_run(ids, repeated, scope_id, apply=args.apply))


def main(argv: list[str] | None = None) -> int:
    # --help and a usage error leave through SystemExit, as they should
    args = _parser().parse_args(argv)
    try:
        _install_safe_logging()
        return _main(args)
    except Exception as exc:
        # Whatever got this far is not an outcome the run knows: the interpreter's traceback would carry the
        # exception's message, which can hold a file or folder name.
        why = _failure_summary(exc)
        logger.error("stopped on an unexpected fault (%s)", why)
        return EXIT_ERRORS


if __name__ == "__main__":
    raise SystemExit(main())
