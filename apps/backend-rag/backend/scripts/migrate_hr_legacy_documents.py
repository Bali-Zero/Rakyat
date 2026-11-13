"""R5: the legacy HR papers into DOKUMEN KARYAWAN (SPEC v2.1 R5, STAFF_CARD.md C13).

    python -I backend/scripts/migrate_hr_legacy_documents.py            # dry-run (the default)
    python -I backend/scripts/migrate_hr_legacy_documents.py --apply

The C10 import wrote slot values from Drive listings; none is verified, so no card links them. For
each one listed in `hr_legacy_documents_manifest.json` (frozen read-only before this job:
`[hr_employees.id, slot, sha256(value)]`, `docs.contract` folded into `contract`) the job copies the
file into the person's folder and writes the COPY's id with its proof, as the door writes an upload.
Per slot, holding the door's member advisory lock for the whole operation:

1. re-read the slot: still present-but-unverified, its sha256 the manifest's, the value id-shaped
   (`^[A-Za-z0-9_-]{25,100}$` with a letter). Otherwise `changed`, and nothing is sent anywhere;
2. reuse an earlier copy (appProperties `nz_hr_member`, `nz_hr_slot` and `nz_hr_src` =
   sha256(value)[:16], inside the person folder): a resume needs no readable source. Otherwise
   read the source's metadata (the dry-run stops before any write) and `files.copy` it there with
   the door's title and appProperties plus `nz_hr_src`. Only a usable id string Drive returned is
   ever used, never one coerced. No id whose sha256 is ANY manifest digest is ever reused or
   written (SPEC v2.2 R5.2), and every Drive mutation of the run - the job's and the door
   helpers' - crosses ONE `ProtectedDrive`, which refuses a manifest source in any id position
   before the call (R5.1);
3. compare-and-set: the door's own UPDATE (slot + `doc_proof.<slot>`; the contract slot also drops
   `docs.contract`), only where the slot still holds the manifest-hashed unverified value. No row:
   `conflict`, and the copy is trashed by its own Drive id while the lock is still held. A database
   error before COMMIT (a certain rollback) trashes it too, under the lock, after a re-read;
4. a failure at or after COMMIT is re-read under the door's locks: landed is `done`, otherwise
   `uncertain` and the copy stays for the next run to reuse.

A source file is only read and copied: never updated, moved, trashed or re-shared. Its modifiedTime,
parents and trashed flag are read once per distinct source before any Drive mutation of the run
and again after the last slot (R5.4); a read-only census then counts every tagged copy of every
manifest entry. SIGINT, SIGTERM and SIGHUP only set a stop flag (R5.3, a drain): the slot in
flight ends by its normal path, no further slot starts, audit and census still run. A watchdog
thread bounds each unit of work; past its deadline the counts are written and the process exits.

Output (R5.5): ONE JSON line, rebuilt from a closed grammar of codes and bounded integers:
`{"acceptance5", "manifest", "mode", "outcomes", "run": completed|interrupted|deadline}` or
`{"failed": <code>[, "outcomes"]}`; exit 0, 130, 124 or 1, decided from the rendered line. Exactly
one per manifest entry, summing to it: done, reused, changed, conflict, uncertain,
copy_unidentified, not_started, deadline, fence_failed, source_folder, eligible (dry-run),
drive_error:<status>, folder:<code> (`folder:source_protected` included), lock_timeout, db_error;
besides: unlisted, copy_left, protected_refusal, returned_unusable, source:<class>,
source_baselined, source_unreadable, audit_*, copy_held / copy_reusable / copy_residual,
census_skipped and log_records. Containment: file
descriptors 1 and 2 point at /dev/null for the whole run (a print, a traceback, a library or a C
extension writing there reaches nothing) and the line goes out on a private duplicate of stdout;
every log record is only counted; the identifier loggers sit at WARNING; Sentry must be unbound.
"""

import warnings

if __name__ == "__main__":
    warnings.simplefilter("ignore")

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

IDENTIFIER_LOGGERS = (
    "backend.services.integrations.service_account_drive_service",
    "backend.app.services.hr.hr_service",
    "backend.app.services.hr.hr_documents",
    "googleapiclient.discovery",
    "googleapiclient.discovery_cache",
    "googleapiclient.http",
)
FAILED_CODES = frozenset(
    {
        "import_failed",
        "manifest_invalid",
        "sentry_bound",
        "connect_failed",
        "drive_client_failed",
        "internal_error",
    }
)
_PER_ENTRY = re.compile(  # exactly one per manifest entry (R5.5)
    r"done|reused|changed|conflict|uncertain|copy_unidentified|not_started|deadline|fence_failed"
    r"|source_folder|eligible|lock_timeout|db_error|drive_error:(?:[1-5][0-9]{2}|other)"
    r"|folder:(?:not_found|hr_record_required|drive_unavailable|lock_timeout|db_error"
    r"|commit_unknown|source_protected)"
)
_BESIDES = re.compile(  # never part of the per-entry sum
    r"unlisted|copy_left|protected_refusal|returned_unusable|log_records"
    r"|source:(?:pdf|image|folder|google|other)|source_baselined|source_unreadable"
    r"|audit_unchanged|audit_changed|audit_unreadable"
    r"|copy_held|copy_reusable|copy_residual|census_skipped"
)
_RUNS = {"completed": 0, "interrupted": 130, "deadline": 124}
_A5 = ("observed", "nothing_migrated", "not_demonstrated")
_MAX_COUNT = 1_000_000


