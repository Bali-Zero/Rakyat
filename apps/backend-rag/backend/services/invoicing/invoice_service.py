"""
Invoice Automation Service.

Orchestrates the complete invoice workflow:
1. Generate invoice PDF locally
2. Send invoice via Zoho Email to client
3. Send notification email to Asya (accounting)
4. Upload PDF to Google Drive (backup)
5. Update practice with invoice details
"""

import asyncio
import base64
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import asyncpg
import httpx
from googleapiclient.errors import HttpError as GoogleHttpError

from backend.app.core.config import settings
from backend.app.utils.logging_utils import get_logger
from backend.core.cache import invalidate_crm_stats
from backend.services.integrations.service_account_drive_service import ServiceAccountDriveService
from backend.services.invoicing.accounting import ACCOUNTING_EMAIL
from backend.services.invoicing.client_email_outcome import (
    activity_description,
    client_email_outcome,
)
from backend.services.invoicing.entities import (
    DEFAULT_ENTITY_KEY,
    ENTITIES,
    InvoiceEntity,
    resolve_invoice_entity,
)
from backend.services.invoicing.invoice_company import fetch_bill_to
from backend.services.invoicing.invoice_generator import InvoiceGenerator
from backend.services.invoicing.pph import PphWithholding, pph_from_metadata, pph_on_record
from backend.services.notifications.email_audit import (
    EmailNotDelivered,
    format_send_error,
    log_email_attempt,
    notify_email_failure_critical,
    raise_if_not_delivered,
    record_email_result,
)
from backend.services.notifications.email_branding import team_email_html
from backend.services.notifications.email_http import get_email_client

logger = get_logger(__name__)

# CC on all invoice emails: accounting (Antonello 2026-06-17 — dropped
# zero@ so the owner inbox no longer receives every single invoice;
# accounting owns invoices, the assigned lead is appended separately).
INVOICE_CC_EMAILS = [ACCOUNTING_EMAIL]

# Internal email API (sender is always zantara@balizero.com)
_EMAIL_API_URL = os.getenv(
    "INTERNAL_EMAIL_API_URL",
    "https://nuzantara-rag.fly.dev/api/notifications/send-email",
)
_EMAIL_API_KEY = os.getenv("NUZANTARA_API_KEY", "")

_RESOLUTION_REASONS = frozenset(
    {
        "lookup_failed",
        "issuer_schema_missing",
        "practice_missing",
        "invalid_lookup_result",
        "invalid_routing_timestamp",
        "invalid_practice_type_code",
        "unknown_stored_issuer",
        "incomplete_stored_issuer",
        "invalid_stored_binding",
        "default_entity_invalid",
        "ambiguous_legacy_metadata",
        "orphaned_issuer_metadata",
    }
)
_PERSISTENCE_REASONS = frozenset({"issuer_conflict", "invalid_persisted_binding", "write_failed"})


class InvoiceIssuerResolutionError(RuntimeError):
    def __init__(self, reason: str) -> None:
        if reason not in _RESOLUTION_REASONS:
            raise ValueError("invalid issuer resolution reason")
        self.reason = reason
        super().__init__(reason)


class InvoiceIssuerPersistenceError(RuntimeError):
    def __init__(self, reason: str, sqlstate: str | None = None) -> None:
        if reason not in _PERSISTENCE_REASONS:
            raise ValueError("invalid issuer persistence reason")
        self.reason = reason
        self.sqlstate = (
            sqlstate
            if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate)
            else None
        )
        super().__init__(reason)


@dataclass(frozen=True)
class InvoiceIssuerDecision:
    entity: InvoiceEntity
    routed_at: datetime
    lookup_at: datetime
    source: Literal["new", "stored", "legacy_row", "legacy_documents"]


_INVOICE_UPSERT_SQL = """
INSERT INTO invoices AS existing (
    practice_id, client_id, invoice_number, invoice_source, amount_idr,
    discount_amount_idr, drive_file_id, drive_web_link, email_sent_to_client,
    accounting_notified, generated_at, issuer_entity_key, issuer_routed_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
ON CONFLICT (practice_id) DO UPDATE SET
    invoice_number = EXCLUDED.invoice_number, invoice_source = EXCLUDED.invoice_source,
    amount_idr = EXCLUDED.amount_idr, discount_amount_idr = EXCLUDED.discount_amount_idr,
    drive_file_id = EXCLUDED.drive_file_id, drive_web_link = EXCLUDED.drive_web_link,
    email_sent_to_client = EXCLUDED.email_sent_to_client,
    accounting_notified = EXCLUDED.accounting_notified, generated_at = EXCLUDED.generated_at,
    issuer_entity_key = COALESCE(existing.issuer_entity_key, EXCLUDED.issuer_entity_key),
    issuer_routed_at = CASE WHEN existing.issuer_entity_key IS NULL THEN EXCLUDED.issuer_routed_at ELSE existing.issuer_routed_at END,
    updated_at = NOW()
WHERE (existing.issuer_entity_key IS NULL AND existing.issuer_routed_at IS NULL AND EXCLUDED.issuer_entity_key = $14)
   OR (existing.issuer_entity_key = EXCLUDED.issuer_entity_key AND existing.issuer_routed_at IS NOT NULL)
RETURNING issuer_entity_key, issuer_routed_at
"""

