"""Drive intake adapter (FASE 1B, scoped + path-aware since m227).

Forks the blob-enumeration logic of the existing Drive poller
(`backend/services/crm/drive_poll_service.py` + `ServiceAccountDriveService.
list_changes_since`) but does NOT process/route into CRM — it downloads each
changed file to a local intake blob dir and calls enqueue().

Scope (m227): the adapter REQUIRES ``INTAKE_DRIVE_SCOPE_FOLDER_ID`` (the Drive
folder id of the watched root, e.g. Dropbox-Intake/). The Drive changes API is
account-wide — without the scope filter this adapter would ingest the codebase
mirror, the nightly backups and every other Drive write. Files whose ancestor
chain does not reach the scope folder are skipped. The ancestor walk also yields
the path RELATIVE to the scope root ("<Client Name>/passport.jpg"), persisted as
``intake_queue.source_path`` — the folder-name entity signal for FASE-4 routing.

ZERO CRM writes. PII stays local: blobs are written under INTAKE_BLOB_ROOT on
the Pro, never shipped to cloud LLMs here. Cursor: this adapter advances its
OWN ``system_settings`` key ``intake_drive_page_token`` so it does not fight
the production poller (``drive_poll_page_token``).

Cursor discipline (no change is ever read and dropped): a page of the changes feed is
ONE bounded fetch (``page_size=limit``, ``max_pages=1``); EVERY change it returns is
processed and the feed's own continuation is stored as the cursor (the page's
``nextPageToken`` while more pages remain, ``newStartPageToken`` once the feed is
drained). The next page resumes exactly there. A page with a
per-change failure HOLDS the cursor so the page is re-listed and retried
(``enqueue`` is idempotent). The hold has no tick limit for anything that can heal
by waiting (network, timeout, 5xx, 429, rate limits, DB or enqueue failures): a
system fault must never cost a page. Only a Drive error that belongs to ONE file
and cannot heal (``_PERMANENT_FILE_REASONS``) is let go, after ``_MAX_HELD_TICKS``
failing ticks, only on a page where another change WAS downloaded and queued, and loudly
(ERROR with the file ids, ``gave_up=N`` on the done line). A page where nothing succeeds
holds the cursor however permanent its errors look: a service account that lost its access
must not cost a page every few ticks.
The scope check obeys the same rule: an ancestor lookup that FAILS (5xx, 429, network...)
says nothing about the scope, so it is a failure of that change (retried, cursor held), never
"out of scope"; only Drive's own 404 notFound for an ancestor makes a file unreachable.
A walk that cannot finish (a chain deeper than ``_MAX_ANCESTOR_DEPTH``, or a cycle) proves
nothing either way and fails the change too. Logs never carry an exception's text or
traceback (they can hold a file or folder name): only opaque ids, the exception class,
Drive's status and machine reason, and where it was raised (``_failure_summary``).
Pace: a tick walks pages, each handled as above with its own cursor write, until the feed
ends, a page is HELD (the tick stops there: the cursor never skips a held page),
``INTAKE_DRIVE_DRAIN_MAX_PAGES`` (20) pages are done, or ``INTAKE_DRIVE_DRAIN_BUDGET_S`` (300)
seconds have passed, counted from the start of the call. The budget is checked between pages,
never inside one, and once more right before each page after the first (the disk probe and the
done line of the page before spend it too); the first page always runs. It paces the walk, it is
not a wall-clock limit: the service build, the disk probe, the logging and a page already started
have no time limit of their own, so a tick USUALLY lasts about the budget plus one page and has no
absolute ceiling (launchd counts its interval from the end of a tick: a long tick delays the next
one and never overlaps it). Before every page the free space of the blob volume is checked: below
``INTAKE_DRIVE_MIN_FREE_GIB`` (20) the tick downloads nothing and holds the cursor, loudly (ERROR,
``disk_hold=1`` on the done line, ``errors`` + 1): the faster pace must not fill the disk again.
The guard cannot be turned off: the floor is an exact decimal (never ``float()``, see
``_floor_bytes``), anything that is not a positive number is the default with a WARNING, and to
loosen it you set a small positive number such as 1.
Each page logs its own done line (a page that was not started, because of the disk or of the
budget, gets one too, with zero counters and the one error of a disk hold), so the counters of
the done lines add up to the ones the tick returns; a line's ``from_token`` is the previous
``next_token``. The last done line says why the tick ended, except when the listing of a page
fails: that ends the tick without a line of its own (nothing is lost, the cursor stays where it
was) and its "Drive enumeration failed" WARNING is the terminal signal. A tick that cannot start
(no scope folder, no Drive service) has no done line either; its WARNING says why.
Re-enqueue by file id (``reenqueue_drive_files``): a change the feed dropped before the cursor discipline
above, which nothing emits again, is read again by its Drive file id. Each id is read with files.get, goes
through the SAME skips and scope walk as a change of the drain (``_ingest_change``) and is then only counted
(dry run: no download, no enqueue; the queue row is looked up by ``source_ref``) or downloaded and enqueued
(apply: ``enqueue`` is idempotent on ``intake_key``, so a second apply adds nothing). The cursor is never
touched. Ids go one at a time with a short pause; before each id of an apply the disk floor is checked (below
it the run stops, loudly, ``disk_hold=1``); a file Drive no longer serves is ``missing``, any other fault
``errors``, with no retry. Logs carry ids and counts only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import shutil
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Context, Decimal, InvalidOperation, Overflow
from pathlib import Path
from typing import Any

import asyncpg

from backend.services.intake.enqueue import enqueue

logger = logging.getLogger(__name__)

_SOURCE = "drive"
_CURSOR_KEY = "intake_drive_page_token"

# Drive folder id of the watched root (e.g. Dropbox-Intake/). REQUIRED —
# fail-safe: without it the adapter refuses to ingest anything.
_SCOPE_ENV = "INTAKE_DRIVE_SCOPE_FOLDER_ID"

# Local blob landing dir (PII stays on the Pro). Override via env.
_BLOB_ROOT = Path(
    os.environ.get("INTAKE_BLOB_ROOT", os.path.expanduser("~/.nuzantara/intake-blobs"))
) / "drive"

# Drive native/Google-Doc mime types we skip (no binary blob to hash).
_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps"

# Max parent-chain hops. Drive nests at most 100 levels, so a longer chain (or a cycle) is an
# anomaly: the walk fails the change and the cursor holds, it never calls the file out of scope.
_MAX_ANCESTOR_DEPTH = 128

# A Drive error that belongs to ONE file and cannot heal by waiting: the machine reasons that
# are permanent for each HTTP status. Only these may ever cost a change. Every other failure
# (network, timeout, 5xx, 429, rate-limit 403s, DB or enqueue errors, any other reason) holds
# the cursor with no tick limit: a system fault must never cost a page.
_PERMANENT_FILE_REASONS = {
    403: frozenset({"cannotDownloadAbusiveFile", "forbidden", "fileNotDownloadable"}),
    404: frozenset({"notFound"}),
}

# Failing ticks at one cursor after which, in a tick that also had a success, the files whose
# errors are ALL permanent are let go.
_MAX_HELD_TICKS = 3

# Pace of one tick (every value can be overridden in the environment; an argument beats it).
# The budget is checked BETWEEN pages (again right before each page after the first) and the first
# page always runs, so a tick usually lasts about the budget plus one page: an estimate, not a
# ceiling (the service build, the disk probe, the logging and a page already started have no time
# limit). Measured on the Pro (4,939 ticks): 2.2 s per downloaded file and 0.2 s per skipped change;
# Drive returns about 50 changes a page, so ~110 s of downloads (130 s is the slow-page figure). A
# test pins the default budget plus such a page under the LaunchAgent's StartInterval (600 s);
# launchd counts it from the END of a tick, so a long tick delays the next and never overlaps it.
# The floor keeps the faster pace from filling the blob volume.
_BUDGET_ENV = "INTAKE_DRIVE_DRAIN_BUDGET_S"
_DEFAULT_BUDGET_S = 300.0
_MAX_PAGES_ENV = "INTAKE_DRIVE_DRAIN_MAX_PAGES"
_DEFAULT_MAX_PAGES = 20
_MIN_FREE_ENV = "INTAKE_DRIVE_MIN_FREE_GIB"
_DEFAULT_MIN_FREE_GIB = 20
_GIB = 2**30
# The floor is parsed as an exact decimal. One beyond 10**4000 bytes (a typo, no volume is that
# big) raises Overflow in the decimal arithmetic and is the default with a WARNING.
_FLOOR_EMAX = 4000

# A Drive file id as the Drive API issues them. The ids of a re-enqueue come from a file somebody wrote, and
# an id becomes part of a path on disk and of a queue key: anything else is refused before any Drive call.
_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,100}")

# Pause between two ids of a re-enqueue: one id at a time, no burst against the Drive API.
_REENQUEUE_PAUSE_S = 0.25


def _local_blob_path(file_id: str, file_name: str) -> Path:
    safe_name = file_name.replace("/", "_") if file_name else file_id
    return _BLOB_ROOT / file_id[:2] / f"{file_id}__{safe_name}"


def _write_all(fd: int, data: bytes) -> None:
    """os.write until every byte is out: one call may take fewer (the seam the tests break a write at)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("a write made no progress")
        view = view[written:]