class _Counted(logging.Handler):
    """The only log handler of the run: it counts records and never formats one."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records = 0

    def emit(self, record: logging.LogRecord) -> None:  # noqa: ARG002
        self.records += 1


def contain_logging() -> _Counted:
    for name in IDENTIFIER_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for lg in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(lg, logging.Logger):
            lg.handlers.clear()
            lg.propagate = True
    counted = _Counted()
    logging.root.handlers[:] = [counted]
    logging.root.setLevel(logging.WARNING)
    return counted


def seal_streams() -> int:
    """Point fd 1 and fd 2 at /dev/null for the rest of the process; return a private,
    non-inheritable duplicate of the original stdout, the only way out (`emit`)."""
    sys.stdout.flush()
    sys.stderr.flush()
    out = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    return out


def per_entry(outcomes: dict[str, Any]) -> int:
    return sum(v for k, v in outcomes.items() if _PER_ENTRY.fullmatch(k))


def _counts_ok(outcomes: Any) -> bool:
    return isinstance(outcomes, dict) and all(
        isinstance(k, str)
        and (_PER_ENTRY.fullmatch(k) is not None or _BESIDES.fullmatch(k) is not None)
        and type(v) is int
        and 0 <= v < _MAX_COUNT
        for k, v in outcomes.items()
    )


def render(result: dict[str, Any], manifest: int | None = None) -> bytes:
    """The ONE line the job prints (SPEC v2.2 R5.5), rebuilt from validated parts: closed codes,
    bounded ints, the per-entry partition summing to the manifest. Anything else is
    `{"failed": "internal_error"}`. `manifest` bounds the counts a failure line carries."""
    fallback = b'{"failed": "internal_error"}\n'
    keys, outcomes = set(result), result.get("outcomes")
    if "failed" in result:
        if result["failed"] not in FAILED_CODES:
            return fallback
        if keys == {"failed"}:
            return json.dumps({"failed": result["failed"]}).encode() + b"\n"
        if keys != {"failed", "outcomes"} or not _counts_ok(outcomes) or manifest is None:
            return fallback
        if per_entry(outcomes) > manifest:
            return fallback
        line: dict[str, Any] = {"failed": result["failed"]}
    else:
        n = result.get("manifest")
        ok = (
            keys == {"acceptance5", "manifest", "mode", "outcomes", "run"}
            and result["acceptance5"] in _A5
            and result["mode"] in ("apply", "dry-run")
            and result["run"] in _RUNS
            and type(n) is int
            and 0 <= n < _MAX_COUNT
            and _counts_ok(outcomes)
            and per_entry(outcomes) == n
        )
        if not ok:
            return fallback
        line = {k: result[k] for k in ("acceptance5", "manifest", "mode")}
    line["outcomes"] = {k: outcomes[k] for k in sorted(outcomes)}
    if "run" in result:
        line["run"] = result["run"]
    return json.dumps(line).encode() + b"\n"


def exit_code(line: bytes) -> int:
    """R5.5: decided FROM the rendered line. 1 a failure (the renderer's fallback included); 0, 130
    or 124 a completed, interrupted or deadline-ended run."""
    doc = json.loads(line)
    return 1 if "failed" in doc else _RUNS[doc["run"]]


def _write(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


def emit(fd: int, result: dict[str, Any]) -> None:
    _write(fd, render(result))


_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_OUT = 1

if __name__ == "__main__":
    if sys.flags.isolated != 1:
        os.write(2, b"migrate_hr_legacy_documents: run with python -I\n")
        os._exit(2)
    if sys.argv[1:] not in ([], ["--apply"]):
        os.write(2, b"usage: migrate_hr_legacy_documents.py [--apply]\n")
        os._exit(2)
    _OUT = seal_streams()
    sys.excepthook = lambda *_: emit(_OUT, {"failed": "internal_error"})
    contain_logging()
    sys.path.insert(0, _ROOT)
    for _dir in [
        *os.environ.get("PYTHONPATH", "").split(os.pathsep),
        os.path.join(_ROOT, "backend"),
    ]:
        if _dir and os.path.isabs(_dir) and os.path.isdir(_dir) and _dir not in sys.path:
            sys.path.append(_dir)

try:
    import asyncpg

    from backend.app.core.config import settings
    from backend.app.core.database import DatabasePoolPolicy, create_database_pool
    from backend.app.services.hr import hr_documents as hd
    from backend.app.services.hr.hr_card_models import _slot_value, project_documents
    from backend.app.services.hr.hr_service import (
        _LOCK_ERRORS,
        CARD_EDIT_LOCK_TIMEOUT_MS,
        business_today,
    )
except BaseException:
    if __name__ != "__main__":
        raise
    emit(_OUT, {"failed": "import_failed"})
    os._exit(1)

MANIFEST = Path(__file__).resolve().with_name("hr_legacy_documents_manifest.json")
ACTOR = "r5:migrate_hr_legacy_documents"  # `doc_proof.<slot>.by` of every slot this job writes
SRC_PROP = "nz_hr_src"
_ID_SHAPE = re.compile(r"[A-Za-z0-9_-]{25,100}")
_LETTER = re.compile(r"[A-Za-z]")
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_EXT = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}
_SCAN_SQL = "SELECT id, team_member_id, profile::text AS p FROM hr_employees ORDER BY id"
_ROW_SQL = "SELECT team_member_id, profile::text AS p FROM hr_employees WHERE id = $1"
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))"


def _cas_sql(slot: str) -> str:
    """The door's write for `slot`, narrowed to the row whose slot still holds a JSON string with
    sha256 = $6, unverified (`doc_proof.<slot>.id` differs from it) and, for the contract, whose
    `docs.contract` is absent or equal to it. $1-$5 are the door's own parameters."""
    base = hd._write_sql(slot, remove=False)
    tail = " WHERE team_member_id = $1 AND jsonb_typeof(profile) = 'object' RETURNING profile"
    if not base.endswith(tail):
        raise RuntimeError("the door's write statement changed shape")
    v = (
        "(profile #> '{contract,file_id}')"
        if slot == hd.CONTRACT_SLOT
        else "(profile #> ARRAY['docs', $2::text])"
    )
    cas = (
        f" AND jsonb_typeof({v}) = 'string'"
        f" AND encode(sha256(convert_to({v} #>> '{{}}', 'UTF8')), 'hex') = $6::text"
        f" AND (profile #> ARRAY['doc_proof', $2::text, 'id']) IS DISTINCT FROM {v}"
    )
    if slot == hd.CONTRACT_SLOT:
        cas += f" AND ((profile #> '{{docs,contract}}') IS NULL OR (profile #> '{{docs,contract}}') = {v})"
    return base.removesuffix(" RETURNING profile") + cas + " RETURNING profile"


CAS_SQL = {slot: _cas_sql(slot) for slot in hd.ALL_SLOTS}


class Fatal(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_manifest(path: Path = MANIFEST) -> dict[tuple[int, str], str]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
        out: dict[tuple[int, str], str] = {}
        for emp_id, slot, digest in entries:
            ok = type(emp_id) is int and slot in hd.ALL_SLOTS and isinstance(digest, str)
            if not ok or not _DIGEST.fullmatch(digest) or (emp_id, slot) in out:
                raise ValueError
            out[(emp_id, slot)] = digest
    except Exception:
        raise Fatal("manifest_invalid") from None
    return out


def assert_no_sentry() -> None:
    try:
        import sentry_sdk
    except ImportError:
        return
    if sentry_sdk.get_client().is_active():
        raise Fatal("sentry_bound")


def id_shaped(value: str) -> bool:
    return _ID_SHAPE.fullmatch(value) is not None and _LETTER.search(value) is not None


def drive_id(value: Any) -> bool:
    """A file id as Drive returns one: a non-empty string of its alphabet, never coerced."""
    return isinstance(value, str) and _DRIVE_ID.fullmatch(value) is not None


def _profile(raw: Any) -> Any:
    return json.loads(raw) if raw is not None else None


def eligible(profile: Any, slot: str, digest: str) -> str | None:
    """The slot's value iff it may be sent to Drive: present-but-unverified by the shared
    projection, sha256 = the manifest's, id-shaped, and (contract) `docs.contract` absent or equal."""
    state = project_documents(profile)[slot]
    if not state["present"] or state["file_id"] is not None:
        return None
    value = _slot_value(profile, slot)
    if sha256_hex(value) != digest or not id_shaped(value):
        return None
    docs = profile.get("docs")
    if slot == hd.CONTRACT_SLOT and isinstance(docs, dict) and docs.get("contract", value) != value:
        return None
    return value


@dataclass(frozen=True)
class Item:
    emp_id: int
    member_id: str
    slot: str
    digest: str


def classify(rows: list[Any], manifest: dict[tuple[int, str], str], counts: Counter) -> list[Item]:
    """Every manifest slot that may be migrated now (its value is re-read under the lock, never
    carried from here); every other present-but-unverified slot and manifest slot is counted."""
    out: list[Item] = []
    seen: set[tuple[int, str]] = set()
    for row in rows:
        profile = _profile(row["p"])
        for slot, state in project_documents(profile).items():
            key = (row["id"], slot)
            legacy = state["present"] and state["file_id"] is None
            if key not in manifest:
                counts["unlisted"] += int(legacy)  # never sent anywhere
                continue
            seen.add(key)
            if eligible(profile, slot, manifest[key]) is not None:
                out.append(Item(row["id"], str(row["team_member_id"]), slot, manifest[key]))
            elif (
                legacy
                and not id_shaped(v := _slot_value(profile, slot))
                and sha256_hex(v) == manifest[key]
            ):
                counts["fence_failed"] += 1  # the hash matches, the second fence does not
            else:
                counts["changed"] += 1
    counts["changed"] += len(set(manifest) - seen)
    return out


def drive_code(exc: BaseException) -> str:
    status = str(getattr(getattr(exc, "resp", None), "status", None))
    ok = status.isdigit() and 100 <= int(status) <= 599  # the closed grammar's range, no other
    return f"drive_error:{int(status)}" if ok else "drive_error:other"


def source_class(mime: Any) -> str:
    if mime == hd.FOLDER_MIME:
        return "folder"
    if mime in _EXT:
        return "pdf" if mime == "application/pdf" else "image"
    if isinstance(mime, str) and mime.startswith("application/vnd.google-apps."):
        return "google"
    return "other"


def _fingerprint(meta: Any) -> tuple[Any, ...] | None:
    """(modifiedTime, sorted parents, trashed), or None when the metadata is not valid: no
    modifiedTime string, no parents list of strings, or a trashed flag that is not a bool. An
    invalid answer is UNREADABLE, never equal to another invalid one (owner ruling, rev 4)."""
    if not isinstance(meta, dict):
        return None
    modified, parents, trashed = meta.get("modifiedTime"), meta.get("parents"), meta.get("trashed")
    if not isinstance(modified, str) or not modified or not isinstance(parents, list):
        return None
    if not all(isinstance(x, str) for x in parents) or not isinstance(trashed, bool | None):
        return None
    return (modified, tuple(sorted(parents)), bool(trashed))


class UnusableReturnedId(Exception):
    """Drive answered a create or a copy without a usable, unprotected id (SPEC v2.2 R5.1): the
    object may exist; its id is never coerced, used or written."""


class SourceProtected(Exception):
    """A Drive mutation refused BEFORE the call: the inner client received nothing (R5.1)."""


def is_source(file_id: Any, protected: frozenset[str]) -> bool:
    """True for an id whose sha256 is ANY manifest digest: a source, never a copy."""
    return not isinstance(file_id, str) or sha256_hex(file_id) in protected


def protected_id(value: Any, protected: frozenset[str]) -> bool:
    """R5.1: not an id of Drive's alphabet, or an id whose sha256 is a manifest digest."""
    return not drive_id(value) or sha256_hex(value) in protected


class ProtectedDrive:
    """The ONLY Drive the job and the door's helpers it calls ever see (SPEC v2.2 R5.1). Reads pass
    through. A mutation with a protected id in ANY checked position (target, each parent removed,
    parent added, parent of a created or copied file) is refused before the call and counted in
    `refusals`; whichever lookup produced the id does not matter. A create or copy whose answer
    carries no usable, unprotected id raises after the call and is counted in `unusable`. Nothing
    else is exposed."""

    __slots__ = ("_inner", "_protected", "_refusals", "_unusable")

    def __init__(self, inner: Any, protected: frozenset[str]) -> None:
        self._inner = inner
        self._protected = protected
        self._refusals = 0
        self._unusable = 0

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    @property
    def refusals(self) -> int:
        return self._refusals

    @property
    def unusable(self) -> int:
        return self._unusable

    def _refuse_if(self, *ids: Any) -> None:
        if any(protected_id(i, self._protected) for i in ids):
            self._refusals += 1
            raise SourceProtected

    def _returned(self, result: Any) -> Any:
        if not isinstance(result, dict) or protected_id(result.get("id"), self._protected):
            self._unusable += 1
            raise UnusableReturnedId
        return result

    async def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        return await self._inner.get_file_metadata(file_id)

    async def list_files_by_app_properties(self, props: Any, parent_id: str, mimes: Any) -> Any:
        return await self._inner.list_files_by_app_properties(props, parent_id, mimes)

    async def find_folder(self, name: str, parent_id: str) -> Any:
        return await self._inner.find_folder(name, parent_id)

    async def find_folders_by_app_property(self, key: str, value: str) -> Any:
        return await self._inner.find_folders_by_app_property(key, value)

    async def create_folder(
        self,
        name: str,
        parent_id: str | None = None,
        user_id: str | None = None,
        app_properties: dict[str, str] | None = None,
    ) -> Any:
        self._refuse_if(parent_id)
        return self._returned(
            await self._inner.create_folder(
                name, parent_id=parent_id, user_id=user_id, app_properties=app_properties
            )
        )

    async def move_file(self, file_id: str, from_parent_id: str, to_parent_id: str) -> Any:
        froms = from_parent_id.split(",") if isinstance(from_parent_id, str) else [None]
        self._refuse_if(file_id, to_parent_id, *froms)
        return await self._inner.move_file(file_id, from_parent_id, to_parent_id)

    async def copy_file(
        self, file_id: str, parent_id: str, name: str, app_properties: dict[str, str]
    ) -> Any:
        if not drive_id(file_id):  # the source is only READ by a copy: shape-checked, not vetoed
            self._refusals += 1
            raise SourceProtected
        self._refuse_if(parent_id)
        return self._returned(await self._inner.copy_file(file_id, parent_id, name, app_properties))

    async def trash_file(self, file_id: str) -> None:
        self._refuse_if(file_id)
        await self._inner.trash_file(file_id)


class Stop:
    """The drain flag (SPEC v2.2 R5.3): a signal only sets it; nothing is ever cancelled."""

    __slots__ = ("is_set",)

    def __init__(self) -> None:
        self.is_set = False

    def set(self) -> None:
        self.is_set = True


def install_drain(loop: asyncio.AbstractEventLoop, stop: Stop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)


# R5.3 deadlines, seconds, one armed at a time, each the wall-clock of one unit of work
SETUP_S, BASELINE_S, FOLDER_S, SLOT_S, AUDIT_S, CENSUS_S, SHUTDOWN_S = (
    120,
    120,
    300,
    600,
    120,
    120,
    60,
)
_LIST_CAP = 2000  # `list_files_by_app_properties` reads at most 20 pages of 100
_MAY_HAVE_COPIED = frozenset({"copy", "cas", "commit"})
_NOTHING_SENT = frozenset({"lock", "read", "drive"})


class _NoWatch:
    def arm(self, seconds: float) -> None:
        pass

    def disarm(self) -> None:
        pass


class Live:
    """What a deadline needs to account the run it ends: written by the event loop, read by the
    watchdog thread (attribute reads and a plain dict copy of the counts)."""

    def __init__(self) -> None:
        self.watch: _NoWatch = _NoWatch()
        self.reset(None, "dry-run")

    def reset(self, manifest: int | None, mode: str) -> None:
        self.manifest = manifest
        self.mode = mode
        self.counts: Counter = Counter()
        self.step: str | None = None  # the last recorded step of the slot in flight
        self.scanned = False
        self.migrated: set[str] = set()  # sources of slots that ended done/reused (memory only)
        self.acceptance5 = "not_demonstrated"

    def deadline_result(self) -> dict[str, Any]:
        counts = dict(self.counts)
        if self.step in _MAY_HAVE_COPIED:
            counts["uncertain"] = counts.get("uncertain", 0) + 1
        elif self.step in _NOTHING_SENT:
            counts["deadline"] = counts.get("deadline", 0) + 1
        rest = (self.manifest or 0) - per_entry(counts)
        if rest > 0:
            counts["not_started"] = counts.get("not_started", 0) + rest
        return {
            "acceptance5": "not_demonstrated",
            "manifest": self.manifest or 0,
            "mode": self.mode,
            "outcomes": counts,
            "run": "deadline",
        }


LIVE = Live()
_EMIT = threading.Lock()
_EMITTED: list[bool] = []


def _claim_emit() -> bool:
    """The line is written once: by the main thread at the end, or by the watchdog at a deadline."""
    with _EMIT:
        if _EMITTED:
            return False
        _EMITTED.append(True)
        return True


class Watch(_NoWatch):
    """R5.3: the deadline lives OUTSIDE the event loop. A daemon thread ends the process when the
    armed deadline passes, whatever the loop or a worker thread is blocked in: snapshot, one line,
    `os._exit`. Nothing destructive happens there; Postgres rolls back what the process held."""

    def __init__(self, out: int) -> None:
        self._out = out
        self._at: float | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="r5-deadline", daemon=True).start()

    def arm(self, seconds: float) -> None:
        with self._lock:
            self._at = time.monotonic() + seconds

    def disarm(self) -> None:
        with self._lock:
            self._at = None

    def _loop(self) -> None:
        while True:
            time.sleep(0.1)
            with self._lock:
                at = self._at
            if at is not None and time.monotonic() > at:
                self._expire()

    def _expire(self) -> None:
        """Termination never waits on publication (owner ruling, rev 4): one non-blocking write
        attempt, then `os._exit` whatever happened. A line the channel did not take is lost: a hard
        stop without guaranteed accounting. If the main thread holds the line (its write is what
        blocked past the shutdown deadline), the process ends with 1."""
        code = 1
        try:
            line = render(LIVE.deadline_result())
            if _claim_emit():
                code = exit_code(line)
                os.set_blocking(self._out, False)
                os.write(self._out, line)
        except BaseException:
            pass
        finally:
            os._exit(code)


async def _trash(drive: Any, copy_id: str, protected: frozenset[str]) -> bool:
    if is_source(copy_id, protected):
        return False
    try:
        await drive.trash_file(copy_id)
    except Exception:
        return False
    return True


async def _compensate(
    svc: hd.HRDocumentService, item: Item, copy_id: str, protected: frozenset[str]
) -> bool:
    """After a CERTAIN rollback: under the member lock, re-read the slot and trash the copy unless
    the slot holds it. True when nothing is left behind."""
    try:
        async with svc.db_pool.acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
            await conn.execute(_LOCK_SQL, item.member_id)
            row = await conn.fetchrow(_ROW_SQL, item.emp_id)
            if row is not None and _slot_value(_profile(row["p"]) or {}, item.slot) == copy_id:
                return True
            return await _trash(svc.drive, copy_id, protected)
    except Exception:
        return False


async def _eligible_under_lock(conn: Any, item: Item) -> str | None:
    await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
    await conn.execute(_LOCK_SQL, item.member_id)
    row = await conn.fetchrow(_ROW_SQL, item.emp_id)
    if row is None or str(row["team_member_id"]) != item.member_id:
        return None
    return eligible(_profile(row["p"]), item.slot, item.digest)


async def baseline(
    svc: hd.HRDocumentService, items: list[Item], counts: Counter, watch: _NoWatch
) -> dict[str, tuple[Any, ...]]:
    """R5.4, before ANY Drive mutation of the run: each eligible slot's source metadata, read under
    the member lock after the locked re-read, once per distinct source id. In memory only."""
    seen: dict[str, tuple[Any, ...]] = {}
    unreadable: set[str] = set()
    for item in items:
        watch.arm(BASELINE_S)
        try:
            async with svc.db_pool.acquire() as conn, conn.transaction():
                value = await _eligible_under_lock(conn, item)
                if value is None or value in seen or value in unreadable:
                    continue
                try:
                    fingerprint = _fingerprint(await svc.drive.get_file_metadata(value))
                except Exception:
                    fingerprint = None
                if fingerprint is None:
                    unreadable.add(value)
                else:
                    seen[value] = fingerprint
        except Exception:
            continue  # the slot's own locked step decides it
    counts["source_baselined"] += len(seen)
    counts["source_unreadable"] += len(unreadable)
    return seen


async def audit(
    drive: ProtectedDrive, base: dict[str, tuple[Any, ...]], counts: Counter, watch: _NoWatch
) -> set[str]:
    """R5.4, after the last slot (a drained run included): every baselined source, re-read once.
    Returns the sources observed unchanged."""
    unchanged: set[str] = set()
    for value, fingerprint in base.items():
        watch.arm(AUDIT_S)
        try:
            meta = await drive.get_file_metadata(value)
        except Exception:
            counts["audit_unreadable"] += 1
            continue
        now = _fingerprint(meta)
        if now is None:
            counts["audit_unreadable"] += 1
        elif now == fingerprint:
            unchanged.add(value)
            counts["audit_unchanged"] += 1
        else:
            counts["audit_changed"] += 1
    return unchanged


async def census(
    drive: ProtectedDrive,
    pool: Any,
    manifest: dict[tuple[int, str], str],
    protected: frozenset[str],
    counts: Counter,
    watch: _NoWatch,
) -> None:
    """R5.4, both modes, read-only: every copy Drive holds for EVERY manifest entry, eligible or
    not (a transport retry may have made more than one). Each tagged non-source file in the person
    folder is `copy_held` (the slot holds it), `copy_reusable` (the slot is still eligible and it
    is the lowest id, the one the next run reuses) or `copy_residual`. Never trashes anything."""
    watch.arm(CENSUS_S)
    async with pool.acquire() as conn:
        rows = {r["id"]: r for r in await conn.fetch(_SCAN_SQL)}
    folders: dict[str, str | None] = {}
    for (emp_id, slot), digest in sorted(manifest.items()):
        watch.arm(CENSUS_S)
        row = rows.get(emp_id)
        if row is None:
            counts["census_skipped"] += 1
            continue
        member, profile = str(row["team_member_id"]), _profile(row["p"])
        if member not in folders:
            try:
                found = await drive.find_folders_by_app_property(hd.MEMBER_PROP, member)
            except Exception:
                found = []
            one = len(found) == 1 and not protected_id(found[0].get("id"), protected)
            folders[member] = found[0]["id"] if one else None
        folder_id = folders[member]
        if folder_id is None:
            counts["census_skipped"] += 1
            continue
        props = {hd.MEMBER_PROP: member, hd.SLOT_PROP: slot, SRC_PROP: digest[:16]}
        try:
            listed = await drive.list_files_by_app_properties(props, folder_id, None)
        except Exception:
            counts["census_skipped"] += 1
            continue
        if len(listed) >= _LIST_CAP:  # the client stops paging there: coverage not complete
            counts["census_skipped"] += 1
            continue
        copies = sorted(  # the reuse predicate (R5.2), ids de-duplicated
            {
                f["id"]
                for f in listed
                if not protected_id(f.get("id"), protected)
                and folder_id in (f.get("parents") or [])
            }
        )
        ok = isinstance(profile, dict)
        held = project_documents(profile)[slot]["file_id"] if ok else None
        still = ok and eligible(profile, slot, digest) is not None
        for cid in copies:
            if cid == held:
                counts["copy_held"] += 1
            elif still and cid == copies[0]:
                counts["copy_reusable"] += 1
            else:
                counts["copy_residual"] += 1


async def process_slot(
    svc: hd.HRDocumentService,
    item: Item,
    folder: tuple[str | None, Any],
    apply: bool,
    protected: frozenset[str],
    counts: Counter,
) -> None:
    """One manifest slot, under the member advisory lock from the re-read to the commit (SPEC v2.1
    R5; the state table of v2.2 R5.3). `folder` is (person folder id or None, card context).
    Without `apply` (the dry-run) it stops before the first write: lock, re-read, reuse lookup,
    the source's metadata, rollback. A success is counted BEFORE the unit follow-up."""
    drive = svc.drive
    folder_id, ctx = folder
    copy_id: str | None = None
    value: str | None = None
    reused = False
    step = LIVE.step = "lock"
    try:
        async with svc.db_pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                value = await _eligible_under_lock(conn, item)
                if value is None:
                    await tx.rollback()
                    counts["changed"] += 1
                    return
                step = LIVE.step = "drive"
                props = {
                    hd.MEMBER_PROP: item.member_id,
                    hd.SLOT_PROP: item.slot,
                    SRC_PROP: item.digest[:16],
                }
                earlier = []
                if folder_id is not None:  # R5.2: no id that is a manifest source, ever
                    earlier = [
                        f["id"]
                        for f in await drive.list_files_by_app_properties(props, folder_id, None)
                        if not protected_id(f.get("id"), protected)
                        and folder_id in (f.get("parents") or [])
                    ]
                if earlier:  # a resume: the copy exists, the source need not be readable
                    copy_id, reused = min(earlier), True
                    if not apply:
                        await tx.rollback()
                        counts["eligible"] += 1
                        return
                else:
                    meta = await drive.get_file_metadata(value)
                    kind = source_class(meta.get("mimeType"))
                    counts[f"source:{kind}"] += 1
                    if kind == "folder" or not apply or folder_id is None:
                        await tx.rollback()
                        counts["source_folder" if kind == "folder" else "eligible"] += 1
                        return
                    ext = _EXT.get(meta.get("mimeType"))
                    title = hd.document_title(
                        item.slot, ctx.full_name, business_today(), ext or "_"
                    )
                    title = title if ext else title.removesuffix("._")
                    step = LIVE.step = "copy"
                    made = (await drive.copy_file(value, folder_id, title, props)).get("id")
                    if protected_id(made, protected):  # R5.2: the job's own check, not only Drive's
                        raise UnusableReturnedId
                    copy_id = made
                step = LIVE.step = "cas"
                stamp = datetime.now(UTC).isoformat(timespec="seconds")
                written = await conn.fetchrow(
                    CAS_SQL[item.slot],
                    item.member_id,
                    item.slot,
                    copy_id,
                    ACTOR,
                    stamp,
                    item.digest,
                )
                if written is None:
                    left = not await _trash(drive, copy_id, protected)  # the lock is still held
                    try:
                        await tx.rollback()
                    except Exception:
                        pass
                    counts["conflict"] += 1
                    counts["copy_left"] += int(left)
                    return
            except BaseException:
                try:
                    await tx.rollback()
                except Exception:
                    pass
                raise
            step = LIVE.step = "commit"
            await tx.commit()
            step = LIVE.step = "done"
    except BaseException as exc:
        landed = step == "done"
        if step == "commit":
            holds, _ = await svc._reconcile(item.member_id, item.slot, copy_id or "")
            landed = bool(holds)
            if not landed:
                counts["uncertain"] += 1  # the copy stays: the next run reuses it
        elif step == "copy":
            counts[
                "copy_unidentified" if isinstance(exc, UnusableReturnedId) else drive_code(exc)
            ] += 1
        elif step == "drive":
            counts[drive_code(exc)] += 1
        elif step != "done":
            counts["lock_timeout" if isinstance(exc, _LOCK_ERRORS) else "db_error"] += 1
            if step == "cas" and copy_id is not None:
                counts["copy_left"] += int(not await _compensate(svc, item, copy_id, protected))
        if landed:
            LIVE.step = "done"
            LIVE.migrated.add(value or "")
            counts["reused" if reused else "done"] += 1
        if not isinstance(exc, Exception):
            raise
        if not landed:
            return
    else:
        LIVE.migrated.add(value or "")
        counts["reused" if reused else "done"] += 1
    await svc._follow_unit_change(item.member_id, ctx.unit)


async def member_folder(
    svc: hd.HRDocumentService, member_id: str, apply: bool, protected: frozenset[str]
) -> tuple[str | None, Any] | str:
    """(person folder id, card context), or a `folder:<code>`. The dry-run only looks: (the one
    existing folder or None, None). A refusal of the boundary inside `ensure_folder` (which reports
    every Drive error as drive_unavailable) is named `folder:source_protected`."""
    try:
        found = await svc.drive.find_folders_by_app_property(hd.MEMBER_PROP, member_id)
    except Exception:
        return "folder:drive_unavailable"
    if any(protected_id(f.get("id"), protected) for f in found):
        return "folder:source_protected"
    if not apply:
        return (found[0]["id"] if len(found) == 1 else None), None
    refused = svc.drive.refusals
    code: str | None = None
    folder_id: Any = None
    ctx: Any = None
    try:
        folder_id, ctx = await svc.ensure_folder(member_id)
    except hd.HRDocError as exc:
        code = f"folder:{exc.code.value}"
    finally:
        rose = svc.drive.refusals > refused
    if rose or (code is None and protected_id(folder_id, protected)):
        return "folder:source_protected"
    return code if code is not None else (folder_id, ctx)


def acceptance5(apply: bool, unchanged: set[str], counts: Counter) -> str:
    """R5.4: what this run may claim. `observed` only when every slot it migrated has its source
    baselined and audited unchanged, and no audited source changed or became unreadable."""
    if not LIVE.migrated:
        return "nothing_migrated"
    clean = counts["audit_changed"] == counts["audit_unreadable"] == 0
    return "observed" if apply and clean and LIVE.migrated <= unchanged else "not_demonstrated"


async def run(
    pool: asyncpg.Pool,
    drive: Any,
    manifest: dict[tuple[int, str], str],
    *,
    apply: bool,
    stop: Stop | None = None,
    watch: _NoWatch | None = None,
) -> Counter:
    stop = stop if stop is not None else Stop()
    watch = watch if watch is not None else _NoWatch()
    LIVE.reset(len(manifest), "apply" if apply else "dry-run")
    counts = LIVE.counts
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SCAN_SQL)
    protected = frozenset(manifest.values())
    drive = ProtectedDrive(drive, protected)  # R5.1: nothing below holds the raw client
    svc = hd.HRDocumentService(pool, drive)
    items = classify(rows, manifest, counts)
    LIVE.scanned = True
    base = await baseline(svc, items, counts, watch) if apply else {}
    folders: dict[str, tuple[str | None, Any] | str] = {}
    for item in items:
        found = folders.get(item.member_id)
        if found is None and not stop.is_set:
            watch.arm(FOLDER_S)
            found = folders[item.member_id] = await member_folder(
                svc, item.member_id, apply, protected
            )
        if isinstance(found, str):  # a folder step that ended keeps its code (R5.3 precedence)
            counts[found] += 1
            continue
        if stop.is_set or found is None:  # R5.3: the drain; nothing starts after a signal
            counts["not_started"] += 1
            continue
        watch.arm(SLOT_S)
        try:
            await process_slot(svc, item, found, apply, protected, counts)
        finally:
            LIVE.step = None
    unchanged = await audit(drive, base, counts, watch) if apply else set()
    await census(drive, pool, manifest, protected, counts, watch)
    if drive.refusals:
        counts["protected_refusal"] = drive.refusals
    if drive.unusable:
        counts["returned_unusable"] = drive.unusable
    LIVE.acceptance5 = acceptance5(apply, unchanged, counts)
    return counts


