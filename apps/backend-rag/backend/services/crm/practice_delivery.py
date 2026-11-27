"""Final staff outputs, distinct from documents requested from the client."""

import base64
import binascii
import hashlib
from datetime import date
from typing import Any

from fastapi import BackgroundTasks, HTTPException

from backend.app.deps.process_access import assignment_clause, process_assignees
from backend.app.routers.crm_enhanced_documents import DocumentUploadBase64, upload_document_base64
from backend.app.services.hr.delivery_bonus import get_delivery_bonus, record_delivery_bonus
from backend.app.services.hr.hr_service import business_today
from backend.app.utils.client_pools import caller_pool_peers
from backend.app.utils.crm_utils import is_crm_admin
from backend.app.utils.service_accounts import is_human_team_member, normalize_role
from backend.core.cache import invalidate_cache
from backend.services.crm.admin_document_visibility import admin_document_not_deleted_clause
from backend.services.crm.delivery_catalog import delivery_policy
from backend.services.crm.documents import auto_categorize_document
from backend.services.crm.practice_state_machine import normalize_state
from backend.services.crm.visa_followup import get_followup, record_visa_milestone
from backend.services.portal._document_visibility import document_visibility_clause

# Same transport ceiling as the Kita base64 uploader.
MAX_BYTES = 3 * 1024 * 1024
OPERATORS = {
    "consultant",
    "junior consultant",
    "senior consultant",
    "executive consultant",
    "specialist advisor",
    "team leader",
    "supervisor",
    "tax lead",
    "tax consultant",
    "tax manager",
    "tax care",
    "tax supervisor",
    "tax responsible",
}
_LIVE = f"{admin_document_not_deleted_clause('d')} AND NOT COALESCE(d.is_archived, FALSE)"
# Zero 2026-11-27: a November practice completed before Final delivery earns when its proof
# is uploaded. Older months were settled by the status-change rule and earn nothing now.
LATE_PROOF_FROM = date(2026, 11, 1)
_SCOPED = """d.practice_id = p.id AND d.client_id = p.client_id
             AND d.family_member_id IS NOT DISTINCT FROM p.family_member_id"""


def can_manage_delivery(user: dict, assigned_to: str | None) -> bool:
    role = normalize_role(user.get("role"))
    return (
        bool(user.get("email"))
        and is_human_team_member(role)
        and (
            is_crm_admin(user)
            or (
                role in OPERATORS and (assigned_to or "").strip().lower() in caller_pool_peers(user)
            )
        )
    )


async def practice(
    conn: Any, practice_id: int, user: dict, *, lock: bool = False, read_only: bool = False
) -> dict:
    if not is_human_team_member(user.get("role")) or not user.get("email"):
        raise HTTPException(403, "Staff access required")
    params: list[Any] = [practice_id]
    owners = await process_assignees(conn, user)
    row = await conn.fetchrow(
        """SELECT p.id, p.client_id, p.family_member_id, p.assigned_to, p.client_visible,
                  p.status, p.payment_status, pt.code, pt.name, pt.category
           FROM practices p JOIN practice_types pt ON pt.id = p.practice_type_id
           WHERE p.id = $1 AND (p.family_member_id IS NULL OR EXISTS (
               SELECT 1 FROM client_family_members f WHERE f.id=p.family_member_id AND f.client_id=p.client_id))"""
        + assignment_clause(owners, params)
        + (" FOR UPDATE OF p" if lock else ""),
        *params,
    )
    if not row or (not read_only and not can_manage_delivery(user, row["assigned_to"])):
        raise HTTPException(404, "Practice not found")
    return dict(row)


def require_open(p: dict) -> None:
    if normalize_state(p["status"] or "") != "on_process" or p["payment_status"] != "paid":
        raise HTTPException(409, "Final delivery requires a paid practice that is On Process")


