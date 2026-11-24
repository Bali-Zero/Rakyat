"""
Portal billing + profile mixin.

Grouped together because both touch read-only views of client data (invoices,
profile) with no cross-dependency on other portal mixins. Sensitive profile
fields are whitelisted here (PROFILE_EDITABLE_FIELDS).
"""

from typing import Any

import asyncpg

from backend.app.services.crm.audit_logger import audit_logger
from backend.app.utils.logging_utils import get_logger
from backend.phone_lock import phone_core
from backend.services.common.cache import cache_invalidating
from backend.services.portal._drive_fetch import fetch_drive_file
from backend.services.portal._rbac import ClientContext, require_client_access
from backend.services.portal.gate_upload_adapter import GateUploadClient

logger = get_logger(__name__)

PROFILE_CONTACT_REFUSED_MESSAGE = (
    "This phone number cannot be saved on your profile. Please contact your consultant."
)

_CONTACT_LABELS = {"phone": "phone", "whatsapp": "WhatsApp"}
_CONTACT_VALUE_MAX = 40


class ProfileContactRefused(Exception):
    """A self-service phone/whatsapp value with no usable number was refused.

    Format only: the refusal never depends on who else holds a number.
    """

    def __init__(self) -> None:
        super().__init__(PROFILE_CONTACT_REFUSED_MESSAGE)


