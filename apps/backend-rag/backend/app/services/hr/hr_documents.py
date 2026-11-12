"""The HR document door (STAFF_CARD.md C13): upload, replace and remove one employee's paper.

Files live in the owner's Drive tree (`settings.hr_documents_drive_root_id`), one folder per
person under a folder per UNIT. The tree is written as the tree's owner (domain-wide delegation,
`settings.hr_documents_drive_subject`), because the service account alone is only a reader there.

Nothing here puts a personal value (name, file title, file bytes, a stored slot value) in a log, an
error or a response: errors are `HRDocError` codes. Provenance lives in the card itself
(`profile.doc_proof.<slot> = {id, by, at}`, written in the same UPDATE as the slot): there is no
audit table in the door's transaction (production has none).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

import asyncpg

from backend.app.core.config import settings
from backend.app.services.hr.hr_card_models import project_documents
from backend.app.services.hr.hr_people_service import DOC_SLOTS
from backend.app.services.hr.hr_service import (
    _LOCK_ERRORS,
    CARD_EDIT_LOCK_TIMEOUT_MS,
    HRCardCode,
    HRCardError,
    HRService,
    business_today,
)

logger = logging.getLogger(__name__)

# googleapiclient logs every request URL (query included) at DEBUG/INFO. The door never puts a
# stored value in one, but a Drive URL is not a place for personal data either: belt and braces.
for _name in ("googleapiclient.discovery", "googleapiclient.http"):
    logging.getLogger(_name).setLevel(logging.WARNING)

CONTRACT_SLOT = "contract"
ALL_SLOTS: tuple[str, ...] = (*DOC_SLOTS, CONTRACT_SLOT)
SLOT_LABELS = {
    "ktp": "KTP",
    "kk": "KK",
    "npwp": "NPWP",
    "bpjs_kes": "BPJS Kesehatan",
    "bpjs_tk": "BPJS Ketenagakerjaan",
    CONTRACT_SLOT: "Kontrak",
}
MAX_BYTES = 4 * 1024 * 1024  # Vercel refuses request bodies above ~4.5 MB
FALLBACK_UNIT = "LAINNYA"
MEMBER_PROP = "nz_hr_member"
SLOT_PROP = "nz_hr_slot"
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIMES = frozenset({"application/pdf", "image/jpeg", "image/png"})  # what this door uploads


class HRDocCode(str, Enum):
    NOT_FOUND = "not_found"
    HR_RECORD_REQUIRED = "hr_record_required"
    DRIVE_UNAVAILABLE = "drive_unavailable"
    LOCK_TIMEOUT = "lock_timeout"
    DB_ERROR = "db_error"
    COMMIT_UNKNOWN = "commit_unknown"  # failed at or after COMMIT: the write may have landed


class HRDocError(Exception):
    """A document operation failed. A code only: Drive and driver messages can quote values."""

    def __init__(self, code: HRDocCode) -> None:
        super().__init__(code.value)
        self.code = code


# ─── pure helpers ───

_PNG = b"\x89PNG\r\n\x1a\n"


def detect_type(data: bytes) -> tuple[str, str] | None:
    """(mime, extension) from the MAGIC BYTES, never from a client MIME or filename."""
    if data.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(_PNG):
        return "image/png", "png"
    return None


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _clean(text: str) -> str:
    return _CONTROL.sub(" ", text).strip()


def unit_folder_name(unit: Any) -> str:
    """The unit folder: the card's `unit` stripped and upper-cased, LAINNYA when there is none."""
    name = _clean(unit).upper() if isinstance(unit, str) else ""
    return name or FALLBACK_UNIT


def document_title(slot: str, full_name: str, on: date, ext: str) -> str:
    return f"{SLOT_LABELS[slot]} - {_clean(full_name)} - {on.isoformat()}.{ext}"


@dataclass(frozen=True)
class _Context:
    full_name: str
    unit: str


_CONTEXT_SQL = """
    SELECT tm.full_name,
           e.id AS employee_id,
           CASE WHEN jsonb_typeof(e.profile) = 'object' THEN e.profile->>'unit' END AS unit
    FROM team_members tm
    LEFT JOIN hr_employees e ON e.team_member_id = tm.id
    WHERE tm.id = $1
"""