async def state(conn: Any, p: dict, user: dict | None = None) -> dict:
    policy = delivery_policy(p["code"])
    if not policy:
        raise HTTPException(409, "No delivery requirements configured for this service")
    slots = policy["slots"]
    needs_titles = p["code"] in {"other_custom", "company_revision"}
    if needs_titles:
        titles = await conn.fetchval(
            "SELECT titles FROM practice_delivery_titles WHERE practice_id=$1 AND service_code=$2",
            p["id"],
            p["code"],
        )
        slots = [
            {"key": f"custom_{i}", "label": title, "condition": None}
            for i, title in enumerate(titles or [], 1)
        ]
        needs_titles = not titles
    rows = await conn.fetch(
        f"""SELECT s.slot_key, s.waiver_reason, s.issued_on, d.id AS document_id, d.file_name
            FROM practice_delivery_slots s JOIN practices p ON p.id = s.practice_id
            LEFT JOIN documents d ON d.id = s.document_id AND {_SCOPED} AND {_LIVE} AND NULLIF(d.file_id,'') IS NOT NULL
            WHERE s.practice_id=$1 AND s.service_code=$2""",
        p["id"],
        p["code"],
    )
    saved = {row["slot_key"]: row for row in rows}
    bonus = await get_delivery_bonus(conn, p["id"])
    resolved = [
        {
            **slot,
            **{
                field: saved.get(slot["key"], {}).get(field)
                for field in ("waiver_reason", "document_id", "file_name", "issued_on")
            },
        }
        for slot in slots
    ]
    completed = p["status"] == "completed"
    delivered = completed and await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM practice_delivery_completions WHERE practice_id=$1 AND service_code=$2)",
        p["id"],
        p["code"],
    )
    for slot in resolved:
        # The first proof completes the practice; the other documents, and a corrected
        # file, still reach the completed case.
        slot["can_repair"] = bool(completed and not slot["waiver_reason"])
        if (
            slot["key"] == policy["bonus_slot"]
            and slot["issued_on"] is None
            and bonus
            and bonus["event_kind"] == "electronic_visa_issued"
        ):
            slot["issued_on"] = bonus.get("evidence_issued_on")
        slot["reusable_document_id"] = next(
            (
                other["document_id"]
                for other in resolved
                if not slot["document_id"]
                and not slot["waiver_reason"]
                and other["document_id"]
                and combined_permit(slot, other)
            ),
            None,
        )
    view = {
        "status": p["status"],
        "delivery_recorded": bool(delivered),
        "needs_titles": bool(needs_titles),
        "slots": resolved,
        "bonus_slot": policy["bonus_slot"],
        "bonus_trigger": policy["bonus_trigger"],
        "visa_followup": (
            await get_followup(conn, p["id"], user=user) if policy["followup_slot"] else None
        ),
        "bonus_event": bonus["event_kind"] if bonus else None,
        "bonus_status": bonus.get("status") if bonus else None,
        "bonus_rate": await conn.fetchval(
            """SELECT amount_idr FROM hr_bonus_rates WHERE practice_type_code=$1 AND is_active
               AND effective_from<=$2 AND (effective_to IS NULL OR effective_to>=$2)""",
            p["code"],
            business_today(),
        ),
    }
    view["proof_ready"] = proof_kind(view) is not None
    return view


def proof_kind(view: dict) -> str | None:
    """The bonus event the saved documents prove: the bonus slot, else any final document."""
    bonus = next((s for s in view["slots"] if s["key"] == view["bonus_slot"]), None)
    if bonus and not bonus["waiver_reason"]:
        return view["bonus_trigger"] if bonus["document_id"] else None
    return "service_delivered" if any(s["document_id"] for s in view["slots"]) else None


def slot_for(view: dict, key: str) -> dict:
    for slot in view["slots"]:
        if slot["key"] == key:
            return slot
    raise HTTPException(400, "This slot is not part of the practice's delivery")


async def audit(conn: Any, practice_id: int, actor: str, description: str) -> None:
    await conn.execute(
        """INSERT INTO activity_log(entity_type, entity_id, action, performed_by, description)
           VALUES ('practice', $1, 'updated', $2, $3)""",
        practice_id,
        actor,
        description,
    )


async def set_titles(pool: Any, practice_id: int, user: dict, titles: list[str]) -> dict:
    if len({t.casefold() for t in titles}) != len(titles):
        raise HTTPException(422, "Use distinct document titles")
    async with pool.acquire() as conn, conn.transaction():
        p = await practice(conn, practice_id, user, lock=True)
        # A custom practice completed before Final delivery names its documents to receive its proof.
        if p["status"] != "completed":
            require_open(p)
        if p["code"] not in {"other_custom", "company_revision"}:
            raise HTTPException(409, "This service has fixed delivery requirements")
        if await conn.fetchval(
            "SELECT 1 FROM practice_delivery_slots WHERE practice_id=$1", practice_id
        ):
            raise HTTPException(409, "Documents are already staged for this practice")
        await conn.execute(
            """INSERT INTO practice_delivery_titles(practice_id,service_code,titles)
               VALUES($1,$2,$3) ON CONFLICT(practice_id) DO UPDATE
               SET service_code=excluded.service_code,titles=excluded.titles""",
            practice_id,
            p["code"],
            titles,
        )
        await audit(conn, practice_id, user["email"], "Final output titles set")
        return await state(conn, p, user)