def _write_atomically(dest: Path, data: bytes) -> None:
    """Put ``data`` at ``dest`` in one step: it is staged beside the destination and renamed onto it.

    A write that fails (disk full, an I/O error, an interrupt) leaves no file at ``dest`` and no stage: a
    blob already there is never truncated and a missing one never shows up half written. The stage lives in
    the destination's directory (so the rename stays inside one filesystem), carries a name of its own that
    holds no part of the Drive name, and gets the mode a plain write of the file would have given it.
    Atomic against a failed write, not against a power cut: nothing is fsynced, as before.
    """
    stage = dest.parent / f".stage-{os.urandom(8).hex()}.part"
    fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
        os.replace(stage, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(stage)
        raise


async def _download_file(drive_service, file_id: str, dest: Path) -> int:
    """Download Drive file bytes to dest via get_media. Returns byte size.

    The file appears at ``dest`` whole or not at all (``_write_atomically``): the drain and the re-enqueue
    both come through here, and a blob that ``enqueue`` hashes must never be a truncated one.
    """
    import asyncio
    import io

    from googleapiclient.http import MediaIoBaseDownload

    def _blocking_download() -> bytes:
        request = drive_service.service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buf.getvalue()

    data = await asyncio.to_thread(_blocking_download)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(dest, data)
    return len(data)


async def _folder_meta(drive_service, folder_id: str) -> dict[str, Any]:
    """files.get for one ancestor folder (id, name, parents)."""
    import asyncio

    def _blocking_get() -> dict[str, Any]:
        return (
            drive_service.service.files()
            .get(fileId=folder_id, fields="id, name, parents", supportsAllDrives=True)
            .execute()
        )

    return await asyncio.to_thread(_blocking_get)


async def _file_meta(drive_service, file_id: str) -> dict[str, Any]:
    """files.get for one file, with the fields a change of the feed carries (see list_changes_since)."""
    import asyncio

    def _blocking_get() -> dict[str, Any]:
        return (
            drive_service.service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, parents, trashed",
                supportsAllDrives=True,
            )
            .execute()
        )

    return await asyncio.to_thread(_blocking_get)