# FOR UPDATE: the previous state is read under the same row lock the new write takes. Both the
# slot value and the proof come out of ONE statement of ONE row, as jsonb (their types matter).
_PREVIOUS_SQL = """
    SELECT CASE WHEN jsonb_typeof(profile) <> 'object' THEN NULL
                WHEN $2::text = 'contract' THEN
                    CASE WHEN jsonb_typeof(profile->'contract') = 'object'
                         THEN profile->'contract'->'file_id' END
                ELSE CASE WHEN jsonb_typeof(profile->'docs') = 'object'
                          THEN profile->'docs'->$2::text END
           END AS v,
           CASE WHEN jsonb_typeof(profile) = 'object'
                     AND jsonb_typeof(profile->'doc_proof') = 'object'
                     AND jsonb_typeof(profile->'doc_proof'->$2::text) = 'object'
                THEN profile->'doc_proof'->$2::text->'id' END AS pid,
           jsonb_typeof(profile) = 'object' AS is_object
    FROM hr_employees WHERE team_member_id = $1 FOR UPDATE
"""

# The write (R2 + N1 + N4): the slot and `doc_proof.<slot>` in ONE UPDATE, per slot, through
# jsonb_set, so the other five proofs and every other profile key survive. A contract write or
# remove also deletes the legacy duplicate `docs.contract`. $1 member, $2 slot, $3 new id (NULL on
# remove), $4 actor email, $5 UTC timestamp. Only constants are formatted into it.
_DOCS_OBJ = (
    "COALESCE(CASE WHEN jsonb_typeof(profile->'docs') = 'object' THEN profile->'docs' END,"
    " '{}'::jsonb)"
)
_CONTRACT_OBJ = (
    "COALESCE(CASE WHEN jsonb_typeof(profile->'contract') = 'object' THEN profile->'contract' END,"
    " '{}'::jsonb)"
)
_PROOF_OBJ = (
    "COALESCE(CASE WHEN jsonb_typeof(profile->'doc_proof') = 'object' THEN profile->'doc_proof' END,"
    " '{}'::jsonb)"
)
_STAMP = "jsonb_build_object('id', $3::text, 'by', $4::text, 'at', $5::text)"
_DROP_LEGACY_CONTRACT_DUP = (
    "CASE WHEN jsonb_typeof(profile->'docs') = 'object'"
    " THEN jsonb_set({inner}, '{{docs}}', (profile->'docs') - 'contract') ELSE {inner} END"
)


def _write_sql(slot: str, remove: bool) -> str:
    if slot == CONTRACT_SLOT:
        if remove:
            c = (
                "CASE WHEN jsonb_typeof(profile->'contract') = 'object' THEN"
                " jsonb_set(profile, '{contract}', (profile->'contract') - 'file_id')"
                " ELSE profile END"
            )
        else:
            c = (
                f"jsonb_set(profile, '{{contract}}', {_CONTRACT_OBJ}"
                " || jsonb_build_object('file_id', $3::text))"
            )
        inner = _DROP_LEGACY_CONTRACT_DUP.format(inner=c)
    elif remove:
        inner = f"jsonb_set(profile, '{{docs}}', {_DOCS_OBJ} - $2::text)"
    else:
        inner = (
            f"jsonb_set(profile, '{{docs}}', {_DOCS_OBJ} || jsonb_build_object($2::text, $3::text))"
        )
    proofed = (
        f"jsonb_set({inner}, '{{doc_proof}}',"
        f" {_PROOF_OBJ} || jsonb_build_object($2::text, {_STAMP}))"
    )
    return (
        f"UPDATE hr_employees SET profile = {proofed}, updated_at = NOW()"
        " WHERE team_member_id = $1 AND jsonb_typeof(profile) = 'object' RETURNING profile"
    )


_WRITE_SQL = {
    (slot, remove): _write_sql(slot, remove) for slot in ALL_SLOTS for remove in (False, True)
}


def _verified_previous(row: Any) -> str | None:
    """The previous slot value iff it is VERIFIED: a JSON string equal to `doc_proof.<slot>.id`
    (a JSON string). Plain equality, nothing hashed, nothing looked up. Anything else is None."""
    value, proof = row["v"], row["pid"]
    if isinstance(value, str) and value.strip() and isinstance(proof, str) and proof == value:
        return value
    return None


