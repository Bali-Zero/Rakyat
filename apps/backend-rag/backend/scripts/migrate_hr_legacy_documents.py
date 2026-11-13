"""R5: the legacy HR papers into DOKUMEN KARYAWAN (SPEC v2.1 R5, STAFF_CARD.md C13).

    python -I backend/scripts/migrate_hr_legacy_documents.py            # dry-run (the default)
    python -I backend/scripts/migrate_hr_legacy_documents.py --apply

The C10 import wrote slot values from Drive listings; none is verified, so no card links them. For
each one listed in `hr_legacy_documents_manifest.json` (frozen read-only before this job:
`[hr_employees.id, slot, sha256(value)]`, `docs.contract` folded into `contract`) the job copies the
file into the person's folder and writes the COPY's id with its proof, as the door writes an upload.
Per slot, holding the door's member advisory lock for the whole operation:

1. re-read the slot: still present-but-unverified, its sha256 the manifest's, the value id-shaped
   (`^[A-Za-z0-9_-]{25,100}$` with a letter). Otherwise nothing is sent anywhere;
2. reuse an earlier copy (appProperties `nz_hr_member`, `nz_hr_slot` and `nz_hr_src` =
   sha256(value)[:16], inside the person folder), else `files.copy` the source there with the
   door's title and appProperties plus `nz_hr_src`;
3. compare-and-set: the door's own UPDATE (slot + `doc_proof.<slot>`; the contract slot also drops
   `docs.contract`), only where the slot still holds the manifest-hashed unverified value. No row:
   `conflict`, and the copy is trashed by its own Drive id;
4. a failure at or after COMMIT is re-read under the door's locks before anything is decided.

A source file is only read and copied: never updated, moved, trashed or re-shared. Every source's
modifiedTime, parents and trashed flag are read before the first copy and again after the last.

Output: ONE JSON line of counts, nothing else. Outcomes per manifest slot: done, reused, verified
(already verified: the `changed` case of a rerun), changed, conflict, uncertain, outside_manifest,
source_folder, drive_error:<status>, folder:<code>, lock_timeout, db_error; plus source:<class>,
eligible (dry-run), source_unchanged / source_changed / source_unreadable (apply) and log_records.
No value, name, file name or Drive id reaches stdout, stderr, a log or Sentry: Sentry must not be
bound, every log record is only counted, and the loggers that print identifiers sit at WARNING.
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


_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

if __name__ == "__main__":
    if sys.flags.isolated != 1:
        os.write(2, b"migrate_hr_legacy_documents: run with python -I\n")
        os._exit(2)
    if sys.argv[1:] not in ([], ["--apply"]):
        os.write(2, b"usage: migrate_hr_legacy_documents.py [--apply]\n")
        os._exit(2)
    contain_logging()
    sys.path.insert(0, _ROOT)
    for _dir in [
        *os.environ.get("PYTHONPATH", "").split(os.pathsep),
        os.path.join(_ROOT, "backend"),
    ]:
        if _dir and os.path.isabs(_dir) and os.path.isdir(_dir) and _dir not in sys.path:
            sys.path.append(_dir)

try:  # a failed import (a settings error quotes its input) is a fixed code, never a traceback
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
    os.write(1, b'{"failed": "import_failed"}\n')
    os._exit(1)

MANIFEST = Path(__file__).resolve().with_name("hr_legacy_documents_manifest.json")
ACTOR = "r5:migrate_hr_legacy_documents"  # `doc_proof.<slot>.by` of every slot this job writes
SRC_PROP = "nz_hr_src"
_ID_SHAPE = re.compile(r"[A-Za-z0-9_-]{25,100}")
_LETTER = re.compile(r"[A-Za-z]")
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


def _settled(profile: Any, slot: str) -> str:
    return "verified" if project_documents(profile)[slot]["file_id"] is not None else "changed"


@dataclass(frozen=True)
class Item:
    emp_id: int
    member_id: str
    slot: str
    digest: str


def classify(
    rows: list[Any], manifest: dict[tuple[int, str], str], counts: Counter
) -> list[tuple[Item, str]]:
    """(item, value) for every manifest slot that may be migrated now; every other present-but-
    unverified slot and every other manifest slot is only counted."""
    out: list[tuple[Item, str]] = []
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
            value = eligible(profile, slot, manifest[key])
            if value is not None:
                out.append(
                    (Item(row["id"], str(row["team_member_id"]), slot, manifest[key]), value)
                )
            elif (
                legacy
                and not id_shaped(v := _slot_value(profile, slot))
                and sha256_hex(v) == manifest[key]
            ):
                counts["outside_manifest"] += 1  # the hash matches, the second fence does not
            else:
                counts[_settled(profile, slot)] += 1
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


async def migrate_slot(
    svc: hd.HRDocumentService, item: Item, folder_id: str, ctx: Any, mime: Any
) -> str:
    drive = svc.drive
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
                profile = _profile(row["p"]) if row is not None else None
                value = None
                if row is not None and str(row["team_member_id"]) == item.member_id:
                    value = eligible(profile, item.slot, item.digest)
                if value is None:
                    await tx.rollback()
                    return _settled(profile, item.slot)
                step = "drive"
                props = {
                    hd.MEMBER_PROP: item.member_id,
                    hd.SLOT_PROP: item.slot,
                    SRC_PROP: item.digest[:16],
                }
                earlier = await drive.list_files_by_app_properties(props, folder_id, None)
                if earlier:
                    copy_id, reused = min(str(f["id"]) for f in earlier), True
                else:
                    ext = _EXT.get(mime)
                    title = hd.document_title(
                        item.slot, ctx.full_name, business_today(), ext or "_"
                    )
                    title = title if ext else title.removesuffix("._")
                    copy_id = str((await drive.copy_file(value, folder_id, title, props))["id"])
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
                    await tx.rollback()
                    step = "conflict"
                    await drive.trash_file(copy_id)
                    return "conflict"
            except BaseException:
                if step != "conflict":
                    try:
                        await tx.rollback()
                    except Exception:
                        pass
                raise
            step = "commit"
            await tx.commit()
            step = "done"
    except Exception as exc:
        if step == "conflict":
            return "conflict"  # the copy could not be trashed: it stays, never the source
        if step == "commit":
            holds, _ = await svc._reconcile(item.member_id, item.slot, copy_id or "")
            if not holds:
                return "uncertain"
        elif step != "done":
            if step == "drive":
                return drive_code(exc)
            return "lock_timeout" if isinstance(exc, _LOCK_ERRORS) else "db_error"
    await svc._follow_unit_change(item.member_id, ctx.unit)
    return "reused" if reused else "done"


async def run(
    pool: asyncpg.Pool, drive: Any, manifest: dict[tuple[int, str], str], *, apply: bool
) -> Counter:
    counts: Counter = Counter()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SCAN_SQL)
    ready: list[tuple[Item, Any]] = []
    before: list[tuple[str, tuple[Any, ...]]] = []
    for item, value in classify(rows, manifest, counts):
        try:
            meta = await drive.get_file_metadata(value)
        except Exception as exc:
            counts[drive_code(exc)] += 1
            continue
        cls = source_class(meta.get("mimeType"))
        counts[f"source:{cls}"] += 1
        if cls == "folder":
            counts["source_folder"] += 1
            continue
        before.append((value, _fingerprint(meta)))
        ready.append((item, meta.get("mimeType")))
    if not apply:
        counts["eligible"] += len(ready)
        return counts
    svc = hd.HRDocumentService(pool, drive)
    folders: dict[str, tuple[str, Any] | str] = {}
    for item, mime in ready:
        if item.member_id not in folders:
            try:
                folder_id, ctx = await svc.ensure_folder(item.member_id)
                folders[item.member_id] = (str(folder_id), ctx)
            except hd.HRDocError as exc:
                folders[item.member_id] = f"folder:{exc.code.value}"
        found = folders[item.member_id]
        if isinstance(found, str):
            counts[found] += 1
            continue
        counts[await migrate_slot(svc, item, found[0], found[1], mime)] += 1
    for value, fingerprint in before:
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
    return {
        "mode": "apply" if apply else "dry-run",
        "manifest": len(manifest),
        "outcomes": dict(sorted(counts.items())),
    }


def main(argv: list[str]) -> int:
    try:
        out: dict[str, Any] = asyncio.run(_main(argv == ["--apply"]))
        code = 0
    except Fatal as exc:
        out, code = {"failed": exc.code}, 1
    except BaseException:
        out, code = {"failed": "internal_error"}, 1
    sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    os._exit(main(sys.argv[1:]))