async def waive(
    pool: Any, practice_id: int, key: str, user: dict, reason: str, tasks: BackgroundTasks
) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        p = await practice(conn, practice_id, user, lock=True)
        # A requirement found inapplicable after the first proof is still recorded.
        if p["status"] != "completed":
            require_open(p)
        slot = slot_for(await state(conn, p, user), key)
        if not slot["condition"] or slot["document_id"]:
            raise HTTPException(409, "Only unfilled conditional slots may be marked inapplicable")
        await conn.execute(
            """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,waiver_reason,updated_by)
               VALUES($1,$2,$3,$4,$5) ON CONFLICT(practice_id,slot_key) DO UPDATE
               SET service_code=excluded.service_code,waiver_reason=excluded.waiver_reason,
                   document_id=NULL,company_id=NULL,company_document_id=NULL,
                   updated_by=excluded.updated_by,updated_at=now()""",
            practice_id,
            p["code"],
            key,
            reason,
            user["email"],
        )
        # Waiving an unfilled bonus slot can turn an earlier upload into the proof.
        completed = await accept(conn, p, key, user)
        await audit(conn, practice_id, user["email"], f"Delivery slot {key} inapplicable: {reason}")
        updated = await state(conn, {**p, "status": "completed"} if completed else p, user)
    if completed:
        notify_completed(pool, p, user, tasks)
    await invalidate_delivery_cache(p)
    return updated


def upload_digest(encoded: str, file_name: str, mime_type: str) -> str:
    # Imported here so the light API process does not load the portal document pipeline at startup.
    from backend.services.portal.document_processing import VirusScanner

    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Invalid base64 file") from exc
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(422, "Upload a nonempty file of up to 3 MB")
    if (
        not VirusScanner.validate_mime_type(mime_type)
        or not VirusScanner.scan(data, file_name)["clean"]
    ):
        raise HTTPException(422, "Unsupported file for final delivery")
    return hashlib.sha256(data).hexdigest()


