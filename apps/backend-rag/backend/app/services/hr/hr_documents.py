"""The HR document door (STAFF_CARD.md C12): upload, replace and remove one employee's paper.

Files live in the owner's Drive tree (`settings.hr_documents_drive_root_id`), one folder per
person under a folder per UNIT. The tree is written as the tree's owner (domain-wide delegation,
`settings.hr_documents_drive_subject`), because the service account alone is only a reader there.

Nothing here puts a personal value (name, file title, file bytes) in a log, an error or an audit
row: errors are `HRDocError` codes, the audit row carries slot + file ids only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

import asyncpg

from backend.app.core.config import settings
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
AUDIT_UPLOAD = "hr_document_upload"
AUDIT_REMOVE = "hr_document_remove"


class HRDocCode(str, Enum):
    NOT_FOUND = "not_found"
    HR_RECORD_REQUIRED = "hr_record_required"
    DRIVE_UNAVAILABLE = "drive_unavailable"
    LOCK_TIMEOUT = "lock_timeout"
    DB_ERROR = "db_error"


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

# FOR UPDATE: the previous id is read under the same row lock the new write takes.
_PREVIOUS_SQL = """
    SELECT CASE WHEN jsonb_typeof(profile) <> 'object' THEN NULL
                WHEN $2 = 'contract' THEN
                    CASE WHEN jsonb_typeof(profile->'contract') = 'object'
                         THEN profile->'contract'->>'file_id' END
                ELSE CASE WHEN jsonb_typeof(profile->'docs') = 'object'
                          THEN profile->'docs'->>$2 END
           END
    FROM hr_employees WHERE team_member_id = $1 FOR UPDATE
"""

_REMOVE_DOC_SQL = """
    UPDATE hr_employees SET
        profile = jsonb_set(profile, '{docs}', (profile->'docs') - $2::text),
        updated_at = NOW()
    WHERE team_member_id = $1 AND jsonb_typeof(profile) = 'object'
      AND jsonb_typeof(profile->'docs') = 'object'
"""

_REMOVE_CONTRACT_SQL = """
    UPDATE hr_employees SET
        profile = jsonb_set(profile, '{contract}', (profile->'contract') - 'file_id'),
        updated_at = NOW()
    WHERE team_member_id = $1 AND jsonb_typeof(profile) = 'object'
      AND jsonb_typeof(profile->'contract') = 'object'
"""

_AUDIT_SQL = """
    INSERT INTO security_audit_log
        (user_id, action, resource_type, resource_id, success, details)
    VALUES ($1, $2, 'hr_employee', $3, TRUE, $4::jsonb)
