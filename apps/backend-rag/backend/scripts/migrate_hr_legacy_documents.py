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
   the door's title and appProperties plus `nz_hr_src`. Only a non-empty id string Drive returned
   is ever used, never one coerced. No id whose sha256 is ANY manifest digest is ever reused,
   trashed or used as the person folder (whose reconciliation may move it);
3. compare-and-set: the door's own UPDATE (slot + `doc_proof.<slot>`; the contract slot also drops
   `docs.contract`), only where the slot still holds the manifest-hashed unverified value. No row:
   `conflict`, and the copy is trashed by its own Drive id while the lock is still held. A database
   error before COMMIT (a certain rollback) trashes it too, under the lock, after a re-read;
4. a failure at or after COMMIT is re-read under the door's locks: landed is `done`, otherwise
   `uncertain` and the copy stays for the next run to reuse.

A source file is only read and copied: never updated, moved, trashed or re-shared. Its modifiedTime,
parents and trashed flag are read under the lock before the copy and again after the last slot.

Output: ONE JSON line, rebuilt from a closed grammar of codes and bounded integers. Per manifest
slot: done, reused, changed, conflict, uncertain, outside_manifest, source_folder, eligible
(dry-run), drive_error:<status>, folder:<code> (`folder:source_protected` included), lock_timeout,
db_error; besides: copy_left (a copy a failed trash left behind), source:<class>, source_unchanged /
source_changed / source_unreadable (the audit of every source the run read) and log_records. Containment: file
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
import socket
import sys
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
_OUTCOME = re.compile(
    r"done|reused|changed|conflict|uncertain|outside_manifest|source_folder|eligible"
    r"|copy_left|lock_timeout|db_error|source_unchanged|source_changed|source_unreadable"
    r"|log_records|source:(?:pdf|image|folder|google|other)|drive_error:(?:[1-5][0-9]{2}|other)"
    r"|folder:(?:not_found|hr_record_required|drive_unavailable|lock_timeout|db_error"
    r"|commit_unknown|source_protected)"
)
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


def render(result: dict[str, Any]) -> bytes:
    """The ONE line the job prints, rebuilt from validated parts: closed codes, bounded ints."""
    if set(result) == {"failed"} and result["failed"] in FAILED_CODES:
        return json.dumps({"failed": result["failed"]}).encode() + b"\n"
    outcomes = result.get("outcomes")
    ok = (
        set(result) == {"mode", "manifest", "outcomes"}
        and result["mode"] in ("apply", "dry-run")
        and type(result["manifest"]) is int
        and 0 <= result["manifest"] < _MAX_COUNT
        and isinstance(outcomes, dict)
        and all(
            isinstance(k, str)
            and _OUTCOME.fullmatch(k) is not None
            and type(v) is int
            and 0 <= v < _MAX_COUNT
            for k, v in outcomes.items()
        )
    )
    if not ok:
        return b'{"failed": "internal_error"}\n'
    line = {
        "manifest": result["manifest"],
        "mode": result["mode"],
        "outcomes": {k: outcomes[k] for k in sorted(outcomes)},
    }
    return json.dumps(line).encode() + b"\n"


def emit(fd: int, result: dict[str, Any]) -> None:
    data = render(result)
    while data:
        data = data[os.write(fd, data) :]


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
                counts["outside_manifest"] += int(legacy)
                continue
            seen.add(key)
            if eligible(profile, slot, manifest[key]) is not None:
                out.append(Item(row["id"], str(row["team_member_id"]), slot, manifest[key]))
            elif (
                legacy
                and not id_shaped(v := _slot_value(profile, slot))
                and sha256_hex(v) == manifest[key]
            ):
                counts["outside_manifest"] += 1  # the hash matches, the second fence does not
            else:
                counts["changed"] += 1
    counts["changed"] += len(set(manifest) - seen)
    return out


def drive_code(exc: BaseException) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return f"drive_error:{int(status)}" if str(status).isdigit() else "drive_error:other"


def source_class(mime: Any) -> str:
    if mime == hd.FOLDER_MIME:
        return "folder"
    if mime in _EXT:
        return "pdf" if mime == "application/pdf" else "image"
    if isinstance(mime, str) and mime.startswith("application/vnd.google-apps."):
        return "google"
    return "other"


def _fingerprint(meta: dict[str, Any]) -> tuple[Any, ...]:
    return (
        meta.get("modifiedTime"),
        tuple(sorted(meta.get("parents") or [])),
        bool(meta.get("trashed")),
    )


class _BadDriveId(Exception):
    pass


def is_source(file_id: Any, protected: frozenset[str]) -> bool:
    """True for an id whose sha256 is ANY manifest digest: a source, never a copy."""
    return not isinstance(file_id, str) or sha256_hex(file_id) in protected


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


