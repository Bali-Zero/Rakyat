"""Claim and settle document-bound passport work in PostgreSQL.

All mutators lock the document before its job, including the enrollment trigger.
A renewed lease replaces the token, fencing a worker that resumes after expiry.
"""

from typing import Any
from uuid import uuid4

from backend.app.utils.json_utils import to_jsonb

MAX_ATTEMPTS = 3
LEASE_SECONDS = 300
DEFER_SECONDS = 60

_BINDING = """
    j.document_id=d.id AND j.client_id=d.client_id AND j.file_id=d.file_id
    AND j.content_hash=coalesce(d.content_hash,'')
"""


async def enqueue_selected(pool: Any, doc_ids: list[int]) -> int:
    """Enroll only operator-selected, eligible historical documents."""
    if not 1 <= len(doc_ids) <= 20 or any(type(value) is not int or value < 1 for value in doc_ids):
        raise ValueError("Expected 1 to 20 positive document IDs")
    if len(set(doc_ids)) != len(doc_ids):
        raise ValueError("Duplicate document ID")
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            WITH selected AS (
                SELECT * FROM documents d WHERE d.id=ANY($1::bigint[])
                  AND crm_passport_ocr_writable(d) AND crm_passport_ocr_review_code(d) IS NULL
                  AND d.intake_idempotency_key IS NULL AND d.intake_proposal_id IS NULL
                  AND NOT EXISTS (SELECT FROM clients c WHERE c.id=d.client_id
                                  AND c.passport_ocr_data->>'file_id'=d.file_id)
                ORDER BY d.id FOR UPDATE
            ), inserted AS (
                INSERT INTO crm_passport_ocr_jobs(document_id,client_id,file_id,content_hash)
                SELECT id,client_id,file_id,coalesce(content_hash,'') FROM selected
                ON CONFLICT (document_id) DO NOTHING RETURNING document_id
            ) SELECT count(*) FROM inserted
            """,
            doc_ids,
        )


async def review_untyped(pool: Any, *, doc_id: int, client_id: int, file_id: str) -> bool:
    """Put a detected-but-untyped passport candidate into staff review.

    This never changes the declared type or client record.  It is intentionally
    narrow: only an otherwise clean, pending document with a non-passport type
    can be transitioned, once, by its exact document/client/file binding.
    """
    if type(doc_id) is not int or type(client_id) is not int or doc_id < 1 or client_id < 1:
        raise ValueError("doc_id and client_id must be positive integers")
    if not isinstance(file_id, str) or not file_id.strip():
        raise ValueError("file_id must be nonempty")
    async with pool.acquire() as conn:
        changed = await conn.fetchval(
            """UPDATE documents
               SET ocr_status='review', ocr_completed_at=now(),
                   ocr_extracted_data=jsonb_build_object(
                       'review_reason','confirm_passport_document_type'
                   )
             WHERE id=$1 AND client_id=$2
               AND file_id=$3
               AND NOT crm_passport_ocr_is_passport(document_type)
               AND intake_idempotency_key IS NULL AND intake_proposal_id IS NULL
               AND ocr_status='pending' AND ocr_completed_at IS NULL
               AND coalesce(ocr_extracted_data,'{}'::jsonb) IN ('{}'::jsonb,'null'::jsonb)
               AND verified_at IS NULL AND verified_by IS NULL AND rejection_reason IS NULL
               AND deleted_at IS NULL AND NOT coalesce(is_archived,false)
               AND status IN ('active','google_drive')
             RETURNING id""",
            doc_id,
            client_id,
            file_id,
        )
        return changed is not None


async def _review(conn: Any, doc_id: int, reason: str, writable: bool) -> None:
    await conn.execute(
        """UPDATE crm_passport_ocr_jobs SET status='review',terminal_reason=$2,
           lease_token=NULL,updated_at=now() WHERE document_id=$1""",
        doc_id,
        reason,
    )
    if writable:
        await conn.execute(
            """UPDATE documents SET ocr_status='review',ocr_completed_at=now(),
               ocr_extracted_data=jsonb_build_object('review_reason',$2::text) WHERE id=$1""",
            doc_id,
            reason,
        )


async def claim(
    pool: Any,
    *,
    doc_id: int | None = None,
    client_id: int | None = None,
    file_id: str | None = None,
) -> dict[str, Any] | None:
    binding = (doc_id, client_id, file_id)
    if any(value is not None for value in binding) and any(value is None for value in binding):
        raise ValueError("A targeted claim requires document, client and file")
    async with pool.acquire() as conn, conn.transaction():
        selected = await conn.fetchval(
            """SELECT d.id FROM crm_passport_ocr_jobs j JOIN documents d ON d.id=j.document_id
               WHERE j.status IN ('pending','leased') AND j.available_at<=clock_timestamp()
                 AND ($1::bigint IS NULL OR (j.document_id=$1 AND j.client_id=$2 AND j.file_id=$3))
               ORDER BY j.available_at,j.document_id LIMIT 1 FOR UPDATE OF d SKIP LOCKED""",
            *binding,
        )
        if selected is None:
            return None
        row = await conn.fetchrow(
            f"""SELECT j.document_id,j.client_id,j.file_id,j.content_hash,j.attempts,
                       ({_BINDING}) AND crm_passport_ocr_writable(d) AS writable,
                       crm_passport_ocr_review_code(d) AS reason
                FROM crm_passport_ocr_jobs j JOIN documents d ON d.id=j.document_id
                WHERE j.document_id=$1 AND j.status IN ('pending','leased')
                  AND j.available_at<=clock_timestamp() FOR UPDATE OF j""",
            selected,
        )
        if row is None:
            return None
        if not row["writable"] or row["reason"] or row["attempts"] >= MAX_ATTEMPTS:
            reason = row["reason"] or ("attempt_limit" if row["writable"] else "binding_changed")
            await _review(conn, row["document_id"], reason, bool(row["writable"]))
            return None
        owner = await conn.fetchval(
            "SELECT full_name FROM clients WHERE id=$1 AND status='active' AND deleted_at IS NULL FOR SHARE",
            row["client_id"],
        )
        if not owner:
            await _review(conn, row["document_id"], "client_identity_unavailable", True)
            return None
        token = uuid4()
        attempts = await conn.fetchval(
            """UPDATE crm_passport_ocr_jobs SET status='leased',lease_token=$2,attempts=attempts+1,
               available_at=clock_timestamp()+$3 * interval '1 second',updated_at=now()
               WHERE document_id=$1 AND status IN ('leased','pending') AND available_at<=clock_timestamp()
               RETURNING attempts""",
            row["document_id"],
            token,
            LEASE_SECONDS,
        )
        if attempts is None:
            return None
        return {**dict(row), "token": token, "attempts": attempts, "full_name": owner}


async def finish(
    pool: Any,
    job: dict[str, Any],
    *,
    extracted: dict[str, Any] | None = None,
    reason: str | None = None,
    retryable: bool = False,
) -> str:
    if bool(extracted) == bool(reason) or (retryable and not reason):
        raise ValueError("Expected an extraction or a failure reason")
    doc_id = job["document_id"]
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchval("SELECT id FROM documents WHERE id=$1 FOR UPDATE", doc_id)
        row = await conn.fetchrow(
            f"""SELECT j.attempts,({_BINDING}) AND crm_passport_ocr_writable(d) AS writable,
                       crm_passport_ocr_review_code(d) AS reason
                FROM documents d JOIN crm_passport_ocr_jobs j ON j.document_id=d.id
                WHERE d.id=$1 AND j.client_id=$2 AND j.file_id=$3 AND j.content_hash=$4
                  AND j.status='leased' AND j.lease_token=$5 AND j.available_at>clock_timestamp()
                FOR UPDATE OF j""",
            doc_id,
            job["client_id"],
            job["file_id"],
            job["content_hash"],
            job["token"],
        )
        if row is None:
            return "stale"
        if not row["writable"]:
            await _review(conn, doc_id, "binding_changed", False)
            return "stale"
        owner = await conn.fetchval(
            "SELECT full_name FROM clients WHERE id=$1 AND status='active' AND deleted_at IS NULL FOR SHARE",
            job["client_id"],
        )
        changed = row["reason"] or (
            "client_identity_changed" if owner != job["full_name"] else None
        )
        if changed or (reason and (not retryable or row["attempts"] >= MAX_ATTEMPTS)):
            await _review(conn, doc_id, changed or reason, True)
            return "review"
        if retryable:
            await conn.execute(
                """UPDATE crm_passport_ocr_jobs SET status='pending',lease_token=NULL,
                   available_at=clock_timestamp()+$2 * interval '1 second',updated_at=now()
                   WHERE document_id=$1""",
                doc_id,
                DEFER_SECONDS,
            )
            return "deferred"
        await conn.execute(
            """UPDATE documents SET ocr_status='completed',ocr_completed_at=now(),
               ocr_extracted_data=$2::text::jsonb WHERE id=$1""",
            doc_id,
            to_jsonb(extracted),
        )
        await conn.execute(
            """UPDATE crm_passport_ocr_jobs SET status='completed',lease_token=NULL,updated_at=now()
               WHERE document_id=$1""",
            doc_id,
        )
        return "completed"