async def check_fingerprint(
    conn: Any, p: dict, slot_key: str, digest: str, *, record: bool
) -> None:
    # Mutating callers hold the practice row lock and roll this insert back
    # with the delivery if any subsequent handler fails.
    if record:
        await conn.execute(
            """INSERT INTO practice_delivery_fingerprints(practice_id,sha256,service_code,slot_key)
               VALUES($1,$2,$3,$4) ON CONFLICT(practice_id,sha256) DO NOTHING""",
            p["id"],
            digest,
            p["code"],
            slot_key,
        )
    other_kind = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM practice_delivery_fingerprints
                         WHERE practice_id=$1 AND sha256=$2 AND (service_code<>$3 OR slot_key<>$4))""",
        p["id"],
        digest,
        p["code"],
        slot_key,
    )
    if other_kind:
        raise HTTPException(
            409,
            "This file belongs to another output. For a combined permit, use the confirmed reuse action.",
        )


async def staged_file(
    conn: Any, p: dict, company_id: int | None, marker: str, kind: str, *, lock: bool = False
) -> Any:
    return await conn.fetchrow(
        f"""SELECT d.id AS document_id, cd.id AS company_document_id
            FROM documents d JOIN practices p ON p.id=$1
            LEFT JOIN company_documents cd ON cd.company_id=$2 AND cd.google_drive_file_id=d.file_id
            WHERE d.intake_idempotency_key=$3 AND d.document_type=$4 AND {_SCOPED} AND {_LIVE}
              AND d.uploaded_source='team' AND d.file_id IS NOT NULL
              AND ($2::int IS NULL OR cd.id IS NOT NULL)
            LIMIT 1"""
        + (" FOR UPDATE OF d" if lock else ""),
        p["id"],
        company_id,
        marker,
        kind,
    )


async def recoverable_marker(conn: Any, p: dict, base: str) -> str:
    # The generic store's idempotency probe also matches deleted rows. A fresh
    # key after an archive/delete keeps the old row hidden and retries stable.
    retired = await conn.fetchval(
        f"""SELECT max(d.id) FROM documents d WHERE d.client_id=$1
            AND (d.intake_idempotency_key=$2 OR d.intake_idempotency_key LIKE $2 || ':%')
            AND (NOT ({_LIVE}) OR NULLIF(d.file_id,'') IS NULL OR d.family_member_id IS DISTINCT FROM $3)""",
        p["client_id"],
        base,
        p["family_member_id"],
    )
    return f"{base}:{retired}" if retired else base


async def existing_output(conn: Any, p: dict, slot: dict, request: Any) -> Any:
    # The generic uploader refuses an exact copy even if the original arrived
    # in another CRM workflow. Adopt only an already client-visible staff
    # output of the same type and subject. Never promote a private intake row.
    digest = hashlib.md5(base64.b64decode(request.file_content), usedforsecurity=False).hexdigest()
    return await conn.fetchrow(
        f"""SELECT d.id AS document_id, cd.id AS company_document_id
            FROM documents d JOIN practices p ON p.id=$1
            LEFT JOIN company_documents cd ON cd.company_id=$2 AND cd.google_drive_file_id=d.file_id
            WHERE {_SCOPED} AND {document_visibility_clause("d")} AND d.uploaded_source='team'
              AND d.file_id IS NOT NULL AND d.content_hash=$3 AND d.document_type=$4
              AND ($2::int IS NULL OR cd.id IS NOT NULL AND cd.client_visible IS TRUE)
            ORDER BY d.id LIMIT 1 FOR UPDATE OF d""",
        p["id"],
        request.company_id,
        digest,
        document_type(slot["key"], slot["label"]),
    )


def document_type(key: str, label: str) -> str:
    # CRM recap and expiry cards classify immigration documents by this field.
    # Slot keys are stable across label changes and intentionally generic.
    words = label.upper().replace("—", " ").replace("(", " ").split()
    for token, kind in (("ITK", "itk"), ("E-ITAS", "itas"), ("E-ITAP", "itap"), ("IMK", "imk")):
        if token in words:
            return kind
    if key == "electronic_visa":
        return "evisa"
    # These identifiers are also the company profile vault's named output cards.
    company_types = {
        "Akta Pendirian": "akta_pendirian",
        "SK Pengesahan AHU": "sk_decree",
        "NIB": "nib",
        "Company NPWP": "npwp",
        "Akta Perubahan": "akta_perubahan",
    }
    if label in company_types:
        return company_types[label]
    category = auto_categorize_document(label)
    if category["matched_keyword"]:
        return category["document_type"].lower().replace(" ", "_")
    return key


def combined_permit(first: dict, second: dict) -> bool:
    types = {document_type(s["key"], s["label"]) for s in (first, second)}
    return types in ({"imk", "itas"}, {"imk", "itap"})


async def reuse(
    pool: Any, practice_id: int, key: str, user: dict, document_id: int, tasks: BackgroundTasks
) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        p = await practice(conn, practice_id, user, lock=True)
        repair = p["status"] == "completed"
        if not repair:
            require_open(p)
        view = await state(conn, p, user)
        slot = slot_for(view, key)
        if slot["document_id"] == document_id:
            return await state(conn, p, user)
        if (repair and not slot["can_repair"]) or slot["reusable_document_id"] != document_id:
            raise HTTPException(409, "Select the combined permit already attached to this practice")
        source = await conn.fetchrow(
            f"""SELECT s.company_id FROM practice_delivery_slots s
                JOIN practices p ON p.id=s.practice_id JOIN documents d ON d.id=s.document_id
                WHERE p.id=$1 AND s.service_code=$2 AND d.id=$3 AND {_SCOPED} AND {_LIVE}
                  AND NULLIF(d.file_id,'') IS NOT NULL LIMIT 1 FOR UPDATE OF d""",
            practice_id,
            p["code"],
            document_id,
        )
        if not source:
            raise HTTPException(409, "The combined permit is no longer available")
        previous = await previous_slot(conn, p, key)
        if repair and previous and previous.get("company_id") != source.get("company_id"):
            raise HTTPException(409, "A repair cannot change the permit's company")
        await conn.execute(
            """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,
                                                  company_id,company_document_id,updated_by)
               SELECT practice_id,service_code,$3,document_id,company_id,company_document_id,$5
               FROM practice_delivery_slots WHERE practice_id=$1 AND service_code=$2 AND document_id=$4
               LIMIT 1 ON CONFLICT(practice_id,slot_key) DO UPDATE
               SET service_code=excluded.service_code, document_id=excluded.document_id,
                   company_id=excluded.company_id,company_document_id=excluded.company_document_id,
                   waiver_reason=NULL,updated_by=excluded.updated_by,updated_at=now()""",
            practice_id,
            p["code"],
            key,
            document_id,
            user["email"],
        )
        completed = await accept(conn, p, key, user)
        await retire_replaced_file(conn, p, previous)
        await audit(conn, practice_id, user["email"], f"Combined permit confirmed for slot: {key}")
        updated = await state(conn, {**p, "status": "completed"} if completed else p, user)
    if completed:
        notify_completed(pool, p, user, tasks)
    await invalidate_delivery_cache(p)
    return updated


async def stage(
    pool: Any,
    p: dict,
    slot: dict,
    user: dict,
    request: Any,
    marker: str,
    tasks: BackgroundTasks,
    *,
    repair: bool = False,
) -> None:
    # A private draft remains recoverable by its deterministic storage key if
    # the operation is interrupted between Drive and the delivery transaction.
    kind = document_type(slot["key"], slot["label"])
    async with pool.acquire() as conn:
        if await staged_file(conn, p, request.company_id, marker, kind):
            return
    from backend.services.portal._mixins.documents import PortalDocumentsMixin

    category = (
        "tax"
        if p["category"].startswith("tax")
        else (
            "pma"
            if p["category"] == "company"
            else PortalDocumentsMixin._classify_document_category(
                p["category"] + " " + slot["label"], request.file_name
            )
        )
    )
    data = DocumentUploadBase64(
        file=request.file_content,
        file_name=request.file_name,
        mime_type=request.mime_type,
        document_type=kind,
        document_category=category,
        client_visible=False,
        practice_id=p["id"],
        family_member_id=p["family_member_id"],
        company_id=request.company_id,
        intake_idempotency_key=marker,
    )
    try:
        await upload_document_base64(
            p["client_id"],
            data,
            pool=pool,
            current_user=user,
            background_tasks=tasks,
            access_already_verified=True,
            uploaded_source="team",
            missing_delivery_slot=slot["key"] if repair else None,
        )
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        # The canonical writer refuses duplicate bytes. A competing retry is
        # safe to reuse only when its server-generated storage identity matches.
        async with pool.acquire() as conn:
            if not await staged_file(conn, p, request.company_id, marker, kind) and not (
                str(exc.detail).startswith("Duplicate file:")
                and await existing_output(conn, p, slot, request)
            ):
                raise


def issue_date(view: dict, slot: dict, supplied: date | None) -> date | None:
    if view["bonus_slot"] != "electronic_visa" or slot["key"] != "electronic_visa":
        if supplied is not None:
            raise HTTPException(422, "An issue date belongs only on the Electronic Visa")
        return None
    original = slot.get("issued_on")
    if original is not None:
        return original
    if supplied is None or supplied > business_today():
        raise HTTPException(
            422, "Enter the official Electronic Visa issue date, today or earlier in Bali"
        )
    return supplied


async def previous_slot(conn: Any, p: dict, key: str) -> Any:
    return await conn.fetchrow(
        """SELECT document_id, company_document_id, company_id FROM practice_delivery_slots
           WHERE practice_id=$1 AND service_code=$2 AND slot_key=$3""",
        p["id"],
        p["code"],
        key,
    )


async def retire_replaced_file(conn: Any, p: dict, previous: Any) -> None:
    if not previous or not previous.get("document_id"):
        return
    # Only hide a workflow-owned version from this exact practice/subject.
    # Capturing IDs before replacement also handles a new upload without a
    # company and an old archived document that disappeared from the live view.
    await conn.execute(
        f"""UPDATE documents d SET client_visible=false FROM practices p
            WHERE p.id=$1 AND d.id=$2 AND {_SCOPED} AND d.uploaded_source='team'
              AND d.intake_idempotency_key LIKE 'case-delivery:%' AND NOT EXISTS
              (SELECT 1 FROM practice_delivery_slots s WHERE s.document_id=d.id)""",
        p["id"],
        previous["document_id"],
    )
    if previous["company_document_id"] is None:
        return
    await conn.execute(
        f"""UPDATE company_documents cd SET client_visible=false
            FROM documents d JOIN practices p ON p.id=$1
            WHERE cd.id=$3 AND cd.company_id=$4 AND d.id=$2 AND {_SCOPED}
              AND d.uploaded_source='team'
              AND (cd.google_drive_file_id=d.file_id OR (NULLIF(d.file_id,'') IS NULL AND (
                to_jsonb(cd)->>'intake_idempotency_key'=d.intake_idempotency_key OR
                strpos(COALESCE(to_jsonb(cd)->>'notes',''),'[intake_key:' || d.intake_idempotency_key || ']')>0)))
              AND d.intake_idempotency_key LIKE ('case-delivery:' || p.id::text || ':' || cd.company_id::text || ':%')
              AND EXISTS(SELECT 1 FROM client_company_links l WHERE l.client_id=p.client_id AND l.company_id=cd.company_id)
              AND NOT EXISTS (
                  SELECT 1 FROM practice_delivery_slots s
                  JOIN practices owner ON owner.id=s.practice_id
                  JOIN practice_types pt ON pt.id=owner.practice_type_id AND pt.code=s.service_code
                  JOIN documents source ON source.id=s.document_id AND source.practice_id=owner.id
                    AND source.client_id=owner.client_id AND source.family_member_id IS NOT DISTINCT FROM owner.family_member_id
                  WHERE s.company_document_id=cd.id AND s.company_id=cd.company_id AND source.file_id=cd.google_drive_file_id
                    AND {admin_document_not_deleted_clause("source")} AND NOT COALESCE(source.is_archived,false)
                    AND EXISTS(SELECT 1 FROM client_company_links cl
                               WHERE cl.company_id=cd.company_id AND cl.client_id=owner.client_id))""",
        p["id"],
        previous["document_id"],
        previous["company_document_id"],
        previous["company_id"],
    )


async def upload(
    pool: Any,
    practice_id: int,
    key: str,
    user: dict,
    request: Any,
    tasks: BackgroundTasks,
    *,
    repair: bool = False,
) -> dict:
    async with pool.acquire() as conn:
        snapshot = await practice(conn, practice_id, user)
        # A completed practice takes later documents on either route: a lost response's retry
        # of the first proof, or a file whose twin first proof completed the practice meanwhile.
        repair = repair or snapshot["status"] == "completed"
        if not repair:
            require_open(snapshot)
        initial_view = await state(conn, snapshot, user)
        initial_slot = slot_for(initial_view, key)
        if repair:
            if snapshot["status"] != "completed" or initial_slot["waiver_reason"]:
                raise HTTPException(
                    409,
                    "Only a required document of a completed practice can be added",
                )
            original_company = await case_company(conn, snapshot, key)
            if request.company_id is not None and request.company_id != original_company:
                raise HTTPException(409, "A repair cannot change the permit's company")
            request = request.model_copy(update={"company_id": original_company})
        issue_date(initial_view, initial_slot, request.issued_on)
    digest = upload_digest(request.file_content, request.file_name, request.mime_type)
    base = f"case-delivery:{practice_id}:{request.company_id or 0}:{key}:{digest}"
    async with pool.acquire() as conn:
        await check_fingerprint(conn, snapshot, key, digest, record=False)
        marker = await recoverable_marker(conn, snapshot, base)
        if repair and not initial_slot["can_repair"]:
            replay = await staged_file(
                conn,
                snapshot,
                request.company_id,
                marker,
                document_type(key, initial_slot["label"]),
            )
            if not replay:
                replay = await existing_output(conn, snapshot, initial_slot, request)
            if replay and replay["document_id"] == initial_slot["document_id"]:
                return initial_view
            raise HTTPException(409, "This delivered permit is already available")
    await stage(pool, snapshot, initial_slot, user, request, marker, tasks, repair=repair)
    async with pool.acquire() as conn, conn.transaction():
        p = await practice(conn, practice_id, user, lock=True)
        repair = repair or p["status"] == "completed"
        if not repair:
            require_open(p)
        view = await state(conn, p, user)
        slot = slot_for(view, key)
        if (
            any(
                p[k] != snapshot[k]
                for k in ("code", "client_id", "family_member_id", "assigned_to")
            )
            or (repair and p["status"] != "completed")
            or initial_slot["label"] != slot["label"]
        ):
            raise HTTPException(409, "Practice changed during upload. Review the current practice.")
        if request.company_id is not None and not await conn.fetchval(
            "SELECT 1 FROM client_company_links WHERE company_id=$1 AND client_id=$2 FOR SHARE",
            request.company_id,
            p["client_id"],
        ):
            raise HTTPException(409, "Company is not linked to this client")
        issued_on = issue_date(view, slot, request.issued_on)
        row = await staged_file(
            conn, p, request.company_id, marker, document_type(key, slot["label"]), lock=True
        )
        if not row:
            row = await existing_output(conn, p, slot, request)
        if not row:
            raise HTTPException(409, "Uploaded file does not match the practice subject")
        if request.company_id is not None and await conn.fetchval(
            "SELECT 1 FROM practice_delivery_slots WHERE practice_id=$1 AND company_id<>$2 LIMIT 1",
            practice_id,
            request.company_id,
        ):
            raise HTTPException(409, "Practice documents already name a different company")
        await check_fingerprint(conn, p, key, digest, record=True)
        # Replaying an upload into the slot that already owns the file is safe,
        # even if the separate combined-permit action also referenced it.
        if row["document_id"] == slot["document_id"]:
            return view
        if any(
            other["key"] != key and other["document_id"] == row["document_id"]
            for other in view["slots"]
        ):
            raise HTTPException(409, "This document already fills another slot")
        if (repair and not slot["can_repair"]) or slot["document_id"] != initial_slot[
            "document_id"
        ]:
            raise HTTPException(409, "Another operator changed this slot. Refresh and review.")
        previous = await previous_slot(conn, p, key)
        if repair and await case_company(conn, p, key) != request.company_id:
            raise HTTPException(409, "Permit destination changed. Refresh and review.")
        await conn.execute(
            """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,
                                                  company_id,company_document_id,updated_by,issued_on)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(practice_id,slot_key) DO UPDATE
               SET service_code=excluded.service_code, document_id=excluded.document_id,
                   company_id=excluded.company_id,company_document_id=excluded.company_document_id,
                   waiver_reason=NULL,issued_on=COALESCE(practice_delivery_slots.issued_on,excluded.issued_on),updated_by=excluded.updated_by,updated_at=now()""",
            practice_id,
            p["code"],
            key,
            row["document_id"],
            request.company_id,
            row["company_document_id"],
            user["email"],
            issued_on,
        )
        completed = await accept(conn, p, key, user, issued_on=issued_on)
        await retire_replaced_file(conn, p, previous)
        description = (
            "Final document added after completion" if repair else "Final document uploaded"
        )
        await audit(conn, practice_id, user["email"], f"{description}: {key}")
        updated = await state(conn, {**p, "status": "completed"} if completed else p, user)
    if completed:
        notify_completed(pool, p, user, tasks)
    await invalidate_delivery_cache(p)
    return updated


async def case_company(conn: Any, p: dict, key: str) -> int | None:
    """A file keeps its slot's company; a first file joins the company the case already names."""
    previous = await previous_slot(conn, p, key)
    if previous and previous["document_id"]:
        return previous["company_id"]
    return await conn.fetchval(
        """SELECT company_id FROM practice_delivery_slots WHERE practice_id=$1 AND service_code=$2
           AND company_id IS NOT NULL ORDER BY slot_key LIMIT 1""",
        p["id"],
        p["code"],
    )