async def _main(apply: bool, out: int) -> dict[str, Any]:
    stop = Stop()
    install_drain(asyncio.get_running_loop(), stop)  # before the first database or Drive call
    counted = contain_logging()
    assert_no_sentry()
    manifest = load_manifest(MANIFEST)
    mode = "apply" if apply else "dry-run"
    LIVE.reset(len(manifest), mode)
    watch = LIVE.watch = Watch(out)
    watch.start()
    watch.arm(SETUP_S)
    socket.setdefaulttimeout(60)  # httplib2 takes the process default when it builds a transport
    try:
        pool = await create_database_pool(
            dsn=settings.database_url,
            policy=DatabasePoolPolicy(
                min_size=1, max_size=2, command_timeout=60, max_inactive_connection_lifetime=300
            ),
        )
    except Exception:
        raise Fatal("connect_failed") from None
    try:
        try:
            drive = await hd.get_hr_drive_service()
        except Exception:
            raise Fatal("drive_client_failed") from None
        counts = await run(pool, drive, manifest, apply=apply, stop=stop, watch=watch)
    finally:
        watch.arm(SHUTDOWN_S)
        await pool.close()
    watch.disarm()
    counts["log_records"] = counted.records
    return {
        "acceptance5": LIVE.acceptance5,
        "manifest": len(manifest),
        "mode": mode,
        "outcomes": counts,
        "run": "interrupted" if stop.is_set else "completed",
    }


def main(argv: list[str], out: int) -> int:
    LIVE.reset(None, "dry-run")
    LIVE.watch = _NoWatch()
    try:
        result: dict[str, Any] = asyncio.run(_main(argv == ["--apply"], out))
        result["outcomes"] = dict(result["outcomes"])
    except Fatal as exc:
        result = {"failed": exc.code}
    except BaseException:
        result = {"failed": "internal_error"}
    if "failed" in result and LIVE.scanned:  # R5.5: the counts so far ride on the failure line
        result["outcomes"] = dict(LIVE.counts)
    line = render(result, LIVE.manifest)
    if not _claim_emit():  # a deadline is ending the process right now
        while True:
            time.sleep(1)
    LIVE.watch.arm(SHUTDOWN_S)  # a write the reader never takes is a deadline too
    _write(out, line)
    LIVE.watch.disarm()
    return exit_code(line)


if __name__ == "__main__":
    os._exit(main(sys.argv[1:], _OUT))
