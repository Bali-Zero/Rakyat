"""Employee-only post-issuance permit tracking in Bali calendar days."""

from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any

import asyncpg
import httpx
from fastapi import HTTPException

from backend.app.deps.owner import OWNER_EMAILS, require_owner
from backend.app.services.hr.hr_service import business_today
from backend.app.services.internal_email import EmailProviderRejected, send_internal_email
from backend.app.utils.service_accounts import is_human_team_member
from backend.services.crm.delivery_catalog import delivery_policy

OWNER_MAILBOX = "zero@balizero.com"


async def record_visa_milestone(conn: Any, practice: dict, slot_key: str) -> None:
    """Caller owns the practice lock and transaction that accepted the file.

    The first eVisa starts the clock, irrespective of its printed issue date.
    The database also reacts to document archive/deletion and explicit reuse.
    """
    policy = delivery_policy(practice["code"]) or {}
    permit = policy.get("followup_slot")
    if not permit or slot_key not in {"electronic_visa", permit}:
        return
    if slot_key == "electronic_visa":
        has_receipt = await conn.fetchval(
            """WITH issuance AS (
                   SELECT practice_id,service_code,(recorded_at AT TIME ZONE 'Asia/Makassar')::date AS day_zero
                   FROM hr_delivery_bonus_events WHERE practice_id=$1 AND service_code=$2
                     AND event_kind='electronic_visa_issued'
               ), recorded AS (
                   INSERT INTO practice_visa_followups(practice_id,service_code,slot_key,started_on,due_on,allow_postpone)
                   SELECT practice_id,service_code,$3,day_zero,day_zero+30,$4 FROM issuance
                   ON CONFLICT(practice_id) DO NOTHING
               )
               SELECT EXISTS (SELECT 1 FROM issuance)""",
            practice["id"],
            practice["code"],
            permit,
            bool(policy.get("can_postpone")),
        )
        if not has_receipt:
            raise HTTPException(409, "The official visa upload receipt is missing")
    await conn.execute("SELECT refresh_practice_visa_followup($1)", practice["id"])


def followup_view(
    row: Any, today: date, *, user: dict | None = None, now: datetime | None = None
) -> dict:
    if user is not None and not is_human_team_member(user.get("role")):
        raise HTTPException(403, "Staff access required for private permit notes")
    started, due = row["started_on"], row["due_on"]
    age = (today - started).days
    final_due = started + timedelta(days=60)
    email = row["email_state"]
    if email == "sending" and row["email_last_attempt_at"] < (now or datetime.now(UTC)) - timedelta(
        minutes=5
    ):
        # A process may have disappeared after provider acceptance. Never
        # manufacture a receipt or resend outside Brevo's dedupe window.
        email = "uncertain"
    if row["fulfilled_at"]:
        status = "fulfilled"
    elif row["owner_release_at"]:
        status = "owner_released"
    elif email == "sent":
        status = "awaiting_owner"
    elif email != "not_sent":
        status = "email_" + email
    elif today >= final_due:
        status = "escalation_due"
    elif today >= due:
        status = "overdue"
    else:
        status = "awaiting_permit"
    open_ = not (row["fulfilled_at"] or row["owner_release_at"])
    return {
        "slot_key": row["slot_key"],
        "started_on": started,
        "due_on": due,
        "final_due_on": final_due,
        "days_remaining": max((due - today).days, 0),
        "status": status,
        "email_state": email,
        "postponed_at": row["postponed_at"],
        "postponement_note": row["postponement_note"],
        "escalation_note": row["escalation_note"],
        "email_sent_at": row["email_sent_at"],
        "email_receipt_confirmed_at": row["email_receipt_confirmed_at"],
        "can_confirm_receipt": bool(
            email == "uncertain"
            and row["email_attempts"] > 0
            and (user or {}).get("email") in OWNER_EMAILS
        ),
        "fulfilled_at": row["fulfilled_at"],
        "owner_release_at": row["owner_release_at"],
        "can_postpone": bool(
            open_ and row["allow_postpone"] and not row["postponed_at"] and 30 <= age < 60
        ),
        "can_escalate": bool(open_ and age >= 60 and email in {"not_sent", "failed"}),
        "can_release": False,
    }