async def accept(
    conn: Any, p: dict, key: str, user: dict, *, issued_on: date | None = None
) -> bool:
    """Apply a saved final document under the practice lock. True when it completed the practice.

    The first proof completes an open practice and earns its bonus at upload time. A completed
    practice publishes each later file; one completed before Final delivery earns on its proof
    only from LATE_PROOF_FROM.
    """
    view = await state(conn, p, user)
    kind = proof_kind(view)
    open_case = p["status"] != "completed"
    earns = kind is not None and (
        open_case
        or bool(
            await conn.fetchval(
                "SELECT (completion_date AT TIME ZONE 'Asia/Makassar')::date >= $2 FROM practices WHERE id=$1",
                p["id"],
                LATE_PROOF_FROM,
            )
        )
    )
    if key == view["bonus_slot"] or not open_case:
        await publish(conn, p, [key])
    if earns and kind != "service_delivered" and key == view["bonus_slot"]:
        await record_delivery_bonus(
            conn, p["id"], p["code"], p["assigned_to"], kind, issued_on=issued_on
        )
    elif earns and kind == "service_delivered" and not open_case:
        await record_delivery_bonus(conn, p["id"], p["code"], p["assigned_to"], kind)
    # The permit clock starts only from a recorded issuance receipt.
    receipt = await get_delivery_bonus(conn, p["id"])
    if view["bonus_trigger"] == "electronic_visa_issued" and (
        key != "electronic_visa" or (receipt or {}).get("event_kind") == "electronic_visa_issued"
    ):
        await record_visa_milestone(conn, p, key)
    if open_case and kind:
        await finish(conn, p, view, user)
        return True
    return False