_INVOICE_UPSERT_PRE158_SQL = """
INSERT INTO invoices AS existing (practice_id, client_id, invoice_number, invoice_source, amount_idr,
    drive_file_id, drive_web_link, email_sent_to_client, accounting_notified, generated_at,
    issuer_entity_key, issuer_routed_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
ON CONFLICT (practice_id) DO UPDATE SET
    invoice_number = EXCLUDED.invoice_number, invoice_source = EXCLUDED.invoice_source,
    amount_idr = EXCLUDED.amount_idr, drive_file_id = EXCLUDED.drive_file_id,
    drive_web_link = EXCLUDED.drive_web_link, email_sent_to_client = EXCLUDED.email_sent_to_client,
    accounting_notified = EXCLUDED.accounting_notified, generated_at = EXCLUDED.generated_at,
    issuer_entity_key = COALESCE(existing.issuer_entity_key, EXCLUDED.issuer_entity_key),
    issuer_routed_at = CASE WHEN existing.issuer_entity_key IS NULL THEN EXCLUDED.issuer_routed_at ELSE existing.issuer_routed_at END,
    updated_at = NOW()
WHERE (existing.issuer_entity_key IS NULL AND existing.issuer_routed_at IS NULL AND EXCLUDED.issuer_entity_key = $13)
   OR (existing.issuer_entity_key = EXCLUDED.issuer_entity_key AND existing.issuer_routed_at IS NOT NULL)
RETURNING issuer_entity_key, issuer_routed_at
"""

# K127: _INVOICE_UPSERT_SQL plus invoices.pph_expected_idr ($15, migration 238), in the
# INSERT and in DO UPDATE so a PPh cleared on the process is reset to 0 on re-issue.
# Used only for a process that has or had a PPh (see _persist_invoice); on 42703 the
# write falls back to the two variants above, exactly as a process without PPh.
_INVOICE_UPSERT_PPH_SQL = """
INSERT INTO invoices AS existing (
    practice_id, client_id, invoice_number, invoice_source, amount_idr,
    discount_amount_idr, drive_file_id, drive_web_link, email_sent_to_client,
    accounting_notified, generated_at, issuer_entity_key, issuer_routed_at, pph_expected_idr
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $15)
ON CONFLICT (practice_id) DO UPDATE SET
    invoice_number = EXCLUDED.invoice_number, invoice_source = EXCLUDED.invoice_source,
    amount_idr = EXCLUDED.amount_idr, discount_amount_idr = EXCLUDED.discount_amount_idr,
    pph_expected_idr = EXCLUDED.pph_expected_idr,
    drive_file_id = EXCLUDED.drive_file_id, drive_web_link = EXCLUDED.drive_web_link,
    email_sent_to_client = EXCLUDED.email_sent_to_client,
    accounting_notified = EXCLUDED.accounting_notified, generated_at = EXCLUDED.generated_at,
    issuer_entity_key = COALESCE(existing.issuer_entity_key, EXCLUDED.issuer_entity_key),
    issuer_routed_at = CASE WHEN existing.issuer_entity_key IS NULL THEN EXCLUDED.issuer_routed_at ELSE existing.issuer_routed_at END,
    updated_at = NOW()
WHERE (existing.issuer_entity_key IS NULL AND existing.issuer_routed_at IS NULL AND EXCLUDED.issuer_entity_key = $14)
   OR (existing.issuer_entity_key = EXCLUDED.issuer_entity_key AND existing.issuer_routed_at IS NOT NULL)
RETURNING issuer_entity_key, issuer_routed_at
"""


def _row_pph(practice_row: Any, practice_id: int) -> tuple[PphWithholding | None, bool]:
    """The PPh on a practice row, and whether the process has or ever had one.

    Rows read without ``metadata`` have none. ``metadata.pph_log`` survives a cleared
    PPh, so a re-issue after clearing still resets ``invoices.pph_expected_idr``.
    """
    try:
        metadata = practice_row.get("metadata")
    except (AttributeError, KeyError, TypeError):
        return None, False
    return pph_from_metadata(metadata, practice_id=practice_id), pph_on_record(metadata)


