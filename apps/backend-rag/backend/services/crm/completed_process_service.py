"""
Completed Process Automation Service.

Handles automation when a practice status changes to 'completed':
1. Record the completion in ``activity_log`` (action ``process_completed``), always.
2. Notify the team member ONLY when they did not already get the client's milestone email.

The client's own completion email is NOT sent here: ``PracticeStatusListener`` sends the
single client-facing "Completed ... Documents Are Ready" mail (cc: the assigned team member)
and skips it when the client has no email. A second client mail from this service would be a
duplicate, so it was removed (owner decision 2026-11-12).
"""

import os
from typing import Any

import asyncpg

from backend.app.utils.logging_utils import get_logger
from backend.security.pii_log_identifier import redact_identifier_for_log
from backend.services.common.cache import cache_invalidating
from backend.services.notifications.email_audit import (
    _bounded_scrub,
    format_send_error,
    log_email_attempt,
    notify_email_failure_critical,
    raise_if_not_delivered,
    record_email_result,
)
from backend.services.notifications.email_branding import logo_header_html, team_email_html
from backend.services.notifications.email_http import get_email_client

# Internal email API — uses Brevo, from=zantara@balizero.com
_EMAIL_API_URL = os.getenv(
    "INTERNAL_EMAIL_API_URL",
    "https://nuzantara-rag.fly.dev/api/notifications/send-email",
)
_EMAIL_API_KEY = os.getenv("NUZANTARA_API_KEY", "")

logger = get_logger(__name__)

_REASON_NO_CLIENT_EMAIL = "no_client_email"
_REASON_NO_ASSIGNEE = "no_assignee"


def team_recipient(practice_data: dict, client_email: str | None) -> tuple[str | None, str | None]:
    """Who needs a "[TEAM] Process Completed" mail, and why.

    The listener's client mail already cc's ``assigned_to`` when the client has an email and
    ``assigned_to`` is somebody else. Only when that cc did not happen does the team member get
    this mail: the client has no email (listener skipped entirely) or nobody is assigned (the
    listener mail had no cc). Recipient: ``assigned_to``, else ``created_by``; never the client's
    own address. Returns ``(email, reason)``, or ``(None, None)`` when the team already knows.
    """
    assigned_to = practice_data.get("assigned_to") or None
    if client_email and assigned_to and assigned_to != client_email:
        return None, None
    reason = _REASON_NO_CLIENT_EMAIL if not client_email else _REASON_NO_ASSIGNEE
    for candidate in (assigned_to, practice_data.get("created_by")):
        if candidate and candidate != client_email:
            return candidate, reason
    return None, reason