async def publish(conn: Any, p: dict, keys: list[str]) -> None:
    visible = p["client_visible"] is not False
    await conn.execute(
        f"""UPDATE documents d SET client_visible=$4, updated_at=now()
            FROM practice_delivery_slots s JOIN practices p ON p.id=s.practice_id
            WHERE p.id=$1 AND s.service_code=$2 AND s.slot_key=ANY($3::text[])
              AND s.document_id=d.id AND {_SCOPED} AND {_LIVE}""",
        p["id"],
        p["code"],
        keys,
        visible,
    )
    await conn.execute(
        """UPDATE company_documents cd SET client_visible=$4
           FROM practice_delivery_slots s
           WHERE s.practice_id=$1 AND s.service_code=$2 AND s.slot_key=ANY($3::text[])
             AND s.company_document_id=cd.id AND s.company_id=cd.company_id
             AND EXISTS(SELECT 1 FROM client_company_links l WHERE l.company_id=cd.company_id AND l.client_id=$5)""",
        p["id"],
        p["code"],
        keys,
        visible,
        p["client_id"],
    )


async def complete(pool: Any, practice_id: int, user: dict, tasks: BackgroundTasks) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        p = await practice(conn, practice_id, user, lock=True)
        await conn.fetch(
            """SELECT d.id FROM documents d JOIN practice_delivery_slots s ON s.document_id=d.id
               WHERE s.practice_id=$1 ORDER BY d.id FOR UPDATE OF d""",
            practice_id,
        )
        view = await state(conn, p, user)
        prior_service = await conn.fetchval(
            "SELECT service_code FROM practice_delivery_completions WHERE practice_id=$1",
            practice_id,
        )
        if prior_service is not None and prior_service != p["code"]:
            raise HTTPException(409, "This practice has already delivered a different service")
        if p["status"] == "completed" and prior_service is not None:
            return view
        require_open(p)
        if proof_kind(view) is None and (
            not view["slots"]
            or any(not (s["document_id"] or s["waiver_reason"]) for s in view["slots"])
        ):
            raise HTTPException(409, "Upload the first final document first")
        if await conn.fetchval(
            """SELECT 1 FROM practice_delivery_slots s WHERE s.practice_id=$1 AND s.company_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM client_company_links l WHERE l.client_id=$2
                               AND l.company_id=s.company_id)""",
            practice_id,
            p["client_id"],
        ):
            raise HTTPException(409, "The practice company is no longer linked to this client")
        await finish(conn, p, view, user)
        updated = await state(conn, {**p, "status": "completed"}, user)
    notify_completed(pool, p, user, tasks)
    await invalidate_delivery_cache(p)
    return updated