class InvoiceAutomationService:
    """Handles automated invoice generation and distribution via Zoho Email or SMTP fallback."""

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db_pool = db_pool
        self.drive_service = ServiceAccountDriveService()
        self.invoice_generator = InvoiceGenerator()

    async def trigger_on_sending_invoice(
        self,
        practice_id: int,
        triggered_by: str,
    ) -> dict[str, Any]:
        """
        Main trigger function when practice status changes to 'sending_invoice'.

        This function:
        1. Fetches practice and client details
        2. Generates invoice PDF locally
        3. Sends invoice via Zoho Email to client
        4. Sends notification email to Asya (accounting)
        5. Uploads backup PDF to Google Drive
        6. Updates practice with invoice info

        Args:
            practice_id: ID of the practice
            triggered_by: Email of user who triggered the status change

        Returns:
            dict with results of all operations
        """
        logger.info("Invoice automation triggered for practice %s by %s", practice_id, triggered_by)
        invoice_attempt_id = uuid.uuid4().hex
        email_sent = accounting_notified = drive_uploaded = email_attempted = False
        issuer_decision: InvoiceIssuerDecision | None = None

        try:
            # Step 1: Fetch practice and client data
            practice_data = await self._fetch_practice_data(practice_id)
            if not practice_data:
                logger.error("Practice %s not found", practice_id)
                return {"success": False, "error": "Practice not found"}

            client_data = await self._fetch_client_data(practice_data["client_id"])
            if not client_data:
                logger.error(f"Client {practice_data['client_id']} not found")
                return {"success": False, "error": "Client not found"}

            bill_to = await self._fetch_bill_to(practice_data.get("invoice_company_id"))

            issuer_decision = await self._resolve_issuer(
                practice_id, practice_data.get("practice_type_code")
            )
            logger.info(
                "invoice_issuer_selected",
                extra={
                    "context": {
                        "event": "invoice_issuer_selected",
                        "invoice_attempt_id": invoice_attempt_id,
                        "issuer_entity_key": issuer_decision.entity.key,
                        "issuer_routed_at": issuer_decision.routed_at.isoformat(),
                        "source": issuer_decision.source,
                    }
                },
            )

            # Step 2: Generate invoice PDF locally
            logger.info("Generating invoice PDF for practice %s", practice_id)
            try:
                invoice_number = self.invoice_generator.generate_invoice_number(practice_id)

                # Discount fields — added migration 158 (2026-05-06).
                # Optional and default 0/None for legacy rows that predate
                # the columns. The generator handles 0 as "no discount" so
                # the existing visual stays untouched for non-discounted
                # invoices.
                discount_value = practice_data.get("discount_amount")
                discount_amount_float = float(discount_value) if discount_value is not None else 0.0
                discount_reason_value = practice_data.get("discount_reason")
                quoted_price_float = float(practice_data.get("quoted_price", 0))
                # Defensive clamp matches the generator's logic so the email,
                # PDF, and invoices.amount_idr all converge on the same value.
                if discount_amount_float < 0:
                    discount_amount_float = 0.0
                if discount_amount_float > quoted_price_float:
                    discount_amount_float = quoted_price_float
                dpp_float = quoted_price_float - discount_amount_float
                # K127: optional PPh chosen by the tax team (practices.metadata.pph).
                # The client transfers DPP - PPh, so that is TOTAL DUE and the email
                # amount. invoices.amount_idr stays the gross DPP and the PPh goes to
                # invoices.pph_expected_idr (see _persist_invoice).
                pph = pph_from_metadata(practice_data.get("metadata"), practice_id=practice_id)
                pph_amount = pph.amount_on(dpp_float) if pph else 0
                final_amount_float = dpp_float - pph_amount

                pdf_bytes = self.invoice_generator.generate(
                    practice_id=practice_id,
                    client_name=client_data["full_name"],
                    client_email=client_data.get("email"),
                    client_phone=client_data.get("phone"),
                    client_address=client_data.get("address"),
                    practice_type=(
                        practice_data.get("practice_type_name")
                        or practice_data.get("practice_type_code")
                        or "SERVICE"
                    ),
                    practice_description=practice_data.get("notes", "Professional Services"),
                    quoted_price=float(practice_data.get("quoted_price", 0)),
                    discount_amount=discount_amount_float,
                    discount_reason=discount_reason_value,
                    entity=issuer_decision.entity,
                    **bill_to,
                    pph_label=pph.label if pph else None,
                    pph_amount=float(pph_amount),
                )

                filename = f"Invoice_{invoice_number}.pdf"
                logger.info("PDF generated: %s", filename)

            except (OSError, ValueError, KeyError) as pdf_error:
                logger.warning("Failed to generate PDF: %s", pdf_error)
                return {"success": False, "error": f"PDF generation failed: {pdf_error}"}
            except Exception as pdf_error:
                logger.exception("Unexpected error generating PDF for practice %s", practice_id)
                return {"success": False, "error": f"PDF generation failed: {pdf_error}"}

            # Step 3: Send invoice email to client
            # Audit bug #9: failure now persists to email_send_log AND
            # pages the owner via Telegram when the client email is lost —
            # previously AR aging started silently with email_sent=False
            # only visible during Asya's manual ledger review.
            email_sent = False
            email_attempted = bool(client_data.get("email"))
            if email_attempted:
                audit_row_id = await log_email_attempt(
                    self.db_pool,
                    email_type="invoice_client",
                    to_email=client_data["email"],
                    subject=f"Invoice {invoice_number} from Bali Zero",
                    practice_id=practice_id,
                    client_id=client_data["id"],
                )
                try:
                    await self._send_invoice_email_to_client(
                        client_email=client_data["email"],
                        client_name=client_data["full_name"],
                        invoice_number=invoice_number,
                        pdf_bytes=pdf_bytes,
                        filename=filename,
                        amount=final_amount_float,
                        triggered_by=triggered_by,
                        team_member_email=(
                            practice_data.get("assigned_to") or practice_data.get("created_by")
                        ),
                    )
                    email_sent = True
                    await record_email_result(
                        self.db_pool,
                        audit_row_id,
                        status="sent",
                        provider="brevo",
                    )
                    logger.info(f"Invoice email sent to client {client_data['email']}")
                except Exception as email_error:
                    err_msg = format_send_error(email_error)
                    logger.warning(
                        f"Failed to send invoice email to {client_data['email']}: {err_msg}",
                    )
                    await record_email_result(
                        self.db_pool,
                        audit_row_id,
                        status="failed",
                        provider="brevo",
                        error_message=err_msg,
                    )
                    notify_email_failure_critical(
                        email_type="invoice_client",
                        to_email=client_data["email"],
                        subject=f"Invoice {invoice_number}",
                        practice_id=practice_id,
                        error=err_msg,
                    )
            else:
                logger.warning(f"Client {client_data['id']} has no email, skipping email")

            # Step 4: Send notification email to Asya (accounting)
            asya_notified = False
            try:
                await self._send_accounting_notification(
                    client_data=client_data,
                    practice_data=practice_data,
                    invoice_number=invoice_number,
                    pdf_bytes=pdf_bytes,
                    filename=filename,
                    entity=issuer_decision.entity,
                    amount=final_amount_float,
                    client_email_status=client_email_outcome(email_sent, email_attempted),
                )
                asya_notified = True
                accounting_notified = True
                logger.info("Accounting notification sent to %s", ACCOUNTING_EMAIL)
            except httpx.HTTPStatusError as notify_error:
                logger.warning(
                    f"Email API returned {notify_error.response.status_code} "
                    f"sending accounting notification: {notify_error}",
                )
            except httpx.HTTPError as notify_error:
                logger.warning("Failed to notify accounting: %s", notify_error)
            except EmailNotDelivered as notify_error:
                logger.warning(
                    "Accounting notification was not delivered: %s",
                    format_send_error(notify_error),
                )
            except Exception:
                logger.exception("Unexpected error sending accounting notification")

            # Step 5: Upload backup PDF to Google Drive
            logger.info("Uploading invoice backup to Google Drive")
            drive_file_id = None
            drive_web_link = None
            try:
                upload_result = await self.drive_service.upload_file_to_folder(
                    folder_id=practice_data.get("client_drive_folder_id"),
                    file_content=pdf_bytes,
                    file_name=filename,
                    mime_type="application/pdf",
                )
                drive_file_id = upload_result.get("id")
                drive_web_link = upload_result.get("webViewLink")
                drive_uploaded = True
                logger.info("Invoice backup uploaded to Drive: %s", drive_file_id)
            except GoogleHttpError as drive_error:
                logger.warning(
                    f"Google Drive API error uploading invoice backup: "
                    f"{drive_error.status_code} {drive_error.reason}",
                )
            except OSError as drive_error:
                logger.warning("Network error uploading invoice backup to Drive: %s", drive_error)
            except Exception:
                logger.exception("Unexpected error uploading invoice backup to Drive")
                # Continue even if Drive upload fails

            # Step 6: Update practice with invoice details
            invoice_info = {
                "invoice_number": invoice_number,
                "invoice_generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "invoice_drive_id": drive_file_id,
                "invoice_drive_link": drive_web_link,
                "email_attempted": email_attempted,
                "email_sent": email_sent,
                "accounting_notified": asya_notified,
                "source": "local_pdf",
            }
            if pph:
                # Snapshot of the withholding printed on this PDF. invoices has no
                # jsonb column, so it lives with the rest of the invoice in
                # practices.documents.invoice.
                invoice_info["pph"] = {
                    "dpp": round(dpp_float),
                    "pph_type": pph.type,
                    "pph_rate": float(pph.rate_percent),
                    "pph_amount": pph_amount,
                }

            await self._update_practice_with_invoice(
                practice_id=practice_id,
                invoice_info=invoice_info,
                triggered_by=triggered_by,
                issuer_decision=issuer_decision,
                invoice_attempt_id=invoice_attempt_id,
            )

            if email_sent:
                await self._record_invoice_sent_on_client_timeline(
                    practice_data=practice_data,
                    invoice_number=invoice_number,
                    triggered_by=triggered_by,
                )

            logger.info("Invoice automation completed successfully for practice %s", practice_id)

            return {
                "success": True,
                "invoice_number": invoice_number,
                "drive_file_id": drive_file_id,
                "drive_link": drive_web_link,
                "email_attempted": email_attempted,
                "email_sent": email_sent,
                "accounting_notified": asya_notified,
            }

        except InvoiceIssuerResolutionError as error:
            logger.error(
                "invoice_issuer_resolution_failed",
                extra={
                    "context": {
                        "event": "invoice_issuer_resolution_failed",
                        "invoice_attempt_id": invoice_attempt_id,
                        "reason": error.reason,
                        "exception_type": type(error).__name__,
                    }
                },
            )
            return {
                "success": False,
                "error": f"Invoice issuer resolution failed: {error.reason}",
                "error_code": "invoice_issuer_resolution_failed",
            }
        except InvoiceIssuerPersistenceError as error:
            decision = issuer_decision
            context = {
                "event": "invoice_issuer_persistence_failed",
                "invoice_attempt_id": invoice_attempt_id,
                "issuer_entity_key": decision.entity.key if decision else None,
                "issuer_routed_at": decision.routed_at.isoformat() if decision else None,
                "source": decision.source if decision else None,
                "reason": error.reason,
                "exception_type": type(error).__name__,
                "sqlstate": error.sqlstate,
                "email_attempted": email_attempted,
                "email_sent": email_sent,
                "accounting_notified": accounting_notified,
                "drive_uploaded": drive_uploaded,
                "delivery_may_have_occurred": True,
            }
            logger.critical("invoice_issuer_persistence_failed", extra={"context": context})
            return {
                "success": False,
                "error": f"Invoice issuer persistence failed: {error.reason}",
                "error_code": "invoice_issuer_persistence_failed",
                "delivery_may_have_occurred": True,
            }
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as error:
            logger.warning(
                f"Invoice automation failed for practice {practice_id} "
                f"({type(error).__name__}): {error}",
            )
            return {"success": False, "error": str(error)}
        except Exception as error:
            logger.exception("Unexpected error in invoice automation for practice %s", practice_id)
            return {"success": False, "error": str(error)}

    async def _send_invoice_email_to_client(
        self,
        client_email: str,
        client_name: str,
        invoice_number: str,
        pdf_bytes: bytes,
        filename: str,
        amount: float,
        triggered_by: str | None = None,
        team_member_email: str | None = None,
    ) -> bool:
        """Send invoice email to client via internal email API (sender: zantara@balizero.com)."""
        subject = f"Invoice {invoice_number} from Bali Zero"
        # The Bali Zero logo lives on the invoice PDF attachment (top-right,
        # rendered by invoice_generator.py), NOT in the email body — Antonello
        # 2026-06-17: "hai fatto html sul messaggio email, non su allegato".
        body_html = (
            f"<p>Dear {client_name},</p>"
            f"<p>Thank you for choosing Bali Zero for your business in Indonesia.</p>"
            f"<p>Please find your invoice attached to this email.</p>"
            f"<p><b>Invoice Details:</b><br>"
            f"Invoice Number: {invoice_number}<br>"
            f"Amount Due: IDR {amount:,.0f}<br>"
            f"Payment Terms: Net 2 days</p>"
            f"<p>Payment can be made via bank transfer to the details provided on the invoice.</p>"
            f"<p>We look forward to serving you!</p>"
            f"<p>Best regards,<br>Zantara — Bali Zero Team</p>"
            f"<hr><small>For support: {ACCOUNTING_EMAIL} | WhatsApp: {settings.CLIENT_CONTACT_WHATSAPP}</small>"
        )

        cc_emails = list(INVOICE_CC_EMAILS)
        if (
            team_member_email
            and team_member_email not in cc_emails
            and team_member_email != client_email
        ):
            cc_emails.append(team_member_email)
        if triggered_by and triggered_by not in cc_emails and triggered_by != client_email:
            cc_emails.append(triggered_by)

        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        payload: dict[str, Any] = {
            "to": client_email,
            "subject": subject,
            "body": body_html,
            "cc": ", ".join(cc_emails),
            "attachments": [{"name": filename, "content": pdf_b64}],
            # Context for the endpoint's @balizero.com CC hard rule
            # (Antonello 2026-06-17): invoice → asya@ accounting.
            "email_type": "invoice_client",
            "assigned_to": team_member_email,
        }
        client = await get_email_client()
        response = await client.post(
            _EMAIL_API_URL,
            headers={"X-API-Key": _EMAIL_API_KEY},
            json=payload,
        )
        response.raise_for_status()
        # The endpoint answers 200 when every provider failed: its body says so.
        raise_if_not_delivered(response)

        logger.info("Invoice email sent to %s from zantara@ (cc: %s)", client_email, cc_emails)
        return True

    async def _send_accounting_notification(
        self,
        client_data: dict,
        practice_data: dict,
        invoice_number: str,
        pdf_bytes: bytes,
        filename: str,
        entity: InvoiceEntity,
        amount: float,
        client_email_status: str = "sent",
    ) -> bool:
        """Send notification email to accounting (Asya) via internal email API.

        ``client_email_status`` says what really happened to the client's own mail:
        "sent", "failed" (the send raised) or "no_email" (the client has no address).
        The text never claims the client was emailed unless it was.
        """
        subject = f"[TEAM] New Invoice {invoice_number} - {client_data['full_name']}"
        name = client_data["full_name"]
        if client_email_status == "no_email":
            intro = (
                f"Hi Asya — a new invoice has been generated for {name}. The client has NO email "
                "address on file, so the invoice was NOT emailed to them. PDF attached."
            )
            first_step = (
                "Send the invoice to the client yourself: contact them by WhatsApp or another "
                "channel (they were not emailed) and follow up on payment"
            )
        elif client_email_status == "failed":
            intro = (
                f"Hi Asya — a new invoice has been generated for {name}, but the email to the "
                "client FAILED, so they have NOT received it. PDF attached."
            )
            first_step = (
                "Send the invoice to the client yourself by WhatsApp or another channel "
                "(the automatic email failed) and follow up on payment"
            )
        else:
            intro = f"Hi Asya — a new invoice has been generated and sent to {name}. PDF attached."
            first_step = "Reach out to the client via WhatsApp for payment follow-up"
        body_html = team_email_html(
            title=f"New Invoice {invoice_number}",
            intro=intro,
            meta_rows=[
                ("Client", client_data["full_name"]),
                ("Email", client_data.get("email") or "— (no email on file)"),
                ("Practice", f"#{practice_data['id']}"),
                ("Issuing Company", entity.company_name),
                ("Bank Name", entity.bank_name),
                ("Amount", f"IDR {amount:,.0f}"),
            ],
            body_html=(
                "<b>Next steps:</b>"
                "<ol style='margin:6px 0 4px 18px;padding:0;'>"
                f"<li>{first_step}</li>"
                "<li>Keep an eye on our bank account</li>"
                "<li>Once payment comes through, confirm it on the "
                "<a href='https://kita.balizero.com/accounting'>Accounting page</a>, "
                "then move the practice to ON PROCESS</li>"
                "</ol>"
            ),
            cta_label="Open practice in CRM",
            cta_url=f"https://kita.balizero.com/process/{practice_data['id']}",
            signature="Zantara CRM",
        )

        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        payload: dict[str, Any] = {
            "to": ACCOUNTING_EMAIL,
            "subject": subject,
            "body": body_html,
            "attachments": [{"name": filename, "content": pdf_b64}],
        }
        client = await get_email_client()
        response = await client.post(
            _EMAIL_API_URL,
            headers={"X-API-Key": _EMAIL_API_KEY},
            json=payload,
        )
        response.raise_for_status()
        raise_if_not_delivered(response)

        logger.info("Accounting notification sent to %s from zantara@", ACCOUNTING_EMAIL)
        return True

    async def _record_invoice_sent_on_client_timeline(
        self, practice_data: dict, invoice_number: str, triggered_by: str | None
    ) -> None:
        """Client-visible "Invoice sent" entry, written ONLY after the client mail really went out.

        The status move writes "Invoice being prepared" before this automation runs; on no_email
        or failed nothing client-visible is added (the team and accounting notices cover those).
        Best effort: a missing table or a write error never fails the invoice flow.
        """
        if practice_data.get("client_visible") is False or practice_data.get("client_id") is None:
            return
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO timeline_events (
                        client_id, practice_id, event_type, title, description,
                        event_date, client_visible, color, created_by
                    )
                    VALUES ($1, $2, 'payment_due', 'Invoice sent', $3, NOW(), true, 'warning', $4)
                    """,
                    practice_data["client_id"],
                    practice_data["id"],
                    f"Invoice {invoice_number}",
                    triggered_by,
                )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "42P01":
                logger.warning(
                    "Could not write the invoice-sent timeline entry for practice %s: %s",
                    practice_data.get("id"),
                    type(exc).__name__,
                )

    async def _fetch_practice_data(self, practice_id: int) -> dict | None:
        """Fetch practice data from database."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    p.*,
                    pt.code as practice_type_code,
                    pt.name as practice_type_name,
                    c.google_drive_folder_id as client_drive_folder_id
                FROM practices p
                LEFT JOIN practice_types pt ON p.practice_type_id = pt.id
                LEFT JOIN clients c ON p.client_id = c.id
                WHERE p.id = $1
                """,
                practice_id,
            )
            return dict(row) if row else None

    async def _resolve_issuer(
        self, practice_id: int, practice_type_code: str | None
    ) -> InvoiceIssuerDecision:
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT NOW() AS db_now, p.id IS NOT NULL AS practice_exists,
                    i.practice_id IS NOT NULL AS invoice_row_exists, i.issuer_entity_key,
                    i.issuer_routed_at, p.documents AS practice_documents
                    FROM (SELECT $1::integer AS practice_id) AS requested
                    LEFT JOIN public.practices AS p ON p.id = requested.practice_id
                    LEFT JOIN public.invoices AS i ON i.practice_id = requested.practice_id""",
                    practice_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = getattr(exc, "sqlstate", None)
            raise InvoiceIssuerResolutionError(
                "issuer_schema_missing" if state == "42703" else "lookup_failed"
            ) from None
        if row is None:
            raise InvoiceIssuerResolutionError("invalid_lookup_result")
        data = dict(row)
        if data.get("practice_exists") is not True:
            raise InvoiceIssuerResolutionError("practice_missing")
        lookup_at = data.get("db_now")
        if (
            not isinstance(lookup_at, datetime)
            or lookup_at.tzinfo is None
            or lookup_at.utcoffset() is None
        ):
            raise InvoiceIssuerResolutionError("invalid_routing_timestamp")
        row_exists = data.get("invoice_row_exists")
        if not isinstance(row_exists, bool):
            raise InvoiceIssuerResolutionError("invalid_lookup_result")
        key, routed_at = data.get("issuer_entity_key"), data.get("issuer_routed_at")
        if row_exists:
            if key is None and routed_at is None:
                entity = ENTITIES.get(DEFAULT_ENTITY_KEY)
                if entity is None or entity.missing_fields():
                    raise InvoiceIssuerResolutionError("default_entity_invalid")
                return InvoiceIssuerDecision(entity, lookup_at, lookup_at, "legacy_row")
            if not isinstance(key, str) or not key.strip() or routed_at is None:
                raise InvoiceIssuerResolutionError("invalid_stored_binding")
            if (
                not isinstance(routed_at, datetime)
                or routed_at.tzinfo is None
                or routed_at.utcoffset() is None
            ):
                raise InvoiceIssuerResolutionError("invalid_stored_binding")
            entity = ENTITIES.get(key)
            if entity is None:
                raise InvoiceIssuerResolutionError("unknown_stored_issuer")
            if entity.missing_fields():
                raise InvoiceIssuerResolutionError("incomplete_stored_issuer")
            return InvoiceIssuerDecision(entity, routed_at, lookup_at, "stored")
        marker = self._legacy_invoice_marker(data.get("practice_documents"))
        if marker == "orphaned":
            raise InvoiceIssuerResolutionError("orphaned_issuer_metadata")
        if marker == "legacy":
            entity = ENTITIES.get(DEFAULT_ENTITY_KEY)
            if entity is None or entity.missing_fields():
                raise InvoiceIssuerResolutionError("default_entity_invalid")
            return InvoiceIssuerDecision(entity, lookup_at, lookup_at, "legacy_documents")
        try:
            entity = resolve_invoice_entity(practice_type_code, lookup_at)
        except TypeError:
            raise InvoiceIssuerResolutionError("invalid_practice_type_code") from None
        except (ValueError, KeyError):
            raise InvoiceIssuerResolutionError("default_entity_invalid") from None
        return InvoiceIssuerDecision(entity, lookup_at, lookup_at, "new")

    @staticmethod
    def _legacy_invoice_marker(documents: Any) -> Literal["none", "legacy", "orphaned"]:
        value = documents
        try:
            for _ in range(2):
                if isinstance(value, str):
                    value = json.loads(value)
                else:
                    break
        except (TypeError, json.JSONDecodeError):
            raise InvoiceIssuerResolutionError("ambiguous_legacy_metadata") from None
        # practices.documents defaults to '[]' (an empty JSON array) for every new practice:
        # an empty array is "no documents yet", same as NULL. Anything else that is not an
        # object stays ambiguous and fails closed.
        if value is None or (isinstance(value, list) and not value):
            return "none"
        if not isinstance(value, dict):
            raise InvoiceIssuerResolutionError("ambiguous_legacy_metadata")
        if not value or "invoice" not in value:
            return "none"
        invoice = value["invoice"]
        if invoice is None:
            return "legacy"
        if not isinstance(invoice, dict):
            raise InvoiceIssuerResolutionError("ambiguous_legacy_metadata")
        if "issuer_entity_key" in invoice or "issuer_routed_at" in invoice:
            return "orphaned"
        return "legacy"

    async def _fetch_bill_to(self, company_id: int | None) -> dict[str, str | None]:
        if company_id is None:
            return {}
        async with self.db_pool.acquire() as conn:
            return await fetch_bill_to(conn, company_id) or {}

    async def _fetch_client_data(self, client_id: int) -> dict | None:
        """Fetch client data from database."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, full_name, email, phone, address, nationality
                FROM clients
                WHERE id = $1
                """,
                client_id,
            )
            return dict(row) if row else None

    async def _update_practice_with_invoice(
        self,
        practice_id: int,
        invoice_info: dict,
        triggered_by: str,
        *,
        issuer_decision: InvoiceIssuerDecision,
        invoice_attempt_id: str,
    ) -> None:
        """Persist the invoice; any database failure is a loud persistence error."""
        try:
            await self._persist_invoice(
                practice_id,
                invoice_info,
                triggered_by,
                issuer_decision=issuer_decision,
                invoice_attempt_id=invoice_attempt_id,
            )
        except (asyncio.CancelledError, InvoiceIssuerPersistenceError):
            raise
        except Exception as exc:
            raise InvoiceIssuerPersistenceError(
                "write_failed", getattr(exc, "sqlstate", None)
            ) from None

    async def _persist_invoice(
        self,
        practice_id: int,
        invoice_info: dict,
        triggered_by: str,
        *,
        issuer_decision: InvoiceIssuerDecision,
        invoice_attempt_id: str,
    ) -> None:
        """Update practice with invoice information (dual-write: JSONB + invoices table)."""
        async with self.db_pool.acquire() as conn:
            # ── 1. Fetch practice for amount + client_id + discount ───────────
            # Defensive try/except keeps the dual-write working on databases
            # that have not yet applied migration 158.
            try:
                practice_row = await conn.fetchrow(
                    "SELECT client_id, quoted_price, discount_amount, metadata"
                    " FROM practices WHERE id = $1",
                    practice_id,
                )
            except Exception as fetch_exc:
                if getattr(fetch_exc, "sqlstate", None) == "42703":
                    practice_row = await conn.fetchrow(
                        "SELECT client_id, quoted_price, metadata FROM practices WHERE id = $1",
                        practice_id,
                    )
                else:
                    raise

            if not practice_row:
                raise InvoiceIssuerPersistenceError("write_failed")
            generated_at = None
            if invoice_info.get("invoice_generated_at"):
                try:
                    generated_at = datetime.fromisoformat(invoice_info["invoice_generated_at"])
                except (TypeError, ValueError):
                    pass
            quoted = float(practice_row["quoted_price"] or 0)
            try:
                discount = float(practice_row.get("discount_amount") or 0)
            except (AttributeError, KeyError, TypeError):
                discount = 0.0
            discount = max(0.0, min(discount, quoted))
            # amount_idr stores the post-discount total (DPP): gross, with or without
            # PPh. discount_amount_idr mirrors the discount. A PPh the client withholds
            # (K127) goes to pph_expected_idr (migration 238), the accounting contract:
            # the matcher reads a transfer of amount_idr - pph_expected_idr as "net of
            # PPh" (services/accounting/matcher.py _amount_signals) and outstanding is
            # amount_idr - pph_expected_idr - paid. TOTAL DUE on the PDF is that net.
            pph, pph_tracked = _row_pph(practice_row, practice_id)
            pph_expected = pph.amount_on(quoted - discount) if pph else 0
            args = (
                practice_id,
                practice_row["client_id"],
                invoice_info.get("invoice_number") or None,
                invoice_info.get("source", "local_pdf"),
                quoted - discount,
                discount,
                invoice_info.get("invoice_drive_id") or None,
                invoice_info.get("invoice_drive_link") or None,
                bool(invoice_info.get("email_sent", False)),
                bool(invoice_info.get("accounting_notified", False)),
                generated_at,
                issuer_decision.entity.key,
                issuer_decision.routed_at,
                DEFAULT_ENTITY_KEY,
            )
            pph_written = False
            if pph_tracked:
                # A process that has (or had) a PPh also writes pph_expected_idr. A
                # process that never had one keeps the write below unchanged, so its
                # pph_expected_idr stays the column default 0.
                try:
                    persisted = await conn.fetchrow(_INVOICE_UPSERT_PPH_SQL, *args, pph_expected)
                    pph_written = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if getattr(exc, "sqlstate", None) != "42703":
                        raise InvoiceIssuerPersistenceError(
                            "write_failed", getattr(exc, "sqlstate", None)
                        ) from None
                    logger.warning(
                        "invoices.pph_expected_idr missing (migration 238): practice %s "
                        "invoiced without its PPh of %s IDR recorded",
                        practice_id,
                        pph_expected,
                    )
            if not pph_written:
                try:
                    persisted = await conn.fetchrow(_INVOICE_UPSERT_SQL, *args)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if getattr(exc, "sqlstate", None) != "42703":
                        raise InvoiceIssuerPersistenceError(
                            "write_failed", getattr(exc, "sqlstate", None)
                        ) from None
                    # Fallback for databases without migration 158 (discount_amount_idr).
                    pre158_args = (*args[:5], *args[6:])
                    try:
                        persisted = await conn.fetchrow(_INVOICE_UPSERT_PRE158_SQL, *pre158_args)
                    except asyncio.CancelledError:
                        raise
                    except Exception as fallback_exc:
                        raise InvoiceIssuerPersistenceError(
                            "write_failed", getattr(fallback_exc, "sqlstate", None)
                        ) from None
            if persisted is None or persisted["issuer_entity_key"] != issuer_decision.entity.key:
                raise InvoiceIssuerPersistenceError("issuer_conflict")
            persisted_at = persisted["issuer_routed_at"]
            if (
                not isinstance(persisted_at, datetime)
                or persisted_at.tzinfo is None
                or persisted_at.utcoffset() is None
            ):
                raise InvoiceIssuerPersistenceError("invalid_persisted_binding")

            # ── 2. JSONB write (backward compat — kept until Sprint 3 cleanup) ─
            existing_docs = await conn.fetchval(
                "SELECT documents FROM practices WHERE id = $1",
                practice_id,
            )

            documents: dict = {}
            if existing_docs:
                if isinstance(existing_docs, str):
                    try:
                        docs_str = existing_docs.strip()
                        if docs_str:
                            parsed = json.loads(docs_str)
                            if isinstance(parsed, str):
                                parsed = json.loads(parsed)
                            documents = parsed if isinstance(parsed, dict) else {}
                    except (json.JSONDecodeError, TypeError):
                        documents = {}
                elif isinstance(existing_docs, dict):
                    documents = existing_docs

            documents["invoice"] = invoice_info
            documents["invoice"]["issuer_entity_key"] = persisted["issuer_entity_key"]
            documents["invoice"]["issuer_routed_at"] = persisted_at.isoformat()

            # payment_status is NOT reset here: FE ciclo is unpaid→partial→paid
            # (no 'pending' state). Keep whatever value was set before.
            # Pass dict directly — the api pool's jsonb codec encodes once
            # via json.dumps. Double-encoding (calling json.dumps here AND
            # letting the codec re-encode) was the root cause of the
            # discount_log audit trail landing as a JSONB string scalar in
            # production (observed 2026-05-06 on practice_id=390).
            await conn.execute(
                """
                UPDATE practices
                SET documents = $1, updated_at = NOW()
                WHERE id = $2
                """,
                documents,
                practice_id,
            )
            await invalidate_crm_stats()  # F32

            # ── 4. Activity log ───────────────────────────────────────────────
            await conn.execute(
                """
                INSERT INTO activity_log (
                    entity_type, entity_id, action, performed_by, description
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                "practice",
                practice_id,
                "invoice_generated",
                triggered_by,
                activity_description(
                    invoice_info["invoice_number"],
                    client_email_outcome(
                        bool(invoice_info.get("email_sent")),
                        bool(invoice_info.get("email_attempted")),
                    ),
                ),
            )

        logger.info("Practice %s updated with invoice information", practice_id)

    async def regenerate_invoice(
        self,
        practice_id: int,
        triggered_by: str,
    ) -> dict[str, Any]:
        """
        Manually regenerate and resend invoice for a practice.

        Useful if:
        - Client lost the invoice email
        - Need to send updated version
        - Previous automation failed
        """
        logger.info("Manual invoice regeneration requested for practice %s", practice_id)
        return await self.trigger_on_sending_invoice(practice_id, triggered_by)