async def get_followup(conn: Any, practice_id: int, *, user: dict | None = None) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM practice_visa_followups WHERE practice_id=$1", practice_id
    )
    return followup_view(row, business_today(), user=user) if row else None


async def _authorized_record(conn: Any, practice_id: int, user: dict) -> Any:
    from backend.services.crm.practice_delivery import practice

    await practice(conn, practice_id, user, lock=True)
    row = await conn.fetchrow(
        "SELECT * FROM practice_visa_followups WHERE practice_id=$1 FOR UPDATE", practice_id
    )
    if row is None:
        raise HTTPException(404, "No post-issuance permit is being tracked for this practice")
    return row


async def postpone(pool: Any, practice_id: int, user: dict, note: str) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        row = await _authorized_record(conn, practice_id, user)
        view = followup_view(row, business_today(), user=user)
        if row["postponed_at"] and row["postponement_note"] == note:
            return view
        if not view["can_postpone"]:
            raise HTTPException(
                409, "The non-entry declaration is available once, from day 30 through day 59"
            )
        result = await conn.fetchrow(
            """UPDATE practice_visa_followups SET due_on=started_on+60,postponed_at=now(),
               postponed_by=$2,postponement_note=$3 WHERE practice_id=$1 RETURNING *""",
            practice_id,
            user["email"],
            note,
        )
        return followup_view(result, business_today(), user=user)


def email_body(row: Any) -> str:
    # User-written content never becomes HTML and is never placed in a log.
    note = escape(row["escalation_note"]).replace("\n", "<br>")
    return (
        "<p>Zero,</p>"
        f"<p>Ti segnalo che per la pratica <strong>#{row['practice_id']}</strong> "
        f"il permesso successivo al visto non risulta caricato entro 60 giorni. "
        f"Il primo caricamento del visto risale al {row['started_on']:%d/%m/%Y}.</p>"
        f"<p>Motivazione ufficiale del dipendente:</p><p>{note}</p>"
        f"<p>Segnalazione presentata da {escape(row['email_requested_by'])}. "
        f'<a href="https://kita.balizero.com/process/{row["practice_id"]}">Apri la pratica in Kita</a>.</p>'
    )