async def process_slot(
    svc: hd.HRDocumentService,
    item: Item,
    folder: tuple[str | None, Any],
    apply: bool,
    protected: frozenset[str],
    counts: Counter,
    sources: list[tuple[str, tuple[Any, ...]]],
) -> None:
    """One manifest slot, under the member advisory lock from the re-read to the commit. `folder`
    is (person folder id or None, card context). Without `apply` (the dry-run) it stops before
    the first write: lock, re-read, reuse lookup, the source's metadata, rollback."""
    drive = svc.drive
    folder_id, ctx = folder
    copy_id: str | None = None
    reused = False
    step = "lock"
    try:
        async with svc.db_pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
                await conn.execute(_LOCK_SQL, item.member_id)
                step = "read"
                row = await conn.fetchrow(_ROW_SQL, item.emp_id)
                value = None
                if row is not None and str(row["team_member_id"]) == item.member_id:
                    value = eligible(_profile(row["p"]), item.slot, item.digest)
                if value is None:
                    await tx.rollback()
                    counts["changed"] += 1
                    return
                step = "drive"
                props = {
                    hd.MEMBER_PROP: item.member_id,
                    hd.SLOT_PROP: item.slot,
                    SRC_PROP: item.digest[:16],
                }
                earlier = []
                if folder_id is not None:
                    earlier = [
                        f["id"]
                        for f in await drive.list_files_by_app_properties(props, folder_id, None)
                        if drive_id(f.get("id"))
                        and not is_source(f["id"], protected)
                        and folder_id in (f.get("parents") or [])
                    ]
                if earlier:  # a resume: the copy exists, the source need not be readable
                    copy_id, reused = min(earlier), True
                    if not apply:
                        await tx.rollback()
                        counts["eligible"] += 1
                        return
                    try:  # the source audit only (Acceptance 5); never a reason to stop
                        sources.append((value, _fingerprint(await drive.get_file_metadata(value))))
                    except Exception:
                        counts["source_unreadable"] += 1
                else:
                    meta = await drive.get_file_metadata(value)
                    kind = source_class(meta.get("mimeType"))
                    counts[f"source:{kind}"] += 1
                    if kind == "folder" or not apply or folder_id is None:
                        await tx.rollback()
                        counts["source_folder" if kind == "folder" else "eligible"] += 1
                        return
                    sources.append((value, _fingerprint(meta)))
                    ext = _EXT.get(meta.get("mimeType"))
                    title = hd.document_title(
                        item.slot, ctx.full_name, business_today(), ext or "_"
                    )
                    title = title if ext else title.removesuffix("._")
                    made = (await drive.copy_file(value, folder_id, title, props)).get("id")
                    if not drive_id(made) or is_source(made, protected):
                        raise _BadDriveId
                    copy_id = made
                step = "cas"
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
            step = "commit"
            await tx.commit()
            step = "done"
    except Exception as exc:
        if step == "commit":
            holds, _ = await svc._reconcile(item.member_id, item.slot, copy_id or "")
            if not holds:
                counts["uncertain"] += 1  # the copy stays: the next run reuses it
                return
        elif step != "done":
            if step == "drive":
                counts[drive_code(exc)] += 1
                return
            counts["lock_timeout" if isinstance(exc, _LOCK_ERRORS) else "db_error"] += 1
            if step == "cas" and copy_id is not None:
                counts["copy_left"] += int(not await _compensate(svc, item, copy_id, protected))
            return
    await svc._follow_unit_change(item.member_id, ctx.unit)
    counts["reused" if reused else "done"] += 1


async def member_folder(
    svc: hd.HRDocumentService, member_id: str, apply: bool, protected: frozenset[str]
) -> tuple[str | None, Any] | str:
    """(person folder id, card context), or a `folder:<code>`. No folder the job would create,
    use or MOVE (the door's reconciliation moves a person folder whose unit changed) may be a
    manifest source. The dry-run only looks: (the one existing folder or None, None)."""
    try:
        found = await svc.drive.find_folders_by_app_property(hd.MEMBER_PROP, member_id)
    except Exception:
        return "folder:drive_unavailable"
    if any(is_source(f.get("id"), protected) for f in found):
        return "folder:source_protected"
    if not apply:
        return (found[0]["id"] if len(found) == 1 else None), None
    try:
        folder_id, ctx = await svc.ensure_folder(member_id)
    except hd.HRDocError as exc:
        return f"folder:{exc.code.value}"
    if not drive_id(folder_id) or is_source(folder_id, protected):
        return "folder:source_protected"
    return folder_id, ctx


async def run(
    pool: asyncpg.Pool, drive: Any, manifest: dict[tuple[int, str], str], *, apply: bool
) -> Counter:
    counts: Counter = Counter()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SCAN_SQL)
    svc = hd.HRDocumentService(pool, drive)
    protected = frozenset(manifest.values())
    sources: list[tuple[str, tuple[Any, ...]]] = []
    folders: dict[str, tuple[str | None, Any] | str] = {}
    for item in classify(rows, manifest, counts):
        if item.member_id not in folders:
            folders[item.member_id] = await member_folder(svc, item.member_id, apply, protected)
        found = folders[item.member_id]
        if isinstance(found, str):
            counts[found] += 1
            continue
        await process_slot(svc, item, found, apply, protected, counts, sources)
    for value, fingerprint in sources:  # Acceptance 5: every source this run read, re-read
        try:
            meta = await drive.get_file_metadata(value)
        except Exception:
            counts["source_unreadable"] += 1
            continue
        counts["source_unchanged" if _fingerprint(meta) == fingerprint else "source_changed"] += 1
    return counts


async def _main(apply: bool) -> dict[str, Any]:
    counted = contain_logging()
    assert_no_sentry()
    manifest = load_manifest()
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
        counts = await run(pool, drive, manifest, apply=apply)
    finally:
        await pool.close()
    counts["log_records"] = counted.records
    return {"mode": "apply" if apply else "dry-run", "manifest": len(manifest), "outcomes": counts}


def main(argv: list[str], out: int) -> int:
    try:
        result: dict[str, Any] = asyncio.run(_main(argv == ["--apply"]))
        result["outcomes"] = dict(result["outcomes"])
        code = 0
    except Fatal as exc:
        result, code = {"failed": exc.code}, 1
    except BaseException:
        result, code = {"failed": "internal_error"}, 1
    emit(out, result)
    return code


if __name__ == "__main__":
    os._exit(main(sys.argv[1:], _OUT))