"""


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

    async def _read_context(self, member_id: str) -> _Context:
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(_CONTEXT_SQL, member_id)
        except Exception:
            raise HRDocError(HRDocCode.DB_ERROR) from None
        if row is None:
            raise HRDocError(HRDocCode.NOT_FOUND)
        if row["employee_id"] is None:
            raise HRDocError(HRDocCode.HR_RECORD_REQUIRED)
        return _Context(full_name=str(row["full_name"] or ""), unit=str(row["unit"] or ""))

    async def ensure_folder(self, member_id: str) -> tuple[str, _Context]:
        """Find or create the person's folder (moving it when the unit changed), serialised per
        member (and per unit, so two people of one new unit cannot make two unit folders)."""
        ctx = await self._read_context(member_id)
        unit = unit_folder_name(ctx.unit)
        try:
            async with self.db_pool.acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('hr_docs_unit:'||$1))", unit
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", member_id
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
        actor_id: str,
        member_id: str,
        email: str,
        slot: str,
        new_id: str | None,
    ) -> str | None:
        """Returns the previous file id. Raises HRDocError; the transaction rolled back whole."""
        service = HRService(self.db_pool)
        try:
            async with self.db_pool.acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
                if not await service.lock_visible_person(conn, member_id, email):
                    raise HRDocError(HRDocCode.NOT_FOUND)
                prev_row = await conn.fetchrow(_PREVIOUS_SQL, member_id, slot)
                if prev_row is None:
                    raise HRDocError(HRDocCode.HR_RECORD_REQUIRED)
                previous = prev_row[0]
                if new_id is not None:
                    patch = (
                        {"contract": {"file_id": new_id}}
                        if slot == CONTRACT_SLOT
                        else {"docs": {slot: new_id}}
                    )
                    if await service.merge_card_on(conn, member_id, {}, patch) is None:
                        raise HRDocError(HRDocCode.NOT_FOUND)
                elif previous:
                    sql = _REMOVE_CONTRACT_SQL if slot == CONTRACT_SLOT else _REMOVE_DOC_SQL
                    if slot == CONTRACT_SLOT:
                        await conn.execute(sql, member_id)
                    else:
                        await conn.execute(sql, member_id, slot)
                if new_id is not None or previous:
                    await conn.execute(
                        _AUDIT_SQL,
                        actor_id,
                        AUDIT_UPLOAD if new_id is not None else AUDIT_REMOVE,
                        member_id,
                        {"slot": slot, "new_id": new_id, "previous_id": previous},
                    )
                return str(previous) if previous else None
        except HRDocError:
            raise
        except HRCardError as exc:
            if exc.code is HRCardCode.LOCK_TIMEOUT:
                raise HRDocError(HRDocCode.LOCK_TIMEOUT) from None
            if exc.code is HRCardCode.HIRE_DATE_REQUIRED:
                raise HRDocError(HRDocCode.HR_RECORD_REQUIRED) from None
            raise HRDocError(HRDocCode.DB_ERROR) from None
        except _LOCK_ERRORS:
            raise HRDocError(HRDocCode.LOCK_TIMEOUT) from None
        except Exception:
            raise HRDocError(HRDocCode.DB_ERROR) from None

    async def _trash_if_ours(self, member_id: str, previous: str | None, new_id: str | None) -> str:
        """After the commit: trash the replaced file only when this door made it (its appProperties
        name this member); a file someone else put there is never touched."""
        if not previous or previous == new_id:
            return "none"
        try:
            props = await self.drive.get_app_properties(previous)
            if props is None:  # already gone or in the trash
                return "none"
            if props.get(MEMBER_PROP) != member_id:
                return "kept"
            await self.drive.trash_file(previous)
            return "trashed"
        except Exception as exc:
            logger.warning("hr documents previous file left in place (%s)", type(exc).__name__)
            return "kept"

    # ── public flows ──

    async def upload(
        self,
        *,
        actor_id: str,
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
        try:
            previous = await self._db_step(
                actor_id=actor_id, member_id=member_id, email=email, slot=slot, new_id=new_id
            )
        except HRDocError:
            try:  # compensation: the just-uploaded file must not be left orphaned
                await self.drive.trash_file(new_id)
            except Exception as exc:
                logger.error("hr documents compensation trash failed (%s)", type(exc).__name__)
            raise
        outcome = await self._trash_if_ours(member_id, previous, new_id)
        return {"slot": slot, "file_id": new_id, "previous": outcome}

    async def remove(
        self, *, actor_id: str, member_id: str, email: str, slot: str
    ) -> dict[str, Any]:
        await self._read_context(member_id)  # 404 / hr_record_required before the write
        previous = await self._db_step(
            actor_id=actor_id, member_id=member_id, email=email, slot=slot, new_id=None
        )
        outcome = await self._trash_if_ours(member_id, previous, None)
        return {"slot": slot, "file_id": None, "previous": outcome}


_drive_singleton: Any = None


async def get_hr_drive_service() -> Any:
    """The delegated Drive client for the HR tree (built once; its constructor does blocking I/O)."""
    global _drive_singleton
    if _drive_singleton is None:
        import asyncio

        from backend.services.integrations.service_account_drive_service import (
            ServiceAccountDriveService,
        )

        _drive_singleton = await asyncio.to_thread(
            ServiceAccountDriveService,
            root_folder_id=settings.hr_documents_drive_root_id,
            delegated_user=settings.hr_documents_drive_subject,
        )
    return _drive_singleton