def _refuse_unencodable(value: str) -> None:
    """A value that cannot be encoded as UTF-8 (a lone surrogate) cannot be stored."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ProfileContactRefused() from None


class PortalBillingMixin:
    """Client billing (invoices) and profile update."""

    pool: asyncpg.Pool

    # Whitelisted profile fields a client can self-serve update via the portal.
    # Anything else requires team intervention.
    PROFILE_EDITABLE_FIELDS: set[str] = {"phone", "whatsapp", "address"}
    # Contact numbers are ownership keys: a client never writes them directly,
    # a changed value becomes a change request for the consultant.
    PROFILE_CONTACT_FIELDS: tuple[str, ...] = ("phone", "whatsapp")

    # ================================================
    # BILLING
    # ================================================

    @require_client_access
    async def get_billing(
        self,
        client_id: int,
        *,
        current_user: ClientContext,
    ) -> dict[str, Any]:
        """
        Get all invoices for a client with summary statistics.

        Reads from the `invoices` table joined with practices for context.
        Database failures propagate to the router so clients never see a false
        empty-account state.

        Returns format expected by frontend BillingResponse type:
            - invoices: list of invoice dicts
            - summary: {total_invoiced, total_paid, total_pending, count}
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    i.id,
                    i.invoice_number,
                    i.amount_idr,
                    i.invoice_source,
                    i.drive_file_id,
                    i.drive_web_link,
                    i.email_sent_to_client,
                    i.generated_at,
                    i.created_at,
                    i.practice_id,
                    pt.name AS practice_name,
                    pt.category AS practice_category,
                    i.paid_amount_idr,
                    COALESCE((to_jsonb(i) ->> 'pph_expected_idr')::numeric, 0)
                        AS pph_expected_idr,
                    p.payment_status,
                    p.paid_amount,
                    p.quoted_price
                FROM invoices i
                JOIN practices p ON p.id = i.practice_id
                JOIN practice_types pt ON pt.id = p.practice_type_id
                WHERE i.client_id = $1
                ORDER BY i.created_at DESC
                """,
                client_id,
            )

        invoices = []
        total_invoiced = 0.0

        # Money received is tracked on the PRACTICE (`practices.paid_amount`,
        # incremented by the operator's reconciliation in the same transaction
        # that moves payment_status). Crediting `amount_idr` on status == "paid"
        # — as this did until 2026-08-20 — cannot see a partial payment: the
        # operator's lifecycle is unpaid -> partial -> paid, and every invoice
        # under a partial practice contributed 0 to Paid and its full amount to
        # Outstanding. A client who had paid half was shown the whole bill.
        #
        # paid_amount is per PRACTICE while this list is per INVOICE, and a
        # practice can carry several invoices (the unique constraint was
        # dropped), so credit each practice ONCE. It is capped at what that
        # practice was actually invoiced: these three figures answer "of what we
        # billed you, how much is settled", so a payment running ahead of
        # invoicing must not make Outstanding negative. Nothing is hidden by the
        # cap — the raw per-invoice figure is returned as `paid_amount_idr`.
        invoiced_by_practice: dict[int, float] = {}
        paid_by_practice: dict[int, float] = {}

        for row in rows:
            # What the client owes on this invoice: invoices.amount_idr is the gross
            # DPP; a client that withholds PPh (K127, invoices.pph_expected_idr) owes
            # and transfers amount_idr - PPh, the TOTAL DUE on its PDF. Without PPh
            # this is amount_idr, as before. (to_jsonb in the SELECT reads 0 when
            # the column is absent.)
            amount = float(row["amount_idr"] or 0) - float(row.get("pph_expected_idr") or 0)
            # "unpaid", not "pending": pending is not a state this system emits.
            payment_status = row["payment_status"] or "unpaid"
            total_invoiced += amount

            practice_id = row["practice_id"]
            invoiced_by_practice[practice_id] = invoiced_by_practice.get(practice_id, 0.0) + amount
            paid_by_practice[practice_id] = float(row["paid_amount"] or 0)

            invoices.append(
                {
                    "id": row["id"],
                    "invoice_number": row["invoice_number"],
                    "amount_idr": amount,
                    "invoice_source": row["invoice_source"],
                    "has_pdf": bool(row["drive_file_id"]),
                    "drive_web_link": None,
                    "email_sent": bool(row["email_sent_to_client"]),
                    "generated_at": row["generated_at"].isoformat()
                    if row["generated_at"]
                    else None,
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "practice_id": row["practice_id"],
                    "practice_name": row["practice_name"],
                    "practice_category": row["practice_category"],
                    "payment_status": payment_status,
                    "paid_amount_idr": float(row["paid_amount_idr"])
                    if row["paid_amount_idr"] is not None
                    else None,
                }
            )

        total_paid = sum(
            min(paid, invoiced_by_practice[practice_id])
            for practice_id, paid in paid_by_practice.items()
        )

        return {
            "invoices": invoices,
            "summary": {
                "total_invoiced": total_invoiced,
                "total_paid": total_paid,
                # Non-negative by construction: every practice's credit is
                # capped at what that same practice was invoiced.
                "total_pending": total_invoiced - total_paid,
                "count": len(invoices),
            },
        }

    @require_client_access
    async def get_invoice_pdf_url(
        self,
        client_id: int,
        invoice_id: int,
        *,
        current_user: ClientContext,
    ) -> dict[str, str] | None:
        """Get the portal proxy URL for an owned invoice PDF."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT drive_web_link, drive_file_id FROM invoices WHERE id = $1 AND client_id = $2",
                invoice_id,
                client_id,
            )

        if not row or (not row["drive_web_link"] and not row["drive_file_id"]):
            return None

        return {"download_url": f"/api/portal/billing/{invoice_id}/pdf"}

    @require_client_access
    async def download_invoice_pdf(
        self,
        client_id: int,
        invoice_id: int,
        *,
        current_user: ClientContext,
    ) -> dict[str, Any] | None:
        """Download an invoice PDF through the portal proxy."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT invoice_number, drive_web_link, drive_file_id
                FROM invoices
                WHERE id = $1 AND client_id = $2
                """,
                invoice_id,
                client_id,
            )

        if not row or (not row["drive_web_link"] and not row["drive_file_id"]):
            return None

        extractor = getattr(self, "_extract_drive_file_id", None)
        file_id = row["drive_file_id"] or (extractor(row["drive_web_link"]) if extractor else None)
        if not file_id:
            return None

        qa_sink = GateUploadClient.from_environment()
        if qa_sink is not None:
            content = await qa_sink.download(file_id)
            if content is None:
                return None
            return {
                "content": content,
                "file_name": f"{row['invoice_number']}.pdf",
                "mime_type": "application/pdf",
            }

        drive_file = await fetch_drive_file(
            file_id,
            fallback_file_name=f"{row['invoice_number']}.pdf",
            fallback_mime_type="application/pdf",
            what="invoice",
        )
        if drive_file is None:
            return None

        return {
            "content": drive_file.content,
            "file_name": drive_file.file_name,
            "mime_type": drive_file.mime_type,
        }

    # ================================================
    # PROFILE UPDATE
    # ================================================

    @cache_invalidating(
        [
            lambda _self, client_id, *_a, **_k: f"zantara:portal_profile:{client_id}:*",
            lambda _self, client_id, *_a, **_k: f"zantara:crm_client:{client_id}:*",
            "zantara:portal_messages:*",
        ]
    )
    @require_client_access
    async def update_profile(
        self,
        client_id: int,
        fields: dict[str, Any],
        *,
        current_user: ClientContext,
    ) -> dict[str, Any]:
        """
        Update client profile with whitelisted fields only.

        Sensitive fields (full_name, email, passport_number, nationality, etc.)
        are silently ignored — they require team intervention. phone/whatsapp
        are never written here: a changed usable number is recorded as a
        client_to_team change request (see _request_contact_changes), and the
        returned profile carries ``contact_change_requested`` (field names).

        Side effects when fields are actually changed:
        - Inserts a notification_alerts record (portal_profile_update) for the CRM bell
        - Inserts a portal_messages record (client_to_team) so the team sees the change
        - Inserts a crm_audit_log record (old/new values, actor tagged as the
          client) — the same structured trail the CRM's own PATCH path writes
          via @audit_change, so a client self-service edit is no longer
          invisible to that trail. Best-effort: never fails the update.
        - Cache invalidation handled by the router layer

        Returns the updated profile.
        """
        contact_in = {
            k: v for k, v in fields.items() if k in self.PROFILE_CONTACT_FIELDS and v is not None
        }
        safe_fields = {
            k: v
            for k, v in fields.items()
            if k in self.PROFILE_EDITABLE_FIELDS
            and k not in self.PROFILE_CONTACT_FIELDS
            and v is not None
        }
        requested: list[str] = []
        request_inserted = False

        # A value that cannot be stored (a lone surrogate) is refused here, before
        # any statement: it can never equal a stored value, so the order of the
        # checks is unaffected, and asyncpg's argument error never carries it.
        for value in contact_in.values():
            _refuse_unencodable(str(value).strip())

        async with self.pool.acquire() as conn:
            if safe_fields or contact_in:
                async with conn.transaction():
                    if contact_in:
                        # A client never writes phone/whatsapp: a changed usable
                        # number becomes a change request for the consultant.
                        # No lookup of who owns a number happens here, so the
                        # answer cannot depend on ownership. The request, its
                        # duplicate check and its bell alert share this
                        # transaction, behind a lock on the client row.
                        requested, request_inserted = await self._request_contact_changes(
                            conn, client_id, contact_in, with_address="address" in safe_fields
                        )
                    if safe_fields:
                        set_parts = []
                        params: list[Any] = []
                        for i, (key, value) in enumerate(safe_fields.items(), start=1):
                            set_parts.append(f"{key} = ${i}")
                            # An address empty after trim is cleared, not stored as "".
                            if key == "address" and isinstance(value, str):
                                value = value.strip() or None
                            params.append(value)

                        params.append(client_id)
                        set_clause = ", ".join(set_parts)
                        # Capture pre-update values for the audit trail inside
                        # the same transaction as the UPDATE, so the trail
                        # cannot record a prior value that was never the one
                        # overwritten. (No version column on `clients`, so
                        # this does not close lost-update on its own.)
                        audit_old_row = await conn.fetchrow(
                            f"SELECT {', '.join(safe_fields.keys())} FROM clients WHERE id = $1",
                            client_id,
                        )
                        audit_old_state = (
                            {k: audit_old_row[k] for k in safe_fields} if audit_old_row else {}
                        )
                        await conn.execute(
                            f"UPDATE clients SET {set_clause}, updated_at = NOW() WHERE id = ${len(params)} AND deleted_at IS NULL",
                            *params,
                        )

            if safe_fields or requested:
                logger.info(
                    f"Portal profile updated for client {client_id}: "
                    f"{list(safe_fields.keys())} contact requests: {requested}"
                )

            if safe_fields and not request_inserted:
                # Notify CRM team: insert notification_alerts record (deduped daily).
                # Built from the updated fields only: a request that was suppressed
                # as a repeat contributes nothing and never touches an earlier
                # pending request alert.
                fields_label = ", ".join(k.replace("_", " ") for k in safe_fields)
                try:
                    await conn.execute(
                        """
                        INSERT INTO notification_alerts
                            (client_id, alert_type, status, message, email_subject)
                        VALUES ($1, 'portal_profile_update', 'sent', $2, $3)
                        ON CONFLICT (client_id, alert_type, created_date) DO NOTHING
                        """,
                        client_id,
                        f"Client updated their profile: {fields_label}",
                        "Portal: profile update",
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to insert portal_profile_update alert for client %s: %s",
                        client_id,
                        e,
                    )

            if safe_fields:
                # Insert portal_messages record so CRM team can see the change in thread view
                try:
                    await conn.execute(
                        """
                        INSERT INTO portal_messages
                            (client_id, practice_id, subject, direction, content, sent_by, is_system_generated)
                        VALUES ($1, NULL, 'Profile updated', 'client_to_team', $2, 'portal', TRUE)
                        """,
                        client_id,
                        "Client updated their profile via the portal: "
                        f"{', '.join(k.replace('_', ' ') for k in safe_fields)}.",
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to insert portal_messages record for client %s: %s", client_id, e
                    )

            if safe_fields:
                # Structured audit trail (crm_audit_log) — the same trail the
                # CRM's PATCH /api/crm/clients/{id} path writes via
                # @audit_change. Actor is tagged "portal-client:<email>" (not
                # a bare email) so an operator reading the trail can tell a
                # client self-service edit apart from a staff-made change —
                # this repo has no prior non-staff convention for this
                # column beyond "system" for fully-automated jobs, and a
                # client is a human actor, not "system". Never allowed to
                # fail the profile update itself (log_state_change already
                # never raises — it wraps its own DB call — but the wrapper
                # here matches the notification_alerts/portal_messages
                # pattern above and upgrades to ERROR so a swallowed audit
                # failure is never a silent hole).
                try:
                    actor_email = current_user.get("email") or f"client-{client_id}"
                    audit_ok = await audit_logger.log_state_change(
                        entity_type="client",
                        entity_id=client_id,
                        old_state=audit_old_state,
                        new_state=safe_fields,
                        user_email=f"portal-client:{actor_email}",
                        change_type="update",
                        metadata={"source": "portal_self_service", "client_id": client_id},
                    )
                    if not audit_ok:
                        logger.error(
                            "crm_audit_log write returned failure for portal profile "
                            "update, client %s fields %s",
                            client_id,
                            list(safe_fields.keys()),
                        )
                except Exception as e:
                    logger.error(
                        "crm_audit_log write raised for portal profile update, "
                        "client %s fields %s: %s",
                        client_id,
                        list(safe_fields.keys()),
                        e,
                        exc_info=True,
                    )

            result = await self._get_profile_data(conn, client_id)
            if result:
                result["contact_change_requested"] = requested
            return result

    async def _request_contact_changes(
        self, conn: Any, client_id: int, contact_in: dict[str, Any], *, with_address: bool = False
    ) -> tuple[list[str], bool]:
        """Turn changed contact numbers into ONE client_to_team request row.

        Called inside the profile transaction. Each field is judged on its own
        against the stored value of THAT field: equal after trim is a no-op, an
        over-long or unusable value is refused (format only), the same phone
        core is a no-op, anything else is a request carrying the value exactly
        as trimmed. Nothing here looks up who owns a number, so the outcome
        never depends on ownership.

        The first statement locks the client row FOR NO KEY UPDATE, so two
        overlapping saves of one client serialise: the second reads the first
        one's committed request and is suppressed as a repeat. That lock does
        not conflict with the KEY SHARE lock a foreign-key check takes, so
        another session's alert or message insert for this client never waits
        on this transaction. Returns (requested fields, whether a
        request row was inserted by this call). The bell alert for an inserted
        request is written here, in the same transaction, and is not best-effort.
        """
        row = await conn.fetchrow(
            "SELECT phone, phone_normalized, whatsapp, email FROM clients"
            " WHERE id = $1 AND deleted_at IS NULL FOR NO KEY UPDATE",
            client_id,
        )

        def _val(key: str) -> str | None:
            try:
                v = row[key] if row else None
            except (KeyError, TypeError):
                return None
            return None if v is None else str(v)

        stored_by_field = {
            "phone": [_val("phone"), _val("phone_normalized")],
            "whatsapp": [_val("whatsapp")],
        }

        changes: list[tuple[str, str]] = []
        for key, value in contact_in.items():
            raw = str(value).strip()
            stored = [v for v in stored_by_field[key] if v is not None]
            if raw in {v.strip() for v in stored}:
                continue
            # (an unencodable value was refused before any statement, see update_profile)
            if len(raw) > _CONTACT_VALUE_MAX:
                raise ProfileContactRefused()
            core = phone_core(raw)
            if core is None:
                raise ProfileContactRefused()
            if core in {c for c in (phone_core(v) for v in stored) if c is not None}:
                continue
            changes.append((key, raw))

        if not changes:
            return [], False
        requested = [k for k, _ in changes]
        text = "Requested contact change: " + "; ".join(
            f"{_CONTACT_LABELS[k]} \u2192 {v}" for k, v in changes
        )

        # Once: an identical request not yet answered by staff is not filed again.
        last = await conn.fetchrow(
            """
            SELECT pm.content,
                   EXISTS (
                       SELECT 1 FROM portal_messages reply
                       WHERE reply.client_id = pm.client_id
                         AND reply.direction = 'team_to_client'
                         AND reply.is_system_generated = FALSE
                         AND (reply.created_at, reply.id) > (pm.created_at, pm.id)
                   ) AS answered
            FROM portal_messages pm
            WHERE pm.client_id = $1 AND pm.direction = 'client_to_team'
              AND pm.subject = 'Contact change requested'
            ORDER BY pm.created_at DESC, pm.id DESC
            LIMIT 1
            """,
            client_id,
        )
        if last is not None and last["answered"] is not True and last["content"] == text:
            return requested, False

        sent_by = _val("email") or f"client-{client_id}"
        await conn.execute(
            """
            INSERT INTO portal_messages
                (client_id, practice_id, subject, direction, content, sent_by, is_system_generated)
            VALUES ($1, NULL, 'Contact change requested', 'client_to_team', $2, $3, FALSE)
            """,
            client_id,
            text,
            sent_by,
        )
        message = "Client requested a contact change: " + ", ".join(
            _CONTACT_LABELS[k] for k in requested
        )
        if with_address:
            message += "; also updated: address"
        # Re-armed exactly as the WhatsApp hand-off alert is: back to unread, the
        # newest row of the bell. Part of this transaction: if it fails, no
        # request row remains.
        await conn.execute(
            """
            INSERT INTO notification_alerts
                (client_id, alert_type, status, message, email_subject, created_at)
            VALUES ($1, 'portal_profile_update', 'pending', $2, $3, clock_timestamp())
            ON CONFLICT (client_id, alert_type, created_date) DO UPDATE
               SET status = 'pending',
                   message = EXCLUDED.message,
                   created_at = GREATEST(
                       notification_alerts.created_at + interval '1 microsecond',
                       EXCLUDED.created_at
                   ),
                   sent_at = NULL
            """,
            client_id,
            message,
            "Portal: contact change requested",
        )
        return requested, True

    async def _get_profile_data(self, conn: Any, client_id: int) -> dict[str, Any]:
        """Fetch profile data from DB (shared between get_profile and update_profile)."""
        row = await conn.fetchrow(
            """
            SELECT c.id, c.full_name, c.email, c.phone, c.whatsapp,
                   c.nationality, c.passport_number, c.passport_expiry,
                   c.date_of_birth, c.gender, c.address, c.created_at as member_since,
                   tm.email as assigned_to_email, tm.full_name as assigned_to_name,
                   -- `avatar`, not `avatar_url`: team_members carries the former and
                   -- has never carried the latter (information_schema, live, 2026-09-10).
                   -- The read path in routers/portal.py always had this right; this copy
                   -- drifted, and because it is the ONLY caller here, every profile SAVE
                   -- raised UndefinedColumnError AFTER the UPDATE had committed — a 500
                   -- on a write that succeeded. See the tripwire in
                   -- tests/test_data_invariant_tripwires.py.
                   tm.avatar as assigned_to_avatar
            FROM clients c
            LEFT JOIN team_members tm ON tm.email = c.assigned_to AND tm.active = true
                AND tm.role IS DISTINCT FROM 'client'
            WHERE c.id = $1 AND c.deleted_at IS NULL
            """,
            client_id,
        )

        if not row:
            return {}

        return {
            "id": row["id"],
            "full_name": row["full_name"],
            "email": row["email"],
            "phone": row["phone"],
            "whatsapp": row["whatsapp"],
            "nationality": row["nationality"],
            "passport_number": row["passport_number"],
            "passport_expiry": str(row["passport_expiry"]) if row["passport_expiry"] else None,
            "date_of_birth": str(row["date_of_birth"]) if row["date_of_birth"] else None,
            "gender": row["gender"],
            "address": row["address"],
            "member_since": str(row["member_since"]) if row["member_since"] else None,
            "assigned_to": {
                "email": row["assigned_to_email"],
                "name": row["assigned_to_name"],
                "avatar_url": row["assigned_to_avatar"],
            }
            if row["assigned_to_email"]
            else None,
        }


__all__ = ["PortalBillingMixin"]