class HRDocumentService:
    """`drive` is a `ServiceAccountDriveService` (or a test double with the same methods)."""

    def __init__(self, db_pool: asyncpg.Pool, drive: Any) -> None:
        self.db_pool = db_pool
        self.drive = drive

    # ── folders ──

    async def _find_or_create_unit(self, name: str) -> str:
        root = settings.hr_documents_drive_root_id
        found = await self.drive.find_folder(name, root)
        if found is not None:
            return str(found["id"])
        created = await self.drive.create_folder(name, parent_id=root)
        return str(created["id"])

    async def _ensure_person_folder(self, member_id: str, full_name: str, unit: str) -> str:
        unit_id = await self._find_or_create_unit(unit_folder_name(unit))
        existing = await self.drive.find_folders_by_app_property(MEMBER_PROP, member_id)
        if len(existing) > 1:
            raise RuntimeError("ambiguous person folder")
        if not existing:
            created = await self.drive.create_folder(
                _clean(full_name) or member_id,
                parent_id=unit_id,
                app_properties={MEMBER_PROP: member_id},
            )
            return str(created["id"])
        folder = existing[0]
        parents = [str(p) for p in folder.get("parents") or []]
        if unit_id not in parents:
            await self.drive.move_file(str(folder["id"]), ",".join(parents), unit_id)
        return str(folder["id"])

    @staticmethod
    def _context_from(row: Any) -> _Context:
        if row is None:
            raise HRDocError(HRDocCode.NOT_FOUND)
        if row["employee_id"] is None:
            raise HRDocError(HRDocCode.HR_RECORD_REQUIRED)
        return _Context(full_name=str(row["full_name"] or ""), unit=str(row["unit"] or ""))

    async def _read_context(self, member_id: str) -> _Context:
        """Unlocked read, for the remove pre-check only (404 / hr_record_required early)."""
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(_CONTEXT_SQL, member_id)
        except Exception:
            raise HRDocError(HRDocCode.DB_ERROR) from None
        return self._context_from(row)

    async def ensure_folder(
        self, member_id: str, *, skip_if_unit: str | None = None
    ) -> tuple[str | None, _Context]:
        """Find or create the person's folder (moving it when the unit changed).

        Lock order, the same everywhere: the MEMBER advisory lock first, then the context (unit,
        name, visibility, record) is read inside that same transaction, then the UNIT lock. An
        upload that resumes after a unit change therefore reads the new unit."""
        try:
            async with self.db_pool.acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", member_id
                )
                ctx = self._context_from(await conn.fetchrow(_CONTEXT_SQL, member_id))
                if skip_if_unit is not None and unit_folder_name(ctx.unit) == unit_folder_name(
                    skip_if_unit
                ):
                    return None, ctx  # nothing moved since the caller's folder step
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('hr_docs_unit:'||$1))",
                    unit_folder_name(ctx.unit),
                )
                folder_id = await self._ensure_person_folder(member_id, ctx.full_name, ctx.unit)
        except HRDocError:
            raise
        except _LOCK_ERRORS:
            raise HRDocError(HRDocCode.LOCK_TIMEOUT) from None
        except asyncpg.PostgresError:
            raise HRDocError(HRDocCode.DB_ERROR) from None
        except Exception as exc:  # Drive: the class only, never its message
            logger.error("hr documents drive folder failed (%s)", type(exc).__name__)
            raise HRDocError(HRDocCode.DRIVE_UNAVAILABLE) from None
        return folder_id, ctx

    # ── DB step (one transaction: lock, previous, write, audit) ──

    async def _db_step(
        self,
        *,
        actor_email: str,
        member_id: str,
        email: str,
        slot: str,
        new_id: str | None,
        state: dict[str, Any] | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """One transaction touching ONLY `hr_employees` (and the row lock on `team_members`): member
        lock, visibility lock, read the previous state, ONE UPDATE of the slot and its proof.

        Returns (verified previous value or None, the six-key documents map after the write). The
        previous value is returned ONLY when it was verified (R2/R6); an unverified one never
        leaves this function. Raises HRDocError. An error BEFORE the commit is a certain rollback;
        one at or after COMMIT (or a connection lost then) is COMMIT_UNKNOWN: the caller must
        reconcile, not assume. `state["verified"]` is set as soon as it is read."""
        service = HRService(self.db_pool)
        phase = "before"  # before -> committing -> done
        result: tuple[str | None, dict[str, Any]] | None = None
        try:
            async with self.db_pool.acquire() as conn:
                tx = conn.transaction()
                await tx.start()
                try:
                    await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", member_id
                    )
                    if not await service.lock_visible_person(conn, member_id, email):
                        raise HRDocError(HRDocCode.NOT_FOUND)
                    prev_row = await conn.fetchrow(_PREVIOUS_SQL, member_id, slot)
                    if prev_row is None:
                        raise HRDocError(HRDocCode.HR_RECORD_REQUIRED)
                    if not prev_row["is_object"]:
                        raise HRDocError(HRDocCode.DB_ERROR)
                    verified = _verified_previous(prev_row)
                    if state is not None:
                        state["verified"] = verified
                    written = await conn.fetchrow(
                        _WRITE_SQL[(slot, new_id is None)],
                        member_id,
                        slot,
                        new_id,
                        actor_email,
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    )
                    if written is None:
                        raise HRDocError(HRDocCode.NOT_FOUND)
                    documents = project_documents(written["profile"])
                except BaseException:
                    try:
                        await tx.rollback()
                    except Exception:  # the connection may be gone: the server rolls back
                        pass
                    raise
                phase = "committing"
                await tx.commit()
                phase = "done"
                result = (verified, documents)
            return result
        except HRDocError:
            raise
        except Exception as exc:
            if phase == "done" and result is not None:  # committed; only the release failed
                return result
            if phase == "committing":
                logger.error("hr documents commit outcome unknown (%s)", type(exc).__name__)
                raise HRDocError(HRDocCode.COMMIT_UNKNOWN) from None
            if isinstance(exc, HRCardError):
                if exc.code is HRCardCode.LOCK_TIMEOUT:
                    raise HRDocError(HRDocCode.LOCK_TIMEOUT) from None
                if exc.code is HRCardCode.HIRE_DATE_REQUIRED:
                    raise HRDocError(HRDocCode.HR_RECORD_REQUIRED) from None
            elif isinstance(exc, _LOCK_ERRORS):
                raise HRDocError(HRDocCode.LOCK_TIMEOUT) from None
            raise HRDocError(HRDocCode.DB_ERROR) from None

    async def _slot_holds(self, member_id: str, slot: str, file_id: str) -> bool | None:
        """Does the slot hold `file_id` WITH a matching proof? None when it cannot be told.

        Takes the SAME locks as the DB step, in a transaction, in the same order (member advisory
        lock, then the hr_employees row lock): the read waits for the original COMMIT, if it is
        still in flight, to finish and therefore sees its outcome."""
        try:
            async with self.db_pool.acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", member_id
                )
                row = await conn.fetchrow(_PREVIOUS_SQL, member_id, slot)  # FOR UPDATE
        except Exception:
            return None
        return bool(row is not None and row["v"] == file_id and row["pid"] == file_id)

    async def _documents(self, member_id: str) -> dict[str, Any] | None:
        """The documents map from a fresh read, or None if it cannot be read (best effort)."""
        try:
            async with self.db_pool.acquire() as conn:
                profile = await conn.fetchval(
                    "SELECT profile FROM hr_employees WHERE team_member_id = $1", member_id
                )
        except Exception:
            return None
        return project_documents(profile)

    async def _our_folder(self, member_id: str) -> str | None:
        """The member's person folder id, or None when there is not exactly one."""
        try:
            found = await self.drive.find_folders_by_app_property(MEMBER_PROP, member_id)
        except Exception as exc:
            logger.warning("hr documents folder lookup failed (%s)", type(exc).__name__)
            return None
        return str(found[0]["id"]) if len(found) == 1 else None

    async def _trash_if_ours(
        self,
        member_id: str,
        slot: str,
        verified_previous: str | None,
        new_id: str | None,
        folder_id: str | None,
    ) -> None:
        """After the commit: trash the replaced file ONLY if the previous value was VERIFIED (R2:
        it equals `doc_proof.<slot>.id`) and Drive's own listing of this door's files for this
        member and slot (appProperties, inside the person folder, not trashed, pdf/jpeg/png)
        returns an entry whose id equals it, compared locally. The LISTING entry's id is what is
        trashed, never the stored value. An unverified value is never sent anywhere: no Drive
        call is made at all. Any error leaves the file in place; nothing stored is logged."""
        if not verified_previous or verified_previous == new_id or not folder_id:
            return
        try:
            ours = await self.drive.list_files_by_app_properties(
                {MEMBER_PROP: member_id, SLOT_PROP: slot}, folder_id, sorted(DOC_MIMES)
            )
            entry = next(
                (
                    f
                    for f in ours
                    if f.get("id") == verified_previous
                    and f.get("mimeType") in DOC_MIMES
                    and folder_id in (f.get("parents") or [folder_id])
                ),
                None,
            )
            if entry is not None:
                await self.drive.trash_file(str(entry["id"]))  # the listing's id
        except Exception as exc:
            logger.warning("hr documents previous file left in place (%s)", type(exc).__name__)

    async def _follow_unit_change(self, member_id: str, used_unit: str) -> None:
        """After the commit: if the card's unit changed since the folder step (a C11 PATCH in
        between), run the folder step again; it moves the person folder, and the files with it.
        The re-read and the move happen inside ensure_folder, i.e. while holding the member
        advisory lock, so a PATCH cannot change the unit between the two."""
        try:
            await self.ensure_folder(member_id, skip_if_unit=used_unit)
        except Exception as exc:  # the file is saved; the next upload or move corrects the folder
            logger.warning("hr documents unit follow-up failed (%s)", type(exc).__name__)

    # ── public flows ──

    async def upload(
        self,
        *,
        actor_email: str,
        member_id: str,
        email: str,
        slot: str,
        data: bytes,
        mime: str,
        ext: str,
        today: date | None = None,
    ) -> dict[str, Any]:
        folder_id, ctx = await self.ensure_folder(member_id)
        title = document_title(slot, ctx.full_name, today or business_today(), ext)
        try:
            uploaded = await self.drive.upload_file_to_folder(
                folder_id,
                data,
                title,
                mime,
                app_properties={MEMBER_PROP: member_id, SLOT_PROP: slot},
            )
            new_id = str(uploaded["id"])
        except Exception as exc:
            logger.error("hr documents drive upload failed (%s)", type(exc).__name__)
            raise HRDocError(HRDocCode.DRIVE_UNAVAILABLE) from None
        state: dict[str, Any] = {}
        documents: dict[str, Any] | None
        try:
            verified, documents = await self._db_step(
                actor_email=actor_email,
                member_id=member_id,
                email=email,
                slot=slot,
                new_id=new_id,
                state=state,
            )
        except HRDocError as exc:
            if exc.code is HRDocCode.COMMIT_UNKNOWN:
                landed = await self._slot_holds(member_id, slot, new_id)
                if landed:  # it did commit: carry on as a success
                    await self._trash_if_ours(
                        member_id, slot, state.get("verified"), new_id, folder_id
                    )
                    await self._follow_unit_change(member_id, ctx.unit)
                    return {"slot": slot, "documents": await self._documents(member_id)}
                if landed is None:  # cannot tell: an orphan file is safer than a dangling id
                    logger.error("hr documents upload left in place, outcome unverifiable")
                    raise
            try:  # certain rollback (or proven not landed): do not leave the upload orphaned
                await self.drive.trash_file(new_id)
            except Exception as terr:
                logger.error("hr documents compensation trash failed (%s)", type(terr).__name__)
            raise
        await self._trash_if_ours(member_id, slot, verified, new_id, folder_id)
        await self._follow_unit_change(member_id, ctx.unit)
        return {"slot": slot, "documents": documents}

    async def remove(
        self, *, actor_email: str, member_id: str, email: str, slot: str
    ) -> dict[str, Any]:
        await self._read_context(member_id)  # 404 / hr_record_required before the write
        try:
            verified, documents = await self._db_step(
                actor_email=actor_email, member_id=member_id, email=email, slot=slot, new_id=None
            )
        except HRDocError as exc:
            if exc.code is HRDocCode.COMMIT_UNKNOWN:  # nothing was uploaded; keep the old file
                raise HRDocError(HRDocCode.DB_ERROR) from None
            raise
        if verified:  # an unverified value clears the slot and trashes nothing, calls nothing
            await self._trash_if_ours(
                member_id, slot, verified, None, await self._our_folder(member_id)
            )
        return {"slot": slot, "documents": documents}


async def get_hr_drive_service() -> Any:
    """A NEW delegated Drive client per call (route dependency): googleapiclient's service object
    and its httplib2 transport are not thread-safe and requests reach them through
    `asyncio.to_thread`, so one is never shared across requests. The constructor does blocking
    work (credential parsing), hence the thread; `build()` reads the bundled discovery document."""
    import asyncio

    from backend.services.integrations.service_account_drive_service import (
        ServiceAccountDriveService,
    )

    return await asyncio.to_thread(
        ServiceAccountDriveService,
        root_folder_id=settings.hr_documents_drive_root_id,
        delegated_user=settings.hr_documents_drive_subject,
        validate_folders=False,
    )