async def _resolve_relative_dir(
    drive_service,
    parents: list[str] | None,
    scope_id: str,
    cache: dict[str, dict[str, Any]],
) -> str | None:
    """Walk the parent chain up to the scope folder.

    Returns the folder path RELATIVE to the scope root ("" for a direct child,
    "A/B" for nested), or None when the chain ends without reaching ``scope_id`` or an
    ancestor no longer exists (Drive 404 notFound): out of scope. Any OTHER
    lookup failure (5xx, 429, network...) RAISES: it says nothing about the
    scope, so the caller retries the change instead of dropping it. So does a walk
    that cannot finish (more than ``_MAX_ANCESTOR_DEPTH`` levels, or a cycle): it
    proves nothing either way. ``cache`` memoizes folder lookups within one drain tick.
    """
    cur = (parents or [None])[0]
    segments: list[str] = []
    seen: set[str] = set()
    while cur:
        if cur == scope_id:
            return "/".join(reversed(segments))
        if cur in seen or len(segments) >= _MAX_ANCESTOR_DEPTH:
            raise _AncestorChainError(len(segments), cycle=cur in seen)
        seen.add(cur)
        meta = cache.get(cur)
        if meta is None:
            try:
                meta = await _folder_meta(drive_service, cur)
            except Exception as exc:
                if not _is_ancestor_gone(exc):
                    raise
                why = _failure_summary(exc)
                logger.warning(
                    "drain_drive: ancestor %s no longer exists, out of scope (%s)", cur, why
                )
                return None
            cache[cur] = meta
        segments.append(meta.get("name") or cur)
        cur = (meta.get("parents") or [None])[0]
    return None


def _is_permanent_file_error(exc: BaseException) -> bool:
    """True for a Drive error that belongs to one file and cannot heal by waiting.

    Fails closed: the status must have permanent reasons, the body must carry at least one
    reason, EVERY reason must be permanent for that status (a rate-limit reason hidden behind
    a permanent one, in any position, makes it transient) and the quota status
    RESOURCE_EXHAUSTED never qualifies.
    """
    allowed = _PERMANENT_FILE_REASONS.get(_http_status(exc))
    reasons = _drive_error_reasons(exc)
    body = _drive_error_body(exc) or {}
    if not allowed or not reasons or body.get("status") == "RESOURCE_EXHAUSTED":
        return False
    return all(reason in allowed for reason in reasons)


def _is_ancestor_gone(exc: BaseException) -> bool:
    """True when Drive itself says the folder does not exist (404, and only notFound reasons)."""
    return _http_status(exc) == 404 and _is_permanent_file_error(exc)


class _AncestorChainError(Exception):
    """The ancestor walk could not prove the scope either way (too deep, or a cycle)."""

    def __init__(self, depth: int, *, cycle: bool) -> None:
        super().__init__("ancestor chain unresolved")
        self.depth = depth
        self.cycle = cycle


def _http_status(exc: BaseException) -> int | None:
    """The numeric HTTP status of a googleapiclient error, None for anything else."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status if type(status) is int else None


def _drive_error_body(exc: BaseException) -> dict[str, Any] | None:
    """The ``error`` object of a Drive error body, None when the body is not Drive's own shape."""
    try:
        body = json.loads(getattr(exc, "content", None))["error"]
    except (TypeError, ValueError, KeyError):
        return None
    return body if isinstance(body, dict) else None