async def finish(conn: Any, p: dict, view: dict, user: dict) -> None:
    """Complete an open practice under its lock: publish its files and record the delivery."""
    await publish(conn, p, [s["key"] for s in view["slots"] if s["document_id"]])
    await conn.execute(
        """INSERT INTO practice_delivery_completions(practice_id,service_code,delivered_by)
           VALUES($1,$2,$3) ON CONFLICT (practice_id) DO NOTHING""",
        p["id"],
        p["code"],
        user["email"],
    )
    await conn.execute(
        """UPDATE practices SET status='completed',completion_date=COALESCE(completion_date,now()),
           updated_at=CASE WHEN EXTRACT(DAY FROM now() AT TIME ZONE 'Asia/Makassar')>28
             THEN (date_trunc('month', now() AT TIME ZONE 'Asia/Makassar') + interval '1 month') AT TIME ZONE 'Asia/Makassar'
             ELSE now() END WHERE id=$1""",
        p["id"],
    )
    if not view["bonus_slot"] or slot_for(view, view["bonus_slot"])["waiver_reason"]:
        await record_delivery_bonus(conn, p["id"], p["code"], p["assigned_to"], "service_delivered")
    await audit(conn, p["id"], user["email"], "Final documents delivered and practice completed")
    if p["client_visible"] is not False:
        remaining = any(not (s["document_id"] or s["waiver_reason"]) for s in view["slots"])
        await conn.execute(
            """INSERT INTO timeline_events(client_id,practice_id,event_type,title,description,event_date,client_visible,color,created_by)
               VALUES($1,$2,'completion','Case completed',$4,now(),true,'success',$3)""",
            p["client_id"],
            p["id"],
            user["email"],
            "First final document delivered; the remaining documents will follow"
            if remaining
            else "Final documents delivered",
        )