class CompletedProcessService:
    """Handles automation when process is completed."""

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db_pool = db_pool

    @cache_invalidating(
        [
            lambda self, practice_id, *a, **k: f"zantara:crm_practice:{practice_id}:*",
            "zantara:crm_practices:*",
            "zantara:crm_documents:*",
        ]
    )
    async def trigger_on_completed(
        self,
        practice_id: int,
        triggered_by: str,
    ) -> dict[str, Any]:
        """
        Trigger when practice status changes to 'completed'.

        Args:
            practice_id: ID of the practice
            triggered_by: Email of user who triggered the status change

        Returns:
            dict with the result of the operations
        """
        logger.info("Process completion automation triggered for practice %s", practice_id)

        try:
            practice_data = await self._fetch_practice_data(practice_id)
            if not practice_data:
                return {"success": False, "error": "Practice not found"}

            client_data = await self._fetch_client_data(practice_data["client_id"])
            client_email = (client_data or {}).get("email") or None
            client_name = (client_data or {}).get(
                "full_name"
            ) or f"Client #{practice_data['client_id']}"

            # The completion is recorded whatever happens to the mail below.
            await self._log_activity(
                practice_id=practice_id,
                triggered_by=triggered_by,
                description="Process completed",
            )

            recipient, reason = team_recipient(practice_data, client_email)
            team_notified = False
            if recipient and reason:
                try:
                    await self._send_team_leader_completion_notification(
                        team_leader_email=recipient,
                        client_name=client_name,
                        practice_data=practice_data,
                        reason=reason,
                    )
                    team_notified = True
                except Exception as e:
                    logger.error("Failed to notify team leader: %s", _bounded_scrub(str(e), 400))

            return {"success": True, "team_notified": team_notified}

        except Exception as error:
            # No exc_info: the formatter would re-emit the raw exception message (a provider or
            # driver error can echo an address). The scrubbed text and the type are enough.
            logger.error(
                "Process completion automation failed for practice %s (%s): %s",
                practice_id,
                type(error).__name__,
                _bounded_scrub(str(error), 400),
            )
            return {"success": False, "error": _bounded_scrub(str(error), 400)}

    async def _send_team_leader_completion_notification(
        self,
        team_leader_email: str,
        client_name: str,
        practice_data: dict,
        reason: str,
    ) -> None:
        """Tell a team member the process is completed and why they got this mail.

        Sent only when the client's milestone email did not reach them (see
        :func:`team_recipient`), so ``reason`` says what to do instead.
        """
        service_name = practice_data.get("practice_type_name") or "Service"
        practice_id = practice_data["id"]

        if reason == _REASON_NO_CLIENT_EMAIL:
            intro = (
                "The client has no email on file, so the automatic completion email did not "
                "go out. Please let the client know and share the documents directly."
            )
        else:
            intro = (
                "No team member is assigned to this process, so nobody was copied on the "
                "client's completion email. Please follow up with the client if needed."
            )

        subject = f"[TEAM] ✅ Process Completed - {client_name} ({service_name})"
        body = team_email_html(
            title=f"✅ Process Completed — {client_name}",
            intro=intro,
            meta_rows=[
                ("Client", client_name),
                ("Service", service_name),
                ("Practice ID", f"#{practice_id}"),
                ("Status", "✅ COMPLETED"),
            ],
            body_html="",
            cta_label="Open practice in CRM",
            cta_url=f"https://kita.balizero.com/process/{practice_id}",
            signature="Zantara CRM",
        )

        await self._send_with_brevo_fallback(
            team_leader_email,
            subject,
            body,
            email_type="completion_team",
            practice_id=practice_id,
            client_id=practice_data.get("client_id"),
            prebuilt_html=True,
        )

    async def _send_with_brevo_fallback(
        self,
        to_email: str,
        subject: str,
        body: str,
        *,
        email_type: str,
        practice_id: int | None = None,
        client_id: int | None = None,
        cc: str | None = None,
        include_logo: bool = False,
        prebuilt_html: bool = False,
    ) -> None:
        """Send email via Brevo. No provider fallback (2026-09-27, removed).

        A Zoho fallback used to sit here but called
        ``ZohoEmailService.send_email`` with the wrong keyword arguments
        (``to_email``/``subject``/``body`` vs. the real
        ``user_id``/``to``/``subject``/``content``) since the call site was
        first written 2026-02-22 — Zoho was never per-tenant/system mail
        anyway, only a staff member's own OAuth-connected mailbox, which
        contradicts the house rule that CRM mail goes out as
        ``from=zantara@balizero.com`` via Brevo. Every attempt is audited to
        ``email_send_log``; a Brevo failure pages the owner immediately via
        :func:`notify_email_failure_critical` and is re-raised so the
        caller's ``*_notified`` flag reflects reality.
        """
        row_id = await log_email_attempt(
            self.db_pool,
            email_type=email_type,
            to_email=to_email,
            subject=subject,
            practice_id=practice_id,
            client_id=client_id,
        )

        # 1) Brevo
        try:
            if prebuilt_html:
                html_body = body
            else:
                html_body = body.replace("\n", "<br>")
                if include_logo:
                    html_body = f"{logo_header_html()}{html_body}"
            payload: dict = {"to": to_email, "subject": subject, "body": html_body}
            if cc:
                payload["cc"] = cc
            client = await get_email_client()
            response = await client.post(
                _EMAIL_API_URL,
                headers={"X-API-Key": _EMAIL_API_KEY},
                json=payload,
            )
            response.raise_for_status()
            raise_if_not_delivered(response)
            logger.info("Email sent to %s via Brevo", redact_identifier_for_log(to_email))
            await record_email_result(
                self.db_pool,
                row_id,
                status="sent",
                provider="brevo",
            )
            return
        except Exception as brevo_error:
            # C2 (PR #7385 gate follow-up): this used to log the raw
            # exception object (`%s` on `brevo_error` calls `str()` on it,
            # and a provider bounce can echo the address back) BEFORE
            # `format_send_error` even ran. Format first, scrub, then log.
            brevo_err_msg = format_send_error(brevo_error)
            logger.error(
                "Brevo failed for %s, no fallback provider: %s",
                redact_identifier_for_log(to_email),
                _bounded_scrub(brevo_err_msg, 400),
            )
            await record_email_result(
                self.db_pool,
                row_id,
                status="failed",
                provider="brevo",
                error_message=brevo_err_msg,
            )
            notify_email_failure_critical(
                email_type=email_type,
                to_email=to_email,
                subject=subject,
                practice_id=practice_id,
                error=brevo_err_msg,
            )
            raise

    async def _fetch_practice_data(self, practice_id: int) -> dict | None:
        """Fetch practice data from database."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    p.*,
                    pt.code as practice_type_code,
                    pt.name as practice_type_name
                FROM practices p
                LEFT JOIN practice_types pt ON p.practice_type_id = pt.id
                WHERE p.id = $1
                """,
                practice_id,
            )
            return dict(row) if row else None

    async def _fetch_client_data(self, client_id: int) -> dict | None:
        """Fetch client data from database."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id, full_name, email, phone,
                    drive_folder_id, drive_folder_url
                FROM clients
                WHERE id = $1
                """,
                client_id,
            )
            return dict(row) if row else None

    async def _log_activity(
        self,
        practice_id: int,
        triggered_by: str,
        description: str,
    ) -> None:
        """Log activity to activity_log table."""
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO activity_log (
                    entity_type, entity_id, action, performed_by, description
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                "practice",
                practice_id,
                "process_completed",
                triggered_by,
                description,
            )