def _drive_error_reasons(exc: BaseException) -> list[str]:
    """EVERY machine reason of a Drive error ("forbidden", "rateLimitExceeded"...).

    Empty when the body has none or any entry is malformed: a reason we cannot read is never
    taken as an answer.
    """
    errors = (_drive_error_body(exc) or {}).get("errors")
    if not isinstance(errors, list):
        return []
    reasons = [item.get("reason") if isinstance(item, dict) else None for item in errors]
    return reasons if all(isinstance(reason, str) for reason in reasons) else []


# Drive's own machine reasons: the only ones a log line repeats (any other is logged as "other").
_LOGGABLE_REASONS = frozenset(
    {
        "authError",
        "backendError",
        "badRequest",
        "cannotDownloadAbusiveFile",
        "dailyLimitExceeded",
        "downloadQuotaExceeded",
        "fileNotDownloadable",
        "forbidden",
        "insufficientFilePermissions",
        "internalError",
        "notFound",
        "rateLimitExceeded",
        "sharingRateLimitExceeded",
        "userRateLimitExceeded",
    }
)


def _failure_summary(exc: BaseException) -> str:
    """What a log line may say about a failure: its class, Drive's status and reason, where it was raised.

    NEVER its message or its traceback text: both can carry a file or folder name.
    """
    parts = [type(exc).__name__]
    status = _http_status(exc)
    if status is not None:
        parts.append(f"status={status}")
    reasons = _drive_error_reasons(exc)
    if reasons:
        shown = (r if r in _LOGGABLE_REASONS else "other" for r in reasons)
        parts.append("reason=" + ",".join(shown))
    if isinstance(exc, _AncestorChainError):
        parts.append(f"depth={exc.depth}" + (" cycle" if exc.cycle else ""))
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        parts.append(f"at {Path(frames[-1].filename).name}:{frames[-1].lineno}")
    return " ".join(parts)


async def _store_cursor(pool: asyncpg.Pool, token: str, *, held: int = 0) -> None:
    """Idempotent upsert of the intake cursor; ``held`` counts consecutive failing ticks at ``token``."""
    value: dict[str, Any] = {"token": token}
    if held:
        value["held"] = held
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            _CURSOR_KEY,
            json.dumps(value),
        )


def _parse_cursor(raw: Any) -> tuple[str | None, int]:
    """(token, held) out of the stored cursor row; anything unusable reseeds with NO hold count.

    ``held`` counts consecutive failing ticks AT ``token``, so it only means something next to a
    usable token (a non-empty string), and only as a plain non-negative integer: JSON ``true``,
    floats, strings and negative numbers are 0.
    """
    if not isinstance(raw, str):
        return None, 0
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, 0
    token = parsed.get("token") if isinstance(parsed, dict) else None
    if not isinstance(token, str) or not token:
        return None, 0
    held = parsed.get("held")
    return token, held if type(held) is int and held >= 0 else 0


def _new_counters() -> dict[str, int]:
    return {
        "changes": 0,
        "enqueued_new": 0,
        "already_present": 0,
        "skipped_native": 0,
        "skipped_removed": 0,
        "skipped_out_of_scope": 0,
        "errors": 0,
    }


def _new_reenqueue_counters(requested: int) -> dict[str, int]:
    """The counters of a re-enqueue. Each requested id lands in exactly one of them, except
    ``disk_hold`` (a flag) and ``requested`` itself."""
    return {
        "requested": requested,
        "would_enqueue_new": 0,
        "enqueued_new": 0,
        "already_present": 0,
        "skipped_native": 0,
        "skipped_removed": 0,
        "skipped_out_of_scope": 0,
        "missing": 0,
        "errors": 0,
        "not_attempted": 0,
        "disk_hold": 0,
    }


def _clock() -> float:
    return time.monotonic()


def _finite(value: float, name: str, default: float) -> float:
    """``value`` when it is a finite number, else the default (WARNING names the setting): a NaN
    would make every comparison false and switch the limit it sets off. An int is finite however
    large and comes back as typed: ``math.isfinite`` would turn it into a float first and raise
    OverflowError past about 1.8e308."""
    if isinstance(value, int) or math.isfinite(value):
        return value
    logger.warning("drain_drive: %s is not a finite number, using the default %s", name, default)
    return default