def notify_completed(pool: Any, p: dict, user: dict, tasks: BackgroundTasks) -> None:
    from backend.services.crm.completed_process_service import CompletedProcessService
    from backend.services.portal.portal_notification_service import PortalNotificationService

    if p["client_visible"] is not False:
        tasks.add_task(
            PortalNotificationService(pool).notify_practice_status_changed,
            client_id=p["client_id"],
            practice_id=p["id"],
            practice_type=p["name"],
            new_status="completed",
            sent_by=user["email"],
        )
        tasks.add_task(
            CompletedProcessService(pool).trigger_on_completed,
            practice_id=p["id"],
            triggered_by=user["email"],
        )


async def invalidate_delivery_cache(p: dict) -> None:
    for pattern in (
        f"zantara:crm_practice:{p['id']}:*",
        "zantara:crm_practices:*",
        "zantara:crm_practices_stats:*",
        "zantara:crm_clients_stats:*",
        "zantara:crm_practices_revenue_growth:*",
        "zantara:hr_bonuses:*",
        f"zantara:portal_documents:{p['client_id']}:*",
    ):
        await invalidate_cache(pattern)


async def portal_files(conn: Any, practice_id: int, client_id: int) -> dict:
    if not await conn.fetchval(
        """SELECT 1 FROM practices WHERE id=$1 AND client_id=$2 AND client_visible IS NOT FALSE""",
        practice_id,
        client_id,
    ):
        raise HTTPException(404, "Practice not found")
    rows = await conn.fetch(
        f"""SELECT DISTINCT d.id, d.file_name FROM practice_delivery_slots s
            JOIN practices p ON p.id=s.practice_id JOIN practice_types pt ON pt.id=p.practice_type_id
            JOIN documents d ON d.id=s.document_id AND {_SCOPED}
            WHERE p.id=$1 AND p.client_id=$2 AND p.client_visible IS NOT FALSE
              AND s.service_code=pt.code AND {document_visibility_clause("d")}
            ORDER BY d.id""",
        practice_id,
        client_id,
    )
    return {"documents": [dict(row) for row in rows]}