async def escalate(pool: Any, practice_id: int, user: dict, note: str) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        row = await _authorized_record(conn, practice_id, user)
        view = followup_view(row, business_today(), user=user)
        if row["email_sent_at"]:
            return view
        if not view["can_escalate"]:
            raise HTTPException(409, "An official escalation is unavailable or already in progress")
        if row["escalation_note"] and row["escalation_note"] != note:
            raise HTTPException(409, "Retry the original recorded escalation reason")
        claimed = await conn.fetchrow(
            """UPDATE practice_visa_followups SET escalation_note=COALESCE(escalation_note,$3),
               email_state='sending',email_requested_at=COALESCE(email_requested_at,now()),
               email_requested_by=COALESCE(email_requested_by,$2),email_last_attempt_at=now(),
               email_attempts=email_attempts+1 WHERE practice_id=$1 RETURNING *""",
            practice_id,
            user["email"],
            note,
        )
        attempt = claimed["email_attempts"]
        await conn.execute(
            """INSERT INTO practice_visa_followup_send_attempts(practice_id,attempt,requested_by)
               VALUES($1,$2,$3)""",
            practice_id,
            attempt,
            user["email"],
        )
    # The claim is durable before any HTTP call. The keyed internal endpoint
    # sends only through Brevo using the Zantara sender and reports acceptance.
    try:
        accepted = await send_internal_email(
            to=OWNER_MAILBOX,
            subject=f"Pratica #{practice_id}: motivazione ufficiale per permesso mancante",
            body=email_body(claimed),
            idempotency_key=str(claimed["email_idempotency_key"]),
            email_type="visa_permit_escalation",
            practice_id=practice_id,
            raise_on_failure=True,
            log_context=f"visa_permit_escalation:{practice_id}",
        )
        outcome = "sent" if accepted is True else "uncertain"
    except EmailProviderRejected:
        outcome = "failed"
    except httpx.HTTPStatusError as exc:
        outcome = "failed" if exc.response.status_code in {400, 401, 403, 404, 422} else "uncertain"
    except Exception:
        outcome = "uncertain"
    async with pool.acquire() as conn, conn.transaction():
        result = await conn.fetchrow(
            """UPDATE practice_visa_followups SET email_state=$3::text,
               email_sent_at=CASE WHEN $3='sent' THEN now() ELSE NULL END
               WHERE practice_id=$1 AND email_attempts=$2 AND email_state='sending' RETURNING *""",
            practice_id,
            attempt,
            outcome,
        )
        if result is None:
            # The owner may have found the message while the original request
            # was still in flight. Keep that attestation and record this wire
            # response separately on its attempt.
            result = await conn.fetchrow(
                """SELECT * FROM practice_visa_followups WHERE practice_id=$1 AND email_attempts=$2
                   AND email_receipt_confirmed_at IS NOT NULL""",
                practice_id,
                attempt,
            )
            if result is None:
                raise HTTPException(409, "The escalation attempt has already been reconciled")
        await conn.execute(
            """UPDATE practice_visa_followup_send_attempts SET finished_at=now(),outcome=$3
               WHERE practice_id=$1 AND attempt=$2""",
            practice_id,
            attempt,
            outcome,
        )
    if result["email_state"] != "sent":
        detail = (
            "The email provider rejected this send. Retry the recorded reason."
            if outcome == "failed"
            else "Email acceptance is uncertain. Verify the owner mailbox before any further send."
        )
        raise HTTPException(502, detail)
    return followup_view(result, business_today(), user=user)


async def confirm_receipt(pool: Any, practice_id: int, user: dict, reference: str) -> dict:
    """Attest to the already received message without contacting any transport.

    The provider's attempt outcome and the owner's independent receipt are
    both retained. A receipt is deliberately independent of payroll release.
    """
    # Reject non-owners without putting the employee's address in a log.
    if user.get("email") not in OWNER_EMAILS:
        raise HTTPException(403, "Owner access required")
    await require_owner(user)
    async with pool.acquire() as conn, conn.transaction():
        row = await _authorized_record(conn, practice_id, user)
        view = followup_view(row, business_today(), user=user)
        if row["email_receipt_confirmed_at"] and row["email_receipt_reference"] == reference:
            return view
        if not view["can_confirm_receipt"]:
            raise HTTPException(
                409, "Only an uncertain send can be confirmed from the owner mailbox"
            )
        try:
            result = await conn.fetchrow(
                """UPDATE practice_visa_followups SET email_state='sent',email_sent_at=now(),
                   email_receipt_confirmed_at=now(),email_receipt_confirmed_by=$2,email_receipt_reference=$3
                   WHERE practice_id=$1 RETURNING *""",
                practice_id,
                user["email"],
                reference,
            )
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(
                409, "This message reference already confirms another escalation"
            ) from exc
        # If the process disappeared, its actual delivery outcome is unknown.
        # A previously recorded provider response remains untouched.
        await conn.execute(
            """UPDATE practice_visa_followup_send_attempts SET finished_at=now(),outcome='uncertain'
               WHERE practice_id=$1 AND attempt=$2 AND outcome='sending'""",
            practice_id,
            row["email_attempts"],
        )
    return followup_view(result, business_today(), user=user)