def _env_number(name: str, default: float) -> float:
    """A finite number from the environment; anything else is the default (WARNING names the variable)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    return _finite(value, name, default)


def _floor_bytes(min_free_gib: Any) -> int:
    """The free-space floor in bytes, ALWAYS at least 1: the disk guard has no off switch.

    ``INTAKE_DRIVE_MIN_FREE_GIB`` (or the argument, which beats it) is read as an exact decimal and
    never through ``float()``: ``float("1e-324")`` is 0.0 and ``float("-1e-324")`` is -0.0, and a
    floor of 0 would be no floor. Missing or blank: the default, quietly. Not a number, NaN, sNaN,
    infinity, a bool, zero (``-0`` included) or any negative however small, or a size the decimal
    arithmetic cannot hold (beyond ``_FLOOR_EMAX``): the default and a WARNING that names the
    variable and nothing else. Any positive number, however small, is ``ceil(gib * 2**30)`` bytes,
    at least one; a floor larger than any volume is legal and holds every tick (``stop=disk``).
    """
    default = _DEFAULT_MIN_FREE_GIB * _GIB
    if min_free_gib is None:
        raw = os.environ.get(_MIN_FREE_ENV, "").strip()
        if not raw:
            return default
        min_free_gib = raw
    try:
        if isinstance(min_free_gib, bool):
            raise TypeError("a bool is not a size")
        gib = Decimal(min_free_gib)
        if not gib.is_finite() or gib <= 0:
            raise ValueError("not a positive finite number")
        exact = Context(
            prec=len(gib.as_tuple().digits) + 12,
            rounding=ROUND_CEILING,
            Emax=_FLOOR_EMAX,
            Emin=-_FLOOR_EMAX,
            traps=[InvalidOperation, Overflow],
        )
        product = exact.multiply(gib, _GIB)
        return max(1, int(product.to_integral_value(ROUND_CEILING, exact)))
    except (TypeError, ValueError, ArithmeticError):
        logger.warning(
            "drain_drive: %s is not a positive number, using the default %s GiB",
            _MIN_FREE_ENV,
            _DEFAULT_MIN_FREE_GIB,
        )
        return default


def _drain_settings(
    budget_s: float | None, max_pages: int | None, min_free_gib: float | Decimal | None
) -> tuple[float, int, int]:
    """(budget in seconds, page cap, free-space floor in bytes); an argument beats the environment.

    No value turns a limit off or empties a tick. The budget and the page cap go through
    ``float()`` on purpose: an underflow (``1e-324``) is 0.0 and a negative one -0.0, which are the
    TIGHTEST settings (one page), so the lossy conversion errs on the safe side; NaN and infinity
    are the default (WARNING). A huge finite value is honoured as typed. The disk floor does not go
    through ``float()``, where an underflow to zero would be the LOOSEST setting: see ``_floor_bytes``.
    """
    budget = (
        _env_number(_BUDGET_ENV, _DEFAULT_BUDGET_S)
        if budget_s is None
        else _finite(budget_s, _BUDGET_ENV, _DEFAULT_BUDGET_S)
    )
    pages = (
        _env_number(_MAX_PAGES_ENV, _DEFAULT_MAX_PAGES)
        if max_pages is None
        else _finite(max_pages, _MAX_PAGES_ENV, _DEFAULT_MAX_PAGES)
    )
    return budget, max(1, int(pages)), _floor_bytes(min_free_gib)


def _free_bytes() -> int:
    """Free bytes on the volume of the blob root (its nearest existing parent: the dir is made lazily)."""
    probe = _BLOB_ROOT
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _gib_text(n_bytes: int) -> str:
    """Bytes as GiB, one decimal; a size that is tiny (a floor of one byte), huge, or beyond a float
    (a floor typed as 1e400 GiB) in scientific notation, so the line that reports it never raises."""
    try:
        gib = n_bytes / _GIB
    except OverflowError:
        return f"{Decimal(n_bytes) / _GIB:.3e}"
    return f"{gib:.1f}" if gib == 0 or 0.05 <= gib < 1e15 else f"{gib:.3e}"


def _disk_floor_breach(min_free_bytes: int) -> str | None:
    """None when the blob volume has room, else why a page must not download. There is no way to
    skip the probe, and a volume that cannot be read holds like a full one."""
    try:
        free = _free_bytes()
    except Exception as exc:
        why = _failure_summary(exc)
        return f"cannot read the free disk space ({why})"
    if free < min_free_bytes:
        return (
            f"free disk space {_gib_text(free)} GiB is below "
            f"the {_gib_text(min_free_bytes)} GiB floor"
        )
    return None


@dataclass
class _Page:
    """What one page of the feed did: its own counters and where the cursor sits now."""

    counters: dict[str, int]
    from_token: str | None
    cursor: str | None
    more_pages: bool | None
    gave_up: int = 0
    advanced: bool = False
    held: bool = False
    listed: bool = True


async def _already_queued(pool: asyncpg.Pool, file_id: str) -> bool:
    """True when the queue has a row for this Drive file, whatever its content or stage.

    Only a plain True or False is an answer: anything else raises, so a dry run never guesses.
    """
    async with pool.acquire() as conn:
        found = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM intake_queue WHERE source_ref = $1)", f"drive:{file_id}"
        )
    if found is True or found is False:
        return found
    raise TypeError("the queue lookup did not answer with a boolean")


async def _ingest_change(
    pool: asyncpg.Pool,
    drive_service,
    scope_id: str,
    change: dict[str, Any],
    folder_cache: dict[str, dict[str, Any]],
    counters: dict[str, int],
    failures: list[tuple[str, bool]],
    *,
    dry_run: bool = False,
) -> None:
    """ONE change of the feed: skip it, or scope-check, download and enqueue it.

    The body of the drain loop, moved here unchanged. It counts what it did in ``counters`` and
    appends a change that failed to ``failures`` as ``(file_id, permanent Drive error of that
    file?)``: a Drive, disk or DB fault of its own change is recorded there, never raised.
    ``dry_run`` (a re-enqueue that only looks; the drain never sets it) stops after the scope walk:
    no download, no enqueue, and the file counts as ``would_enqueue_new`` or ``already_present``.
    """
    if change.get("removed"):
        counters["skipped_removed"] += 1
        return
    file_meta = change.get("file") or {}
    file_id = change.get("fileId") or file_meta.get("id")
    if not file_id or file_meta.get("trashed"):
        counters["skipped_removed"] += 1
        return
    mime_type = file_meta.get("mimeType", "")
    if mime_type.startswith(_GOOGLE_NATIVE_PREFIX):
        # Google-native docs have no downloadable binary blob to hash.
        # (Folders are google-apps.folder → also skipped here.)
        counters["skipped_native"] += 1
        return

    # Scope filter (m227): only files whose ancestor chain reaches the
    # watched root are ingested; the walk yields the relative folder path.
    try:
        rel_dir = await _resolve_relative_dir(
            drive_service, file_meta.get("parents"), scope_id, folder_cache
        )
    except Exception as exc:
        # A lookup that FAILED says nothing about the scope: retry the change (cursor
        # held, same bookkeeping as a failed download), never count it out of scope.
        why = _failure_summary(exc)
        logger.warning("drain_drive: ancestor walk failed for file_id=%s (%s)", file_id, why)
        counters["errors"] += 1
        failures.append((file_id, _is_permanent_file_error(exc)))
        return
    if rel_dir is None:
        counters["skipped_out_of_scope"] += 1
        return

    if dry_run:
        # A look, not a load. The queue row is found by its source_ref; an apply decides by
        # intake_key, which also holds the content hash.
        try:
            queued = await _already_queued(pool, file_id)
        except Exception as exc:
            why = _failure_summary(exc)
            logger.error("drain_drive queue lookup failed for file_id=%s (%s)", file_id, why)
            counters["errors"] += 1
            failures.append((file_id, _is_permanent_file_error(exc)))
            return
        counters["already_present" if queued else "would_enqueue_new"] += 1
        return

    file_name = file_meta.get("name", file_id)
    source_path = f"{rel_dir}/{file_name}" if rel_dir else file_name
    dest = _local_blob_path(file_id, file_name)
    try:
        byte_size = await _download_file(drive_service, file_id, dest)
        res = await enqueue(
            pool,
            source=_SOURCE,
            source_ref=f"drive:{file_id}",
            blob_path=dest,
            mime_type=mime_type or None,
            byte_size=byte_size,
            source_path=source_path,
        )
    except Exception as exc:
        why = _failure_summary(exc)
        logger.error("drain_drive enqueue failed for file_id=%s (%s)", file_id, why)
        counters["errors"] += 1
        failures.append((file_id, _is_permanent_file_error(exc)))
        return
    if res.was_new:
        counters["enqueued_new"] += 1
    else:
        counters["already_present"] += 1


async def _drain_page(
    pool: asyncpg.Pool,
    drive_service,
    scope_id: str,
    page_token: str | None,
    held_ticks: int,
    limit: int,
) -> _Page:
    """ONE page of the changes feed: process EVERY change it returns, then move, hold or let go of
    the cursor. ``held_ticks`` counts the failing ticks already spent AT ``page_token``."""
    counters = _new_counters()
    try:
        if not page_token:
            page_token = await drive_service.get_start_page_token()
        # ONE bounded page: every change it returns is processed below, and its
        # continuation (not the end of the feed) becomes the cursor.
        result = await drive_service.list_changes_since(page_token, page_size=limit, max_pages=1)
    except Exception as exc:
        why = _failure_summary(exc)
        logger.warning("drain_drive: Drive enumeration failed, skipping (%s)", why)
        return _Page(counters, page_token, page_token, None, listed=False)

    changes = result.get("changes", [])
    counters["changes"] = len(changes)

    folder_cache: dict[str, dict[str, Any]] = {}
    failures: list[tuple[str, bool]] = []  # (file_id, permanent Drive error of that file?)

    for change in changes:
        await _ingest_change(
            pool, drive_service, scope_id, change, folder_cache, counters, failures
        )

    # Move our cursor to the feed's continuation, unless a change of this page failed: then
    # HOLD it (the page is re-listed next tick; enqueue is idempotent). The hold has no tick
    # limit while ANY failure could heal by waiting, so a system fault never costs a page.
    # Only when EVERY failure is a permanent Drive error of its own file, at least one other
    # change of the SAME tick was downloaded and queued (the downloads demonstrably work), and
    # _MAX_HELD_TICKS failing ticks have passed, are those files let go (ERROR with their ids)
    # and the page advances. A tick with no success holds however permanent its errors look:
    # a service account that lost its access must not cost a page every few ticks.
    next_token = result.get("new_page_token")
    gave_up = 0
    succeeded = counters["enqueued_new"] + counters["already_present"]
    if failures:
        held_ticks += 1
        if (
            succeeded
            and all(permanent for _, permanent in failures)
            and held_ticks >= _MAX_HELD_TICKS
        ):
            gave_up = len(failures)
            logger.error(
                "drain_drive: giving up on %d file(s) with a permanent Drive error after %d "
                "failing ticks at %s, file_ids=%s",
                gave_up,
                held_ticks,
                page_token,
                [file_id for file_id, _ in failures],
            )
        else:
            logger.warning(
                "drain_drive: %d change(s) failed (%d permanent), %d succeeded, cursor held at "
                "%s (failing tick %d)",
                len(failures),
                sum(permanent for _, permanent in failures),
                succeeded,
                page_token,
                held_ticks,
            )
            next_token = None
            await _store_cursor(pool, page_token, held=held_ticks)
    if next_token:
        await _store_cursor(pool, next_token)

    return _Page(
        counters,
        page_token,
        next_token or page_token,
        bool(result.get("more_pages")),
        gave_up,
        advanced=bool(next_token),
        held=bool(failures) and not gave_up,
    )


def _fold(total: dict[str, int], page: _Page) -> None:
    """Add the counters of a page to the tick's: the done line and the sum read the same dict."""
    for key, value in page.counters.items():
        total[key] += value


def _spent(started: float, budget: float) -> bool:
    return _clock() - started >= budget


def _log_done(page: _Page, page_no: int, cap: int, stop: str, disk_hold: int = 0) -> None:
    logger.info(
        "drain_drive done: %s page=%d/%d stop=%s disk_hold=%d from_token=%s next_token=%s "
        "more_pages=%s gave_up=%d",
        page.counters,
        page_no,
        cap,
        stop,
        disk_hold,
        page.from_token,
        page.cursor,
        page.more_pages,
        page.gave_up,
    )


async def drain_drive(
    pool: asyncpg.Pool,
    *,
    drive_service=None,
    limit: int = 500,
    budget_s: float | None = None,
    max_pages: int | None = None,
    min_free_gib: float | Decimal | None = None,
) -> dict[str, int]:
    """Enumerate Drive changes since the intake cursor, enqueue in-scope blobs.

    A tick walks the changes feed page by page (see the module docstring): up to ``limit``
    changes per page (the Drive service caps a page at 1000), all processed, the cursor stored
    right after each page. The walk stops when the feed ends, a page is held (never skipped),
    ``max_pages`` pages are done, ``budget_s`` seconds have passed since the call began (checked
    between pages and again right before each page after the first; the first page always runs)
    or the blob volume has less than ``min_free_gib`` GiB free (the guard cannot be turned off:
    anything that is not a positive number is the default floor, see ``_floor_bytes``). An argument
    beats ``INTAKE_DRIVE_DRAIN_MAX_PAGES`` / ``..._BUDGET_S`` / ``INTAKE_DRIVE_MIN_FREE_GIB``.

    `drive_service` is a ServiceAccountDriveService instance; if None, this
    adapter attempts to build one and degrades gracefully (WARN + return zeros)
    if Drive is not configured/reachable (Law 4). ``INTAKE_DRIVE_SCOPE_FOLDER_ID``
    must point at the watched root folder, else the drain refuses to run.

    Returns counters summed over the pages of the tick: {changes, enqueued_new, already_present,
    skipped_native, skipped_removed, skipped_out_of_scope, errors}.
    """
    started = _clock()  # the budget counts from here: the service and the cursor read spend it
    counters = _new_counters()

    scope_id = os.environ.get(_SCOPE_ENV, "").strip()
    if not scope_id:
        logger.warning(
            "drain_drive: %s not set — refusing to ingest the whole Drive "
            "(fail-safe), skipping", _SCOPE_ENV,
        )
        return counters

    if drive_service is None:
        try:
            from backend.services.integrations.service_account_drive_service import (
                ServiceAccountDriveService,
            )

            drive_service = ServiceAccountDriveService()
        except Exception as exc:
            why = _failure_summary(exc)
            logger.warning("drain_drive: Drive service unavailable, skipping (%s)", why)
            return counters

    # Read / seed our own intake cursor (separate from the prod poller token).
    async with pool.acquire() as conn:
        token_row = await conn.fetchval(
            "SELECT value FROM system_settings WHERE key = $1", _CURSOR_KEY
        )
    page_token, held_ticks = _parse_cursor(token_row)

    budget, cap, min_free = _drain_settings(budget_s, max_pages, min_free_gib)
    page_no = 0
    while True:
        page_no += 1
        reason = _disk_floor_breach(min_free)
        if reason is not None:
            logger.error("drain_drive: %s, cursor held without downloading", reason)
            hold = _Page({**_new_counters(), "errors": 1}, page_token, page_token, None)
            _fold(counters, hold)
            _log_done(hold, page_no, cap, "disk", 1)
            break
        if page_no > 1 and _spent(started, budget):
            # The probe above and the done line of the page before ran after the last check and
            # may have used up what was left: no page is started on a spent budget.
            _log_done(_Page(_new_counters(), page_token, page_token, None), page_no, cap, "budget")
            break
        page = await _drain_page(pool, drive_service, scope_id, page_token, held_ticks, limit)
        if not page.listed:
            break
        _fold(counters, page)
        if page.held:
            stop = "held"
        elif not page.advanced:
            stop = "no-token"
        elif not page.more_pages:
            stop = "end"
        elif page_no >= cap:
            stop = "cap"
        elif _spent(started, budget):
            stop = "budget"
        else:
            stop = "continue"
        _log_done(page, page_no, cap, stop)
        if stop != "continue":
            break
        page_token, held_ticks = page.cursor, 0
    return counters


async def _pause(seconds: float) -> None:
    """The short wait between two ids of a re-enqueue (a function of its own so a test never sleeps)."""
    import asyncio

    await asyncio.sleep(seconds)


async def reenqueue_drive_files(
    pool: asyncpg.Pool,
    drive_service,
    scope_id: str,
    file_ids: Sequence[str],
    *,
    apply: bool,
    min_free_gib: float | Decimal | None = None,
    pause_s: float | None = None,
) -> dict[str, int]:
    """Read again, by Drive file id, files the changes feed never delivered; enqueue them on ``apply``.

    The cursor discipline (module docstring) keeps a tick from losing a change, but a change dropped
    before it landed is never emitted again. For each id, one at a time: files.get, then the drain's own
    skips (trashed, Google-native, out of scope) and scope walk (``_ingest_change``). ``apply=False`` only
    looks: no download, no enqueue, and the file counts as ``would_enqueue_new`` or, when the queue
    already has a row for its ``source_ref``, ``already_present``. ``apply=True`` downloads and enqueues
    exactly as the drain does (``enqueue`` is idempotent on ``intake_key``: a second apply adds nothing).
    The cursor is never touched.

    Before every id of an apply the disk floor is checked (``_floor_bytes``, ``min_free_gib`` beats the
    environment): below it the run stops, loudly, with ``disk_hold=1`` and the ids left in
    ``not_attempted``. A file Drive no longer serves (a permanent error of files.get) is ``missing``; any
    other fault of an id (Drive, disk, DB, an ancestor lookup) is ``errors``, with no retry. Logs carry
    ids and counts only, never a name or a path.

    Raises ValueError for an empty scope or an entry that is not a Drive file id, and TypeError when
    ``apply`` is not a bool, all before any Drive call. Returns the counters of ``_new_reenqueue_counters``.
    """
    if not isinstance(apply, bool):
        raise TypeError("apply must be True or False")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError("a scope folder id is required")
    ids = list(file_ids)
    for position, file_id in enumerate(ids):
        if not isinstance(file_id, str) or _FILE_ID_RE.fullmatch(file_id) is None:
            raise ValueError(f"entry {position} is not a Drive file id")

    counters = _new_reenqueue_counters(len(ids))
    min_free = _floor_bytes(min_free_gib)
    pause = _REENQUEUE_PAUSE_S if pause_s is None else pause_s
    folder_cache: dict[str, dict[str, Any]] = {}
    for position, file_id in enumerate(ids):
        if apply:
            reason = _disk_floor_breach(min_free)
            if reason is not None:
                logger.error("reenqueue_drive: %s, stopping without downloading", reason)
                counters["disk_hold"] = 1
                counters["not_attempted"] = len(ids) - position
                break
        if position:
            await _pause(pause)
        try:
            file_meta = await _file_meta(drive_service, file_id)
        except Exception as exc:
            why = _failure_summary(exc)
            if _is_permanent_file_error(exc):
                logger.warning(
                    "reenqueue_drive: file_id=%s is gone or unreadable (%s)", file_id, why
                )
                counters["missing"] += 1
            else:
                logger.warning(
                    "reenqueue_drive: files.get failed for file_id=%s (%s)", file_id, why
                )
                counters["errors"] += 1
            continue
        change = {"fileId": file_id, "removed": False, "file": file_meta}
        await _ingest_change(
            pool, drive_service, scope_id, change, folder_cache, counters, [], dry_run=not apply
        )
    logger.info("reenqueue_drive done: apply=%s %s", apply, counters)
    return counters
