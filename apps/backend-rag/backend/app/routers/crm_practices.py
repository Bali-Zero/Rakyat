"""
ZANTARA CRM - Practices Management Router
Endpoints for managing practices (KITAS, PT PMA, Visas, etc.)

Refactored: Migrated to asyncpg with connection pooling (2025-12-07)
"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, field_validator, model_validator

from backend.app.core.config import settings
from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.deps.crm_access import get_practices_user_filter
from backend.app.routers.crm_enhanced_documents import DocumentUploadBase64, upload_document_base64
from backend.app.services.internal_email import send_internal_email
from backend.app.utils.client_pools import caller_pool_peers
from backend.app.utils.crm_utils import is_crm_admin, verify_client_access
from backend.app.utils.error_handlers import handle_database_error
from backend.app.utils.json_utils import to_jsonb
from backend.app.utils.logging_utils import get_logger, log_database_operation, log_success
from backend.app.utils.service_accounts import CLIENT_ROLES, normalize_role
from backend.core.cache import cached, invalidate_cache
from backend.services.common.background import spawn
from backend.services.crm.entity_search import (
    build_practice_search,
    escape_like,
    smart_search_available,
)
from backend.services.crm.practice_state_machine import (
    OPEN_INQUIRY_TYPE_CODE,
    STATES_WITHOUT_SERVICE,
    InvalidTransitionError,
    QuotedPriceRequiredError,
    normalize_state,
    validate_quoted_price_for_invoice,
    validate_service_selected,
    validate_transition,
)
from backend.services.invoicing import InvoiceAutomationService
from backend.services.invoicing import invoice_company as invoice_company_service
from backend.services.invoicing.client_email_outcome import (
    client_email_outcome,
    regenerated_message,
)
from backend.services.portal._mixins.documents import PortalDocumentsMixin
from backend.services.portal.required_slots import mark_required_slot_uploaded

logger = get_logger(__name__)


# ================================================
# HELPER: Resolve Query() defaults for direct calls
# ================================================
def resolve_query_param(value: Any, default: Any = None) -> Any:
    """
    Resolve FastAPI Query() defaults to their actual values.
    When endpoint functions are called directly (not via HTTP),
    Query(None) remains an object instead of becoming None.
    """
    if hasattr(value, "default"):
        return value.default
    return value if value is not None else default


router = APIRouter(prefix="/api/crm/practices", tags=["crm-practices"])

# Constants
MAX_LIMIT = 200
PRACTICE_CLIENT_CONTACT_FIELDS = (
    "client_name",
    "client_email",
    "client_phone",
    "client_lead",
)
PRACTICE_CLEARABLE_FIELDS = {
    "assigned_to",
    "start_date",
    "completion_date",
    "expiry_date",
    "notes",
    "internal_notes",
    "client_summary",
}
REVENUE_GROWTH_CACHE_NAMESPACE = "crm_practices_revenue_growth"


async def _attach_invoice_company_name(conn: asyncpg.Connection, practice: dict) -> None:
    """Add ``invoice_company_name`` without joining ``companies`` into the practice reads.

    The id comes from ``practices.*``; a database without the column (or without a
    company) simply has no name to show, so these reads never depend on migration 356.
    """
    company_id = practice.get("invoice_company_id")
    practice["invoice_company_name"] = (
        await conn.fetchval("SELECT company_name FROM companies WHERE id = $1", company_id)
        if company_id is not None
        else None
    )


async def _resolve_invoice_company_http(
    conn: asyncpg.Connection,
    client_id: int,
    *,
    company_id: int | None,
    name: str | None,
    npwp: str | None,
    current_user: dict,
) -> int:
    # Creating a company and linking it mutates the client, so it takes the same
    # write permission as the client link route (admin, or the client's owner).
    await verify_client_access(client_id, current_user, conn, allow_assigned=True, write=True)
    try:
        async with conn.transaction():
            return await invoice_company_service.resolve_invoice_company(
                conn,
                client_id,
                company_id=company_id,
                name=name,
                npwp=npwp,
                actor=(current_user.get("email") or "system").lower(),
            )
    except invoice_company_service.InvoiceCompanyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _practice_ownership_flags(
    current_user: dict[str, Any],
    created_by: str | None,
    assigned_to: str | None,
) -> tuple[bool, bool, bool]:
    """The created_by/assigned_to email-match derivation this router uses
    to decide "does this actor own this practice".

    `update_practice` (below) has enforced this exact matching logic
    inline since inception; this function exists so every OTHER mutation
    route on practices reuses the same derivation instead of re-deriving
    it independently (crm-mutation-scope, 2026-08-25 reconciliation F7:
    three of this router's four mutation paths had NO scope check at
    all — only update_practice enforced admin-or-owner).

    `update_practice`'s own inline block is left untouched on purpose —
    it is the proven, load-bearing implementation this mirrors, and this
    file's constraint is "do not weaken/regress it", so callers should
    reuse THIS helper rather than the other way around.

    Returns (user_is_admin, created_by_match, assigned_to_match). Does
    NOT consider client-level ownership (client.assigned_to) — that is
    a *view* concession update_practice grants separately for the
    client-contact-field scrub, never a *write* pathway.
    """
    user_is_admin = is_crm_admin(current_user)
    peers = caller_pool_peers(current_user)
    created_by_match = bool(created_by and created_by.lower() in peers)
    assigned_to_match = bool(assigned_to and assigned_to.lower() in peers)
    return user_is_admin, created_by_match, assigned_to_match


def _deny_practice_mutation(practice_id: int, current_user: dict[str, Any], detail: str) -> None:
    """Log + raise 403 for a scope-denied practice mutation.

    Structured warning — no raw identifiers logged (CLAUDE.md PII
    boundary), only presence booleans — mirrors verify_client_access's
    `crm.rbac_client_write_denied` convention so a denied attempt here
    is auditable the same way a denied client write already is.
    """
    logger.warning(
        "crm.rbac_practice_write_denied",
        extra={
            "practice_id": practice_id,
            "user_email_present": bool(current_user.get("email")),
        },
    )
    raise HTTPException(status_code=403, detail=detail)


async def _create_hr_bonus_on_completed(
    db_pool: asyncpg.Pool,
    practice_id: int,
    assigned_to: str | None,
) -> None:
    """Auto-create HR bonus ledger entry when a practice is completed via UI.

    Non-blocking: logs warning on failure, never crashes the status update.
    Guards: table existence, active rate, employee record, duplicate check.
    """
    try:
        if not assigned_to:
            return

        async with db_pool.acquire() as conn:
            table_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'hr_bonus_rates')"
            )
            if not table_exists:
                return

            pt = await conn.fetchrow(
                """
                SELECT pt.code, pt.name FROM practice_types pt
                JOIN practices p ON p.practice_type_id = pt.id
                WHERE p.id = $1
                """,
                practice_id,
            )
            if not pt:
                return

            practice_type_code = pt["code"]
            practice_type_name = pt["name"]

            rate = await conn.fetchrow(
                """
                SELECT id, amount_idr FROM hr_bonus_rates
                WHERE practice_type_code = $1
                  AND is_active = TRUE
                  AND effective_from <= CURRENT_DATE
                  AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
                """,
                practice_type_code,
            )
            if not rate:
                return

            emp = await conn.fetchrow(
                """
                SELECT e.id FROM hr_employees e
                JOIN team_members tm ON tm.id = e.team_member_id
                WHERE tm.email = $1 AND e.is_active = TRUE
                """,
                assigned_to,
            )
            if not emp:
                return

            existing = await conn.fetchval(
                "SELECT id FROM hr_bonus_ledger WHERE practice_id = $1 AND employee_id = $2",
                practice_id,
                emp["id"],
            )
            if existing:
                return

            # Cutoff rule: completed after 28th → bonus counts for next month
            today = date.today()
            if today.day > 28:
                if today.month == 12:
                    awarded_date = datetime(today.year + 1, 1, 1, tzinfo=timezone.utc)
                else:
                    awarded_date = datetime(today.year, today.month + 1, 1, tzinfo=timezone.utc)
            else:
                awarded_date = datetime.now(tz=timezone.utc)

            await conn.execute(
                """
                INSERT INTO hr_bonus_ledger (
                    practice_id, employee_id, bonus_rate_id,
                    practice_type_code, amount_idr, status, awarded_at
                ) VALUES ($1, $2, $3, $4, $5, 'pending', $6)
                """,
                practice_id,
                emp["id"],
                rate["id"],
                practice_type_code,
                rate["amount_idr"],
                awarded_date,
            )
            logger.info(
                "HR bonus created: practice=%s, hr_employee=%s, rate_id=%s",
                practice_id,
                emp["id"],
                rate["id"],
            )

            # Notify HR admin (Asya) via email
            await _notify_hr_bonus_pending(
                db_pool,
                assigned_to,
                practice_type_name,
                rate["amount_idr"],
                practice_id,
            )
    except Exception as e:
        logger.warning("HR bonus hook failed for practice %s (%s)", practice_id, type(e).__name__)


def hr_bonus_service_label(practice_type_name: str | None) -> str:
    """Service display name for the HR bonus mail; never the raw service code.

    A missing or blank ``practice_types.name`` falls back to a neutral label; the mail body
    already shows the practice id next to it.
    """
    name = " ".join((practice_type_name or "").split())
    return name or "Unnamed service"


async def _notify_hr_bonus_pending(
    db_pool: asyncpg.Pool,
    employee_email: str,
    practice_type_name: str | None,
    amount_idr: int,
    practice_id: int,
) -> None:
    """Send email to HR admin (Asya) when a bonus needs approval.

    Audited via ``email_send_log``; failures page the owner (hr_bonus is a
    critical email type in ``email_audit.CRITICAL_EMAIL_TYPES``).
    """
    from html import escape

    amount_fmt = f"Rp {amount_idr:,}".replace(",", ".")
    practice_label = hr_bonus_service_label(practice_type_name)

    safe_email = escape(employee_email)
    safe_label = escape(practice_label)

    html_body = (
        f"<p>A new bonus entry needs your approval.</p>"
        f"<p><strong>Employee:</strong> {safe_email}<br>"
        f"<strong>Practice:</strong> {safe_label} (#{practice_id})<br>"
        f"<strong>Amount:</strong> {amount_fmt}</p>"
        f'<p><a href="https://kita.balizero.com/hr/bonuses">'
        f"Approve in HR Dashboard</a></p>"
    )

    await send_internal_email(
        to=settings.hr_notification_email,
        subject=f"HR Bonus Pending: {practice_label} — {amount_fmt}",
        body=html_body,
        log_context=f"crm bonus practice={practice_id}",
        email_type="hr_bonus",
        pool=db_pool,
        practice_id=practice_id,
    )


DEFAULT_LIMIT = 50


CACHE_TTL_STATS_SECONDS = 300  # 5 minutes
DEFAULT_EXPIRY_LOOKAHEAD_DAYS = 90
MAX_EXPIRY_LOOKAHEAD_DAYS = 365


PRIORITY_VALUES = {"low", "normal", "high", "urgent"}


STATUS_VALUES = {
    "inquiry",
    "waiting_documents",
    "sending_invoice",
    "on_process",
    "completed",
    "cancelled",
}

PAYMENT_STATUS_VALUES = {"unpaid", "partial", "paid"}

# ================================================
# PYDANTIC MODELS
# ================================================


# Client-visible timeline entry written when a practice enters a status. The entry is written at
# the moment of the move, BEFORE the invoice automation has run, so for sending_invoice it must
# not claim the invoice was sent: whether the client was emailed is only known afterwards
# (and is told truthfully to the team by the accounting mail).
STATUS_TIMELINE_EVENTS: dict[str, tuple[str, str, str]] = {
    "waiting_documents": ("document_request", "Documents needed", "warning"),
    "sending_invoice": ("payment_due", "Invoice being prepared", "warning"),
    "on_process": ("milestone", "Application in process", "info"),
    "completed": ("completion", "Case completed", "success"),
}


class PracticeCreate(BaseModel):
    client_id: int
    # Optional since 2026-09-11 (Ari): an inquiry may be opened without a
    # service; it is stored against the `open_inquiry` placeholder type and
    # the real service must be picked before leaving the inquiry stage.
    practice_type_code: str | None = None
    status: str = "inquiry"
    priority: str = "normal"  # 'low', 'normal', 'high', 'urgent'
    quoted_price: Decimal | None = None
    discount_amount: Decimal | None = (
        None  # Fixed-IDR discount; final invoice total = quoted_price - discount_amount
    )
    discount_reason: str | None = None  # Free-text reason, optional, surfaced on invoice PDF
    assigned_to: str | None = None  # team member email
    start_date: datetime | None = None
    notes: str | None = None
    internal_notes: str | None = None
    family_member_id: int | None = (
        None  # If practice is for a specific family member (dependent KITAS etc.)
    )
    invoice_company_id: int | None = None
    invoice_company_name: str | None = None
    invoice_company_npwp: str | None = None

    @model_validator(mode="after")
    def _invoice_company_id_or_name(self) -> "PracticeCreate":
        if self.invoice_company_id is not None and self.invoice_company_name:
            raise ValueError("send invoice_company_id or invoice_company_name, not both")
        return self

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """Validate status is one of allowed values"""
        if v not in STATUS_VALUES:
            raise ValueError(f"status must be one of {STATUS_VALUES}, got '{v}'")
        return v

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        """Validate priority is one of allowed values"""
        if v not in PRIORITY_VALUES:
            raise ValueError(f"priority must be one of {PRIORITY_VALUES}, got '{v}'")
        return v

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, v: int) -> int:
        """Validate client_id is positive"""
        if v <= 0:
            raise ValueError("client_id must be positive")
        return v

    @field_validator("quoted_price")
    @classmethod
    def validate_quoted_price(cls, v: Decimal | None) -> Decimal | None:
        """Validate quoted_price is non-negative"""
        if v is not None and v < 0:
            raise ValueError("quoted_price must be non-negative")
        return v

    @field_validator("discount_amount")
    @classmethod
    def validate_discount_amount(cls, v: Decimal | None) -> Decimal | None:
        """Validate discount_amount is non-negative."""
        if v is not None and v < 0:
            raise ValueError("discount_amount must be non-negative")
        return v


class PracticeUpdate(BaseModel):
    status: str | None = None
    # null clears (bill the individual); id picks; name types a new/existing company.
    invoice_company_id: int | None = None
    invoice_company_name: str | None = None
    invoice_company_npwp: str | None = None
    # Pick (or change) the service after creation — resolved to
    # practice_type_id in update_practice. Required before leaving inquiry.
    practice_type_code: str | None = None
    priority: str | None = None
    quoted_price: Decimal | None = None
    discount_amount: Decimal | None = None
    discount_reason: str | None = None
    actual_price: Decimal | None = None
    payment_status: str | None = None
    paid_amount: Decimal | None = None
    assigned_to: str | None = None
    start_date: datetime | None = None
    completion_date: datetime | None = None
    expiry_date: date | None = None
    notes: str | None = None
    internal_notes: str | None = None
    client_visible: bool | None = None
    client_summary: str | None = None
    documents: list[dict] | None = None
    missing_documents: list[str] | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        """Validate status is one of allowed values"""
        if v is not None and v not in STATUS_VALUES:
            raise ValueError(f"status must be one of {STATUS_VALUES}, got '{v}'")
        return v

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str | None) -> str | None:
        """Validate priority is one of allowed values"""
        if v is not None and v not in PRIORITY_VALUES:
            raise ValueError(f"priority must be one of {PRIORITY_VALUES}, got '{v}'")
        return v

    @field_validator("payment_status")
    @classmethod
    def validate_payment_status(cls, v: str | None) -> str | None:
        """Validate payment_status is one of allowed values (unpaid|partial|paid)."""
        if v is not None and v not in PAYMENT_STATUS_VALUES:
            raise ValueError(
                f"payment_status must be one of {PAYMENT_STATUS_VALUES}, got '{v}'",
            )
        return v

    @field_validator("quoted_price", "actual_price", "paid_amount")
    @classmethod
    def validate_price_fields(cls, v: Decimal | None) -> Decimal | None:
        """Validate price fields are non-negative"""
        if v is not None and v < 0:
            raise ValueError("Price fields must be non-negative")
        return v


class PracticeResponse(BaseModel):
    id: int
    uuid: str
    client_id: int
    practice_type_id: int
    status: str
    priority: str
    quoted_price: Decimal | None
    discount_amount: Decimal | None = None
    discount_reason: str | None = None
    actual_price: Decimal | None
    payment_status: str
    assigned_to: str | None
    start_date: datetime | None
    completion_date: datetime | None
    expiry_date: date | None
    created_at: datetime
    family_member_id: int | None = None
    invoice_company_id: int | None = None


# ================================================
# ENDPOINTS
# ================================================


@router.post("/", response_model=PracticeResponse)
async def create_practice(
    request: Request,
    practice: PracticeCreate,
    background_tasks: BackgroundTasks,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> PracticeResponse:
    """
    Create a new practice for a client

    - **client_id**: ID of the client
    - **practice_type_code**: Practice type code (retrieved from database)
    - **status**: Initial status (default: 'inquiry')
    - **quoted_price**: Price quoted to client
    - **assigned_to**: Team member email to handle this
    """
    # Extract created_by from current user
    created_by = current_user.get("email", "").lower()

    # No service picked → open inquiry against the placeholder type. Only an
    # inquiry-stage practice may start without a service.
    practice_type_code = practice.practice_type_code or OPEN_INQUIRY_TYPE_CODE
    if (
        practice_type_code == OPEN_INQUIRY_TYPE_CODE
        and practice.status not in STATES_WITHOUT_SERVICE
    ):
        raise HTTPException(
            status_code=400,
            detail=f"practice_type_code is required to create a practice in status '{practice.status}'",
        )
    practice.practice_type_code = practice_type_code

    try:
        async with db_pool.acquire() as conn:
            # Get practice_type_id from code
            practice_type_row = await conn.fetchrow(
                "SELECT id, base_price, category, name FROM practice_types"
                " WHERE code = $1 AND is_active = true",
                practice_type_code,
            )

            if not practice_type_row:
                if practice_type_code == OPEN_INQUIRY_TYPE_CODE:
                    # The placeholder is seeded by migration 311; its absence is a
                    # deployment gap, not a caller error — say so instead of 404.
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            f"Practice type '{OPEN_INQUIRY_TYPE_CODE}' is not seeded "
                            "(migration 311 not applied) — cannot open an inquiry without a service"
                        ),
                    )
                # A retired row (is_active = false, e.g. migration 327) is not
                # found either: the catalogue route hides it, so must creation.
                raise HTTPException(
                    status_code=404,
                    detail=f"Practice type '{practice_type_code}' not found or retired",
                )

            # If family_member_id is set, verify it belongs to the same client.
            # Rejecting mismatches here avoids confusing UX where a practice
            # silently lands on the wrong client's family tree.
            if practice.family_member_id is not None:
                fm_client_id = await conn.fetchval(
                    "SELECT client_id FROM client_family_members WHERE id = $1",
                    practice.family_member_id,
                )
                if fm_client_id is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Family member {practice.family_member_id} not found",
                    )
                if fm_client_id != practice.client_id:
                    raise HTTPException(
                        status_code=400,
                        detail="Family member does not belong to this client",
                    )

            # Use base_price if no quoted price provided
            quoted_price = practice.quoted_price or practice_type_row["base_price"]

            # A practice cannot be CREATED straight into Sending Invoice without a price either.
            try:
                validate_quoted_price_for_invoice(practice.status, quoted_price)
            except QuotedPriceRequiredError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

            # Auto-assign: if not explicitly set, inherit from client or use creator
            assigned_to = practice.assigned_to
            if not assigned_to:
                client_row = await conn.fetchrow(
                    "SELECT assigned_to FROM clients WHERE id = $1",
                    practice.client_id,
                )
                assigned_to = (client_row["assigned_to"] if client_row else None) or created_by

            # Insert practice — conditionally include start_date
            insert_columns = [
                "client_id",
                "practice_type_id",
                "status",
                "priority",
                "quoted_price",
                "assigned_to",
                "notes",
                "internal_notes",
                "inquiry_date",
                "created_by",
            ]
            insert_values: list[Any] = [
                practice.client_id,
                practice_type_row["id"],
                practice.status,
                practice.priority,
                quoted_price,
                assigned_to,
                practice.notes,
                practice.internal_notes,
                datetime.now(tz=timezone.utc),
                created_by,
            ]
            if practice.start_date is not None:
                insert_columns.append("start_date")
                insert_values.append(practice.start_date)
            if practice.family_member_id is not None:
                insert_columns.append("family_member_id")
                insert_values.append(practice.family_member_id)

            # Discount (owner decision Q4=a, 2026-05-06): two dedicated columns
            # plus an audit entry in metadata.discount_log so we can trace who
            # applied which discount when, even if discount_amount is later
            # changed via PATCH.
            if practice.discount_amount is not None:
                if quoted_price is not None and practice.discount_amount > quoted_price:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"discount_amount ({practice.discount_amount}) cannot exceed "
                            f"quoted_price ({quoted_price})"
                        ),
                    )
                insert_columns.append("discount_amount")
                insert_values.append(practice.discount_amount)
            if practice.discount_reason is not None and practice.discount_reason.strip():
                insert_columns.append("discount_reason")
                insert_values.append(practice.discount_reason.strip())
            if practice.discount_amount is not None and practice.discount_amount > 0:
                discount_audit = {
                    "discount_log": [
                        {
                            "amount": str(practice.discount_amount),
                            "reason": (practice.discount_reason or "").strip() or None,
                            "applied_by": created_by,
                            "applied_at": datetime.now(tz=timezone.utc).isoformat(),
                            "event": "create",
                        }
                    ]
                }
                insert_columns.append("metadata")
                # Pass the dict directly — the api pool registers a jsonb
                # codec (service_initializer._light_init_connection) so
                # asyncpg serialises with json.dumps() exactly once.
                # Calling json.dumps() at this layer would double-encode and
                # land the value as a JSONB string scalar (the bug shipped
                # in PR #485 / #492 — observed 2026-05-06 on practice_id=390
                # where metadata::text was '"{\"discount_log\": ...}"').
                insert_values.append(discount_audit)

            # Cast jsonb columns explicitly. asyncpg does not register a
            # JSON codec for `jsonb` by default, so a `json.dumps()` value
            # passed to a `jsonb` column lands as a JSON-encoded STRING
            # scalar (i.e. the whole thing is wrapped in another pair of
            # quotes), not as a parsed object — which silently breaks
            # `metadata ? 'discount_log'` and any downstream jsonb_path_*
            # query. Discovered live 2026-05-06 on practice_id=390 where
            # metadata::text was '"{\"discount_log\": ...}"' instead of
            # '{"discount_log": ...}'. The matching UPDATE block at
            # line ~1268 already does `metadata = $1::jsonb`; the INSERT
            # path now mirrors that.
            JSONB_COLUMNS = {"metadata", "documents", "missing_documents", "custom_fields"}
            placeholders = ", ".join(
                f"${i}::jsonb" if insert_columns[i - 1] in JSONB_COLUMNS else f"${i}"
                for i in range(1, len(insert_values) + 1)
            )
            columns_str = ", ".join(insert_columns)

            async with conn.transaction():
                practice_row = await conn.fetchrow(
                    f"""
                    INSERT INTO practices ({columns_str})
                    VALUES ({placeholders})
                    RETURNING *
                    """,
                    *insert_values,
                )

                if not practice_row:
                    raise HTTPException(status_code=500, detail="Failed to create practice")

                new_practice = dict(practice_row)
                if practice.invoice_company_id is not None or practice.invoice_company_name:
                    new_practice["invoice_company_id"] = await _resolve_invoice_company_http(
                        conn,
                        practice.client_id,
                        company_id=practice.invoice_company_id,
                        name=practice.invoice_company_name,
                        npwp=practice.invoice_company_npwp,
                        current_user=current_user,
                    )
                    await conn.execute(
                        "UPDATE practices SET invoice_company_id = $1 WHERE id = $2",
                        new_practice["invoice_company_id"],
                        new_practice["id"],
                    )
                # Convert UUID to string for Pydantic validation
                if new_practice.get("uuid"):
                    new_practice["uuid"] = str(new_practice["uuid"])

                # Create a client-visible timeline event (optional; requires timeline_events table)
                try:
                    practice_type_name = practice_type_row["name"] or practice.practice_type_code
                    practice_category = practice_type_row["category"] or "practice"
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO timeline_events (
                                client_id, practice_id, event_type, title, description,
                                event_date, client_visible, color, created_by
                            )
                            VALUES ($1, $2, 'milestone', $3, $4, NOW(), true, 'info', $5)
                            """,
                            practice.client_id,
                            new_practice["id"],
                            f"New {practice_category} case started",
                            f"{practice_type_name} started",
                            created_by,
                        )
                except Exception as e:
                    # Undefined table (timeline_events missing) is safe to ignore during rollout.
                    if getattr(e, "sqlstate", None) != "42P01":
                        logger.warning(
                            "Could not create timeline event for practice create: %s",
                            type(e).__name__,
                        )

                # Update client's last_interaction_date
                await conn.execute(
                    """
                    UPDATE clients
                    SET last_interaction_date = NOW()
                    WHERE id = $1
                    """,
                    practice.client_id,
                )

                # Log activity
                await conn.execute(
                    """
                    INSERT INTO activity_log (entity_type, entity_id, action, performed_by, description)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    "practice",
                    new_practice["id"],
                    "created",
                    created_by,
                    f"New {practice.practice_type_code} practice created",
                )

            log_success(
                logger,
                "Created practice",
                practice_id=new_practice["id"],
                client_id=practice.client_id,
            )
            log_database_operation(logger, "CREATE", "practices", record_id=new_practice["id"])

            await invalidate_cache("zantara:crm_practices_stats:*")
            await invalidate_cache("zantara:crm_clients_stats:*")

            # Practice kickoff communications (Trigger 2)
            try:
                from backend.services.crm.welcome.welcome_practice_service import (
                    send_practice_kickoff,
                )

                background_tasks.add_task(
                    send_practice_kickoff,
                    new_practice["id"],
                    practice.client_id,
                    practice.practice_type_code,
                    db_pool,
                )
            except Exception as e:
                logger.error("Practice kickoff communication setup failed: %s", type(e).__name__)

            return PracticeResponse(**new_practice)

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


def _parse_month_param(month: str | None) -> tuple[datetime, datetime] | None:
    """Parse 'YYYY-MM' string into (start_of_month, start_of_next_month) datetimes."""
    if not month:
        return None
    try:
        parts = month.split("-")
        year, m = int(parts[0]), int(parts[1])
        start = datetime(year, m, 1, tzinfo=timezone.utc)
        if m == 12:
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(year, m + 1, 1, tzinfo=timezone.utc)
        return start, end
    except (ValueError, IndexError) as exc:
        logger.debug(
            "crm_practices.month_param_invalid",
            extra={"error_type": type(exc).__name__},
        )
        return None


def _build_status_transitions(rows: list[dict]) -> dict[int, list[dict]]:
    """Group activity_log rows into {practice_id: [{status, at}]} dict."""
    result: dict[int, list[dict]] = {}
    for row in rows:
        pid = row["entity_id"]
        changes = row["changes"]
        if isinstance(changes, str):
            changes = json.loads(changes)
        status = changes.get("status")
        if not status:
            continue
        if pid not in result:
            result[pid] = []
        result[pid].append({"status": status, "at": str(row["performed_at"])})
    return result


@router.get("/", response_model=list[dict])
async def list_practices(
    request: Any = None,
    client_id: int | None = Query(None, description="Filter by client ID"),
    status: str | None = Query(None, description="Filter by status"),
    assigned_to: str | None = Query(None, description="Filter by assigned team member"),
    practice_type: str | None = Query(None, description="Filter by practice type code"),
    priority: str | None = Query(None, description="Filter by priority"),
    month: str | None = Query(None, description="Filter by month YYYY-MM"),
    search: str | None = Query(
        None,
        max_length=200,
        description="Smart search: client name or e-mail, service, practice number, phone",
    ),
    include_history: bool = Query(False, description="Include status transition history"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
    # Support for direct calls from dashboard_summary.py
    user_id: str | None = None,
    pool: Any | None = None,
) -> list[Any]:
    """
    List practices with optional filtering.

    Access Control:
    - Admin users (zero@balizero.com, role=admin): See ALL practices
    - Team members: See only practices where they are the Lead (client.assigned_to = user email)

    Returns practices with client and practice type information joined
    """
    # Use provided pool if called directly
    if pool:
        db_pool = pool

    # FIX: Resolve Depends/Query objects when called directly (not via HTTP)
    # FastAPI doesn't resolve these defaults for direct function calls
    # When current_user is still a Depends object (direct call), skip RBAC filtering
    if not isinstance(current_user, dict):
        current_user = None  # Will skip RBAC filtering safely

    client_id = resolve_query_param(client_id)
    status = resolve_query_param(status)
    assigned_to = resolve_query_param(assigned_to)
    practice_type = resolve_query_param(practice_type)
    priority = resolve_query_param(priority)
    month = resolve_query_param(month)
    search = resolve_query_param(search)
    include_history = resolve_query_param(include_history, False)
    limit = resolve_query_param(limit, DEFAULT_LIMIT)
    offset = resolve_query_param(offset, 0)

    try:
        async with db_pool.acquire() as conn:
            # Build query dynamically
            query_parts = [
                """
                SELECT
                    p.*,
                    c.full_name as client_name,
                    c.email as client_email,
                    c.phone as client_phone,
                    c.assigned_to as client_lead,
                    pt.name as practice_type_name,
                    pt.code as practice_type_code,
                    pt.category as practice_category,
                    fm.full_name as family_member_name,
                    fm.relationship as family_member_relationship
                FROM practices p
                JOIN clients c ON p.client_id = c.id
                JOIN practice_types pt ON p.practice_type_id = pt.id
                LEFT JOIN client_family_members fm ON p.family_member_id = fm.id
                WHERE 1=1
                """,
            ]
            params: list[Any] = []
            param_index = 1

            if client_id:
                query_parts.append(f" AND p.client_id = ${param_index}")
                params.append(client_id)
                param_index += 1

            if status:
                query_parts.append(f" AND p.status = ${param_index}")
                params.append(status)
                param_index += 1

            if assigned_to:
                query_parts.append(f" AND p.assigned_to = ${param_index}")
                params.append(assigned_to)
                param_index += 1

            if practice_type:
                query_parts.append(f" AND pt.code = ${param_index}")
                params.append(practice_type)
                param_index += 1

            if priority:
                query_parts.append(f" AND p.priority = ${param_index}")
                params.append(priority)
                param_index += 1

            # RBAC: non-admin users only see practices assigned to them
            # When current_user is None (direct call), use assigned_to param for filtering
            if current_user:
                practices_filter = get_practices_user_filter(current_user)
                if practices_filter:
                    query_parts.append(f" AND p.assigned_to = ANY(${param_index}::text[])")
                    params.append(practices_filter)
                    param_index += 1
                    logger.info("RBAC: Filtering practices by p.assigned_to")

            # Smart search (same behaviour as the client search; legacy substring fallback)
            if isinstance(search, str) and search.strip():
                smart = (
                    await build_practice_search(conn, search, param_index)
                    if await smart_search_available(conn)
                    else None
                )
                if smart is not None:
                    query_parts.append(smart.clause)
                    params.extend(smart.params)
                    param_index = smart.next_index
                else:
                    query_parts.append(
                        f" AND (c.full_name ILIKE ${param_index} OR pt.code ILIKE ${param_index}"
                        f" OR p.id::text LIKE ${param_index})"
                    )
                    params.append(f"%{escape_like(search.strip())}%")
                    param_index += 1

            # Month filter
            month_range = _parse_month_param(month)
            if month_range:
                month_start, month_end = month_range
                query_parts[0] = query_parts[0].replace(
                    "WHERE 1=1",
                    """LEFT JOIN activity_log al ON al.entity_type = 'practice'
                        AND al.entity_id = p.id
                        AND al.action = 'updated'
                    WHERE 1=1""",
                )
                query_parts.append(
                    f" AND p.status != 'cancelled'"
                    f" AND ((p.created_at >= ${param_index} AND p.created_at < ${param_index + 1})"
                    f" OR (al.performed_at >= ${param_index} AND al.performed_at < ${param_index + 1}))",
                )
                params.extend([month_start, month_end])
                param_index += 2
                query_parts[0] = query_parts[0].replace("SELECT\n", "SELECT DISTINCT\n", 1)

            query_parts.append(
                f" ORDER BY p.created_at DESC LIMIT ${param_index} OFFSET ${param_index + 1}",
            )
            params.extend([limit, offset])

            query = " ".join(query_parts)
            rows = await conn.fetch(query, *params)

            practices = [dict(row) for row in rows]

            # Enrich with status transitions if requested
            if include_history and practices:
                practice_ids = [p["id"] for p in practices]
                history_rows = await conn.fetch(
                    """
                    SELECT entity_id, changes, performed_at
                    FROM activity_log
                    WHERE entity_type = 'practice'
                      AND action = 'updated'
                      AND changes::text LIKE '%"status"%'
                      AND entity_id = ANY($1)
                    ORDER BY performed_at ASC
                    """,
                    practice_ids,
                )
                transitions = _build_status_transitions([dict(r) for r in history_rows])
                for p in practices:
                    p["status_transitions"] = transitions.get(p["id"], [])

            return practices

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.get("/active")
async def get_active_practices(
    request: Request,
    assigned_to: str | None = Query(None),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> list[Any]:
    """
    Get all active practices (in progress, not completed/cancelled)

    Access Control:
    - Admin users: Can view all active practices
    - Team members: Can only view practices assigned to them
    """
    try:
        # RBAC: non-admin users can only see their own practices
        practices_filter = get_practices_user_filter(current_user)
        owners = practices_filter if practices_filter and not assigned_to else None
        if owners is None and assigned_to:
            owners = [assigned_to]

        async with db_pool.acquire() as conn:
            query_parts = ["SELECT * FROM active_practices_view WHERE 1=1"]
            params: list[Any] = []
            param_index = 1

            if owners:
                query_parts.append(f" AND assigned_to = ANY(${param_index}::text[])")
                params.append(owners)

            query_parts.append(" ORDER BY priority DESC, start_date ASC")
            query = " ".join(query_parts)

            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    except Exception as e:
        raise handle_database_error(e)


@router.get("/renewals/upcoming")
async def get_upcoming_renewals(
    request: Request,
    days: int = Query(
        DEFAULT_EXPIRY_LOOKAHEAD_DAYS,
        ge=1,
        le=MAX_EXPIRY_LOOKAHEAD_DAYS,
        description="Days to look ahead",
    ),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> list[Any]:
    """
    Get practices with upcoming renewal dates

    Access Control:
    - Admin users: Can view all upcoming renewals
    - Team members: Can only view renewals for practices assigned to them

    Default: next 90 days
    """
    try:
        # RBAC: non-admin users can only see their own renewals
        practices_filter = get_practices_user_filter(current_user)

        async with db_pool.acquire() as conn:
            query = "SELECT * FROM upcoming_renewals_view WHERE days_until_expiry <= $1"
            params: list[Any] = [days]
            if practices_filter:
                query += " AND assigned_to = ANY($2::text[])"
                params.append(practices_filter)
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    except Exception as e:
        raise handle_database_error(e)


# ================================================
# PRACTICE TYPES CATALOG (must be before {practice_id} routes)
# ================================================

CATEGORY_LABELS = {
    "single_entry_visa": "Single Entry Visa",
    "visa_extension": "Visa Extension",
    "multiple_entry_visa": "Multiple Entry Visa",
    "kitas": "KITAS Permits",
    "kitap": "KITAP Permits",
    "company": "Company Services",
    "tax": "Tax Services",
    # 2026 owner decision Q1=B (2026-05-06, see migration 157): split
    # Tax & Accounting into 4 sub-categories so the dropdown groups
    # monthly basic / monthly bundled / yearly packages / standalone
    # fees as separate options rather than collapsing 17 entries under
    # a single "Tax" header.
    "tax_monthly": "Tax — Monthly Report (basic)",
    "tax_monthly_bundled": "Tax — Monthly Report (incl. LKPM & Annual)",
    "tax_annual": "Tax — Annual Packages",
    "tax_standalone": "Tax — Standalone & Compliance",
    "other": "Other Process",
    "urgent": "Urgent Services",
}

CATEGORY_ORDER = list(CATEGORY_LABELS.keys())


@router.get("/types/catalog")
async def get_practice_types_catalog(
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Get all active practice types grouped by category for the process creation form."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, code, name, description, category, base_price,
                       typical_duration_days
                FROM practice_types
                WHERE is_active = true
                  AND code <> $1
                ORDER BY category, name
                """,
                OPEN_INQUIRY_TYPE_CODE,
            )

            categories: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                cat = row["category"] or "other"
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "description": row["description"],
                        "base_price": float(row["base_price"]) if row["base_price"] else None,
                        "typical_duration_days": row["typical_duration_days"],
                    }
                )

            result = []
            for cat_code in CATEGORY_ORDER:
                if cat_code in categories:
                    result.append(
                        {
                            "code": cat_code,
                            "label": CATEGORY_LABELS.get(cat_code, cat_code),
                            "services": categories[cat_code],
                        }
                    )

            return {
                "categories": result,
                "total_services": len(rows),
            }

    except Exception as e:
        raise handle_database_error(e)


async def _require_visible_client(
    conn: asyncpg.Connection, current_user: dict, client_id: int
) -> None:
    filter_email = get_practices_user_filter(current_user)
    if filter_email:
        visible = await conn.fetchval(
            "SELECT 1 FROM clients WHERE id = $1 AND (assigned_to = ANY($2::text[])"
            " OR EXISTS (SELECT 1 FROM practices WHERE client_id = $1"
            " AND (created_by = ANY($2::text[]) OR LOWER(assigned_to) = ANY($2::text[]))))",
            client_id,
            filter_email,
        )
        if not visible:
            raise HTTPException(status_code=404, detail="Client not found")


@router.get("/invoice-company/suggestion")
async def invoice_company_suggestion(
    request: Request,
    client_id: int = Query(..., gt=0),
    practice_type_code: str | None = Query(None),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Companies the client is linked to and what the form should prefill for this service."""
    try:
        async with db_pool.acquire() as conn:
            await _require_visible_client(conn, current_user, client_id)
            category = (
                await conn.fetchval(
                    "SELECT category FROM practice_types WHERE code = $1", practice_type_code
                )
                if practice_type_code
                else None
            )
            links = await invoice_company_service.fetch_client_company_links(conn, client_id)
        result = invoice_company_service.suggest(links, category)
        return {
            "autofill": result.autofill,
            "suggested_company_id": result.suggested_company_id,
            "companies": [
                {
                    "id": c["company_id"],
                    "company_name": c["company_name"],
                    "npwp_company": c["npwp_company"],
                    "role": c["role"],
                    "is_director": c["is_director"],
                }
                for c in result.companies
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.get("/invoice-company/search")
async def invoice_company_search(
    request: Request,
    client_id: int = Query(..., gt=0),
    q: str = Query("", max_length=100),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Existing companies matching what is typed in the invoice-company field (ids and names)."""
    try:
        async with db_pool.acquire() as conn:
            await _require_visible_client(conn, current_user, client_id)
            companies = await invoice_company_service.search_companies(conn, client_id, q)
        return {"companies": companies}
    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.get("/{practice_id}")
async def get_practice(
    request: Request,
    practice_id: int = Path(..., gt=0, description="Practice ID"),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> Any:
    """
    Get practice details by ID with full client and type info.

    Access Control:
    - Admin users: Can view any practice
    - Team members: Can only view practices where they are assigned_to or created_by
    """
    try:
        async with db_pool.acquire() as conn:
            # RBAC: non-admin users only see practices they own
            practices_filter = get_practices_user_filter(current_user)
            base_query = """
                    SELECT
                        p.*,
                        c.full_name as client_name,
                        c.email as client_email,
                        c.phone as client_phone,
                        c.assigned_to as client_lead,
                        pt.name as practice_type_name,
                        pt.code as practice_type_code,
                        pt.category as practice_category,
                        pt.required_documents as required_documents
                    FROM practices p
                    JOIN clients c ON p.client_id = c.id
                    JOIN practice_types pt ON p.practice_type_id = pt.id
                    WHERE p.id = $1
            """
            if practices_filter:
                base_query += (
                    " AND (c.assigned_to = ANY($2::text[]) OR p.created_by = ANY($2::text[])"
                    " OR LOWER(p.assigned_to) = ANY($2::text[]))"
                )
                row = await conn.fetchrow(base_query, practice_id, practices_filter)
                if not row:
                    logger.info("RBAC: Practice %s not visible to caller", practice_id)
            else:
                row = await conn.fetchrow(base_query, practice_id)

            if not row:
                raise HTTPException(status_code=404, detail="Practice not found")

            practice = dict(row)
            await _attach_invoice_company_name(conn, practice)
            if practices_filter:
                created_by_match = str(practice.get("created_by") or "").lower() in practices_filter
                client_lead_match = (
                    str(practice.get("client_lead") or "").lower() in practices_filter
                )
                if not (created_by_match or client_lead_match):
                    for field in PRACTICE_CLIENT_CONTACT_FIELDS:
                        practice.pop(field, None)
            return practice

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.patch("/{practice_id}")
async def update_practice(
    request: Request,
    practice_id: int = Path(..., gt=0, description="Practice ID"),
    updates: PracticeUpdate = Body(...),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> Any:
    """
    Update practice information.

    Access Control:
    - Admin users: Can update any practice
    - Team members: Can only update practices they created or are assigned to

    Common status values:
    - inquiry
    - waiting_documents
    - sending_invoice
    - on_process
    - completed
    """
    user_email = current_user.get("email", "unknown")
    try:
        async with db_pool.acquire() as conn:
            # Fetch old state for timeline events (status changes)
            old_status: str | None = None
            practice_client_id: int | None = None
            practice_client_visible: bool = True
            practice_created_by: str | None = None
            practice_assigned_to: str | None = None
            practice_client_lead: str | None = None
            old_start_date: Any = None
            old_completion_date: Any = None
            old_discount_amount: Decimal | None = None
            old_metadata: dict | None = None
            old_quoted_price: Decimal | None = None
            old_practice_type_code: str | None = None
            try:
                old_row = await conn.fetchrow(
                    """
                    SELECT p.status, p.client_id, p.client_visible, p.created_by,
                           p.assigned_to, p.start_date, p.completion_date,
                           p.discount_amount, p.metadata, p.quoted_price,
                           c.assigned_to AS client_lead,
                           pt.code AS practice_type_code
                    FROM practices p
                    LEFT JOIN clients c ON p.client_id = c.id
                    LEFT JOIN practice_types pt ON pt.id = p.practice_type_id
                    WHERE p.id = $1
                    """,
                    practice_id,
                )
                if old_row:
                    old_status = old_row["status"]
                    practice_client_id = old_row["client_id"]
                    practice_client_visible = (
                        bool(old_row["client_visible"])
                        if old_row["client_visible"] is not None
                        else True
                    )
                    practice_created_by = old_row.get("created_by", "")
                    practice_assigned_to = old_row.get("assigned_to", "")
                    practice_client_lead = old_row.get("client_lead", "")
                    old_start_date = old_row.get("start_date")
                    old_completion_date = old_row.get("completion_date")
                    old_discount_amount = old_row.get("discount_amount")
                    raw_meta = old_row.get("metadata")
                    if isinstance(raw_meta, str):
                        try:
                            old_metadata = json.loads(raw_meta)
                        except Exception:
                            old_metadata = {}
                    else:
                        old_metadata = raw_meta or {}
                    old_quoted_price = old_row.get("quoted_price")
                    old_practice_type_code = old_row.get("practice_type_code")
            except Exception as e:
                # Backward compatibility: client_visible column may not exist yet.
                if getattr(e, "sqlstate", None) == "42703":
                    old_row = await conn.fetchrow(
                        """
                        SELECT p.status, p.client_id, p.created_by, p.assigned_to,
                               p.start_date, p.completion_date, p.quoted_price,
                               c.assigned_to AS client_lead,
                               pt.code AS practice_type_code
                        FROM practices p
                        LEFT JOIN clients c ON p.client_id = c.id
                        LEFT JOIN practice_types pt ON pt.id = p.practice_type_id
                        WHERE p.id = $1
                        """,
                        practice_id,
                    )
                    if old_row:
                        old_status = old_row["status"]
                        practice_client_id = old_row["client_id"]
                        practice_client_visible = True
                        practice_created_by = old_row.get("created_by", "")
                        practice_assigned_to = old_row.get("assigned_to", "")
                        practice_client_lead = old_row.get("client_lead", "")
                        old_start_date = old_row.get("start_date")
                        old_completion_date = old_row.get("completion_date")
                        old_quoted_price = old_row.get("quoted_price")
                        old_practice_type_code = old_row.get("practice_type_code")
                else:
                    raise

            # RBAC: Check ownership or admin status
            user_is_admin = is_crm_admin(current_user)
            user_peers = caller_pool_peers(current_user)
            created_by_match = bool(
                practice_created_by and practice_created_by.lower() in user_peers
            )
            client_lead_match = bool(
                practice_client_lead and practice_client_lead.lower() in user_peers
            )
            if not user_is_admin:
                # Check if user is creator or assigned to this practice
                assigned_to_match = (
                    practice_assigned_to and practice_assigned_to.lower() in user_peers
                )
                if not (created_by_match or assigned_to_match):
                    raise HTTPException(
                        status_code=403,
                        detail="You don't have permission to update this practice",
                    )

            # Validate state machine transition if status is being changed
            if updates.status and updates.status != old_status:
                # Normalize legacy states for comparison
                normalized_old = normalize_state(old_status)
                try:
                    validate_transition(normalized_old, updates.status, current_user)
                    # An open inquiry (placeholder service) may not advance
                    # until a real service is chosen — in this same PATCH or
                    # an earlier one.
                    validate_service_selected(
                        updates.status,
                        updates.practice_type_code or old_practice_type_code,
                    )
                except InvalidTransitionError as e:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid status transition: {e}",
                    ) from e

            # Build update query dynamically
            update_fields: list[str] = []
            params: list[Any] = []
            param_index = 1

            # practice_type_code → practice_type_id (the column the row stores).
            # Resolved up-front so an unknown/inactive code fails before any SQL.
            update_set = updates.dict(exclude_unset=True)
            invoice_company_touched = any(
                key in update_set
                for key in ("invoice_company_id", "invoice_company_name", "invoice_company_npwp")
            )
            invoice_company_id_in = update_set.pop("invoice_company_id", None)
            invoice_company_name_in = update_set.pop("invoice_company_name", None)
            invoice_company_npwp_in = update_set.pop("invoice_company_npwp", None)
            new_practice_type_code = update_set.pop("practice_type_code", None)
            if "practice_type_code" in updates.model_fields_set and not new_practice_type_code:
                raise HTTPException(status_code=400, detail="practice_type_code cannot be null")
            service_base_price = None  # base price a chosen service back-fills into a NULL price
            if new_practice_type_code:
                # A live practice may not be regressed to the placeholder: the
                # status gate above only fires on a status CHANGE, so a PATCH
                # carrying just practice_type_code needs its own check against
                # the effective (new or current) status.
                effective_status = normalize_state(updates.status or old_status or "")
                if (
                    new_practice_type_code == OPEN_INQUIRY_TYPE_CODE
                    and effective_status not in STATES_WITHOUT_SERVICE
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Cannot set '{OPEN_INQUIRY_TYPE_CODE}' on a practice in status "
                            f"'{effective_status}' — only inquiry/cancelled may have no service"
                        ),
                    )
                new_type_row = await conn.fetchrow(
                    "SELECT id, base_price FROM practice_types WHERE code = $1 AND is_active = true",
                    new_practice_type_code,
                )
                if not new_type_row:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Practice type '{new_practice_type_code}' not found",
                    )
                update_fields.append(f"practice_type_id = ${param_index}")
                params.append(new_type_row["id"])
                param_index += 1
                # An open inquiry was created with quoted_price NULL (the
                # placeholder has no base_price). Back-fill from the chosen
                # service so the invoice step never sees a NULL price, unless
                # the caller sets the price explicitly in this same PATCH.
                # The fill itself is `COALESCE(quoted_price, base)` in the UPDATE, so it is
                # decided on the row as it is when the write happens, not on this early read.
                if "quoted_price" not in update_set and new_type_row["base_price"] is not None:
                    service_base_price = new_type_row["base_price"]

            invoice_company_pos: int | None = None
            if invoice_company_touched:
                clear_requested = (
                    "invoice_company_id" in updates.model_fields_set
                    and invoice_company_id_in is None
                    and not invoice_company_name_in
                    and not invoice_company_npwp_in
                )
                if invoice_company_id_in is not None and invoice_company_name_in:
                    raise HTTPException(
                        status_code=422,
                        detail="send invoice_company_id or invoice_company_name, not both",
                    )
                if (
                    not clear_requested
                    and invoice_company_id_in is None
                    and not invoice_company_name_in
                ):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "invoice_company_name or invoice_company_id is required"
                            " (send invoice_company_id: null alone to clear)"
                        ),
                    )
                if practice_client_id is None:
                    raise HTTPException(status_code=404, detail="Practice not found")
                update_fields.append(f"invoice_company_id = ${param_index}")
                params.append(None)
                invoice_company_pos = len(params) - 1
                param_index += 1

            # Map of allowed fields to database columns
            field_mapping = {
                "status": "status",
                "priority": "priority",
                "quoted_price": "quoted_price",
                "discount_amount": "discount_amount",
                "discount_reason": "discount_reason",
                "actual_price": "actual_price",
                "payment_status": "payment_status",
                "paid_amount": "paid_amount",
                "assigned_to": "assigned_to",
                "start_date": "start_date",
                "completion_date": "completion_date",
                "expiry_date": "expiry_date",
                "notes": "notes",
                "internal_notes": "internal_notes",
                "client_visible": "client_visible",
                "client_summary": "client_summary",
                "documents": "documents",
                "missing_documents": "missing_documents",
            }

            # Owner decision 2026-11-13: no move INTO Sending Invoice without a Quoted Price.
            # The effective price is the one this same PATCH writes (explicit, or back-filled
            # from a service chosen in it), else the one on the row; a PATCH that sets price
            # and status together passes. Only entering the status is gated: a practice
            # already in Sending Invoice keeps accepting unrelated edits.
            if updates.status and normalize_state(old_status or "") != "sending_invoice":
                try:
                    validate_quoted_price_for_invoice(
                        updates.status,
                        update_set["quoted_price"]
                        if "quoted_price" in update_set
                        else (
                            old_quoted_price if old_quoted_price is not None else service_base_price
                        ),
                    )
                except QuotedPriceRequiredError as e:
                    raise HTTPException(status_code=422, detail=str(e)) from e

            for field, value in update_set.items():
                if field not in field_mapping:
                    raise HTTPException(status_code=400, detail=f"Invalid field name: {field}")

                if value is None and field not in PRACTICE_CLEARABLE_FIELDS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field} cannot be null",
                    )

                column_name = field_mapping[field]
                update_fields.append(f"{column_name} = ${param_index}")
                params.append(value)
                param_index += 1

            if service_base_price is not None:
                update_fields.append(f"quoted_price = COALESCE(quoted_price, ${param_index})")
                params.append(service_base_price)
                param_index += 1

            # Autoset date fields on status transitions (only if not already in DB
            # and caller did not pass an explicit value). Uses NOW() server-side,
            # no parameter binding required.
            if (
                updates.status == "completed"
                and "completion_date" not in update_set
                and old_completion_date is None
            ):
                update_fields.append("completion_date = NOW()")
            if (
                updates.status == "on_process"
                and "start_date" not in update_set
                and old_start_date is None
            ):
                update_fields.append("start_date = NOW()")

            if not update_fields:
                raise HTTPException(status_code=400, detail="No fields to update")

            # Cutoff rule: completed after 28th → practice shows as next month
            today = date.today()
            if updates.status == "completed" and today.day > 28:
                if today.month == 12:
                    next_month_1st = datetime(today.year + 1, 1, 1, tzinfo=timezone.utc)
                else:
                    next_month_1st = datetime(today.year, today.month + 1, 1, tzinfo=timezone.utc)
                update_fields.append(f"updated_at = ${param_index}")
                params.append(next_month_1st)
                param_index += 1
            else:
                update_fields.append("updated_at = NOW()")

            # Discount vs quoted_price sanity (owner Q4=a): if both quoted_price
            # and discount_amount end up touched in this PATCH, the new discount
            # must not exceed the new quoted_price. Use the post-update value
            # for each field (fall back to the value already on the row).
            if updates.discount_amount is not None:
                effective_quoted = (
                    updates.quoted_price if updates.quoted_price is not None else old_quoted_price
                )
                if effective_quoted is not None and Decimal(updates.discount_amount) > Decimal(
                    effective_quoted
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"discount_amount ({updates.discount_amount}) cannot exceed "
                            f"quoted_price ({effective_quoted})"
                        ),
                    )

            update_fields_str = ", ".join(update_fields)

            # Column names are from a whitelist (field_mapping), values are parameterized
            # nosemgrep: sqlalchemy-execute-raw-query
            query = f"""
                WITH updated AS (
                    UPDATE practices
                    SET {update_fields_str}
                    WHERE id = ${param_index}
                    RETURNING *
                )
                SELECT
                    updated.*,
                    c.full_name as client_name,
                    c.email as client_email,
                    c.phone as client_phone,
                    c.assigned_to as client_lead,
                    pt.name as practice_type_name,
                    pt.code as practice_type_code,
                    pt.category as practice_category,
                    pt.required_documents as required_documents
                FROM updated
                LEFT JOIN clients c ON updated.client_id = c.id
                LEFT JOIN practice_types pt ON updated.practice_type_id = pt.id
            """
            params.append(practice_id)

            async with conn.transaction():
                if updates.status and normalize_state(updates.status) == "sending_invoice":
                    # The gate above ran on values read before this transaction. Lock the row
                    # and decide on the LOCKED status and price: a concurrent commit (price
                    # to 0/NULL, or a move out of sending_invoice) cannot slip a move in.
                    locked = await conn.fetchrow(
                        "SELECT status, quoted_price FROM practices WHERE id = $1 FOR UPDATE",
                        practice_id,
                    )
                    if locked and normalize_state(locked["status"] or "") != "sending_invoice":
                        try:
                            validate_quoted_price_for_invoice(
                                updates.status,
                                update_set["quoted_price"]
                                if "quoted_price" in update_set
                                else (
                                    locked["quoted_price"]
                                    if locked["quoted_price"] is not None
                                    else service_base_price
                                ),
                            )
                        except QuotedPriceRequiredError as e:
                            raise HTTPException(status_code=422, detail=str(e)) from e
                if invoice_company_pos is not None and (
                    invoice_company_id_in is not None or invoice_company_name_in
                ):
                    # Resolved inside the same transaction as the UPDATE: a later
                    # failure rolls the company and the link back with it.
                    params[invoice_company_pos] = await _resolve_invoice_company_http(
                        conn,
                        practice_client_id,
                        company_id=invoice_company_id_in,
                        name=invoice_company_name_in,
                        npwp=invoice_company_npwp_in,
                        current_user=current_user,
                    )
                row = await conn.fetchrow(query, *params)  # nosemgrep

                if not row:
                    raise HTTPException(status_code=404, detail="Practice not found")

                updated_practice = dict(row)
                await _attach_invoice_company_name(conn, updated_practice)
                response_practice = dict(updated_practice)
                can_view_client_details = user_is_admin or created_by_match or client_lead_match
                if not can_view_client_details:
                    for field in PRACTICE_CLIENT_CONTACT_FIELDS:
                        response_practice.pop(field, None)

                # Discount audit log (owner decision Q3=c, 2026-05-06): append an
                # entry to metadata.discount_log every time discount_amount is
                # touched, even when only the reason changes. Idempotent — we
                # ignore the case where the new value equals the old one.
                try:
                    discount_changed = updates.discount_amount is not None and (
                        old_discount_amount is None
                        or Decimal(updates.discount_amount) != Decimal(old_discount_amount)
                    )
                    reason_changed = (
                        updates.discount_reason is not None
                        and (updates.discount_reason or "").strip()
                    )
                    if discount_changed or reason_changed:
                        user_email = (
                            current_user.get("email", "system")
                            if isinstance(current_user, dict)
                            else "system"
                        )
                        audit_entry = {
                            "amount": str(updates.discount_amount)
                            if updates.discount_amount is not None
                            else (
                                str(old_discount_amount)
                                if old_discount_amount is not None
                                else None
                            ),
                            "reason": (updates.discount_reason or "").strip() or None,
                            "applied_by": user_email,
                            "applied_at": datetime.now(tz=timezone.utc).isoformat(),
                            "event": "update",
                            "previous_amount": str(old_discount_amount)
                            if old_discount_amount is not None
                            else None,
                        }
                        new_meta = dict(old_metadata or {})
                        log = new_meta.get("discount_log") or []
                        if not isinstance(log, list):
                            log = []
                        log.append(audit_entry)
                        new_meta["discount_log"] = log
                        # Pass dict directly — pool's jsonb codec encodes once.
                        # See create_practice() comment around the metadata
                        # insert for the symptom of double-encoding.
                        async with conn.transaction():
                            await conn.execute(
                                "UPDATE practices SET metadata = $1 WHERE id = $2",
                                new_meta,
                                practice_id,
                            )
                        updated_practice["metadata"] = new_meta
                        response_practice["metadata"] = new_meta
                except Exception as audit_exc:
                    # Audit failure must NOT roll back the user-visible discount
                    # update — the business write already succeeded. Log + move on.
                    logger.warning(
                        "Discount audit append failed for practice %s: %s",
                        practice_id,
                        type(audit_exc).__name__,
                    )

                # Create timeline event for client-visible status changes (optional)
                if (
                    updates.status is not None
                    and old_status is not None
                    and updates.status != old_status
                    and practice_client_id is not None
                ):
                    # Respect client visibility flag (defaulting to visible)
                    is_visible = bool(
                        updated_practice.get("client_visible", practice_client_visible)
                    )
                    if is_visible:
                        if updates.status in STATUS_TIMELINE_EVENTS:
                            event_type, title, color = STATUS_TIMELINE_EVENTS[updates.status]
                            description = (
                                updated_practice.get("client_summary")
                                or f"Status: {updates.status}"
                            )
                            try:
                                async with conn.transaction():
                                    await conn.execute(
                                        """
                                        INSERT INTO timeline_events (
                                            client_id, practice_id, event_type, title, description,
                                            event_date, client_visible, color, created_by
                                        )
                                        VALUES ($1, $2, $3, $4, $5, NOW(), true, $6, $7)
                                        """,
                                        practice_client_id,
                                        practice_id,
                                        event_type,
                                        title,
                                        description,
                                        color,
                                        user_email,
                                    )
                            except Exception as e:
                                if getattr(e, "sqlstate", None) != "42P01":
                                    logger.warning(
                                        "Could not create timeline event for status change: %s",
                                        type(e).__name__,
                                    )

                # Log activity
                changed_fields = list(updates.dict(exclude_unset=True).keys())
                # Serialize changes dict to JSON string for asyncpg JSONB compatibility
                # See: backend/app/utils/json_utils.py and CLAUDE.md "Asyncpg + JSONB Guidelines"
                changes_json = to_jsonb(updates.dict(exclude_unset=True))
                await conn.execute(
                    """
                    INSERT INTO activity_log (entity_type, entity_id, action, performed_by, description, changes)
                    VALUES ($1, $2, $3, $4, $5, $6::text::jsonb)
                    """,
                    "practice",
                    practice_id,
                    "updated",
                    user_email,
                    f"Updated: {', '.join(changed_fields)}",
                    changes_json,
                )

                # If status changed to 'completed' and there's an expiry date, create renewal alert
                if updates.status == "completed" and updates.expiry_date:
                    alert_date = updates.expiry_date - timedelta(days=60)  # 60 days before expiry

                    await conn.execute(
                        """
                        INSERT INTO renewal_alerts (
                            practice_id, client_id, alert_type, description,
                            target_date, alert_date, notify_team_member
                        )
                        SELECT
                            $1, client_id, 'renewal_due',
                            'Practice renewal due soon',
                            $2, $3, assigned_to
                        FROM practices
                        WHERE id = $4
                        """,
                        practice_id,
                        updates.expiry_date,
                        alert_date,
                        practice_id,
                    )

            # Notify client via portal about status change
            if (
                updates.status is not None
                and old_status is not None
                and updates.status != old_status
                and practice_client_id is not None
            ):
                try:
                    from backend.services.portal.portal_notification_service import (
                        PortalNotificationService,
                        practice_display_name,
                    )

                    notif_service = PortalNotificationService(db_pool)
                    practice_type = practice_display_name(response_practice)
                    spawn(
                        notif_service.notify_practice_status_changed(
                            client_id=practice_client_id,
                            practice_id=practice_id,
                            practice_type=practice_type,
                            new_status=updates.status,
                            sent_by=user_email,
                        ),
                    )
                except Exception as e:
                    logger.error(
                        "Portal notification for practice status failed: %s", type(e).__name__
                    )

            # 🚀 Waiting Documents Automation: Trigger when status changes to 'waiting_documents'
            if updates.status == "waiting_documents" and old_status != "waiting_documents":
                from backend.services.crm.waiting_documents_service import WaitingDocumentsService

                waiting_docs_service = WaitingDocumentsService(db_pool)
                spawn(
                    waiting_docs_service.trigger_on_waiting_documents(
                        practice_id=practice_id,
                        triggered_by=user_email,
                    ),
                )
                logger.info(
                    "🚀 Waiting documents automation triggered for practice %s", practice_id
                )

            # 🚀 Invoice Automation: Trigger when status changes to 'sending_invoice'
            if updates.status == "sending_invoice" and old_status != "sending_invoice":
                invoice_service = InvoiceAutomationService(db_pool)
                # Run in background to not block the response
                spawn(
                    invoice_service.trigger_on_sending_invoice(
                        practice_id=practice_id,
                        triggered_by=user_email,
                    ),
                )
                logger.info("🚀 Invoice automation triggered for practice %s", practice_id)

            # NOTE: on_process automation handled by PracticeStatusListener (M5)
            # via PG NOTIFY trigger — no need to duplicate here.

            # 🚀 Process Completion Automation: Trigger when status changes to 'completed'
            if updates.status == "completed" and old_status != "completed":
                from backend.services.crm.completed_process_service import CompletedProcessService

                completion_service = CompletedProcessService(db_pool)
                spawn(
                    completion_service.trigger_on_completed(
                        practice_id=practice_id,
                        triggered_by=user_email,
                    ),
                )
                logger.info(
                    "🚀 Process completion automation triggered for practice %s",
                    practice_id,
                )

                # 🎯 HR Bonus Auto-Creation: create bonus ledger entry for assigned team member
                spawn(
                    _create_hr_bonus_on_completed(
                        db_pool,
                        practice_id,
                        updated_practice.get("assigned_to"),
                    ),
                )

            log_success(
                logger,
                "Updated practice",
                practice_id=practice_id,
            )
            await invalidate_cache("zantara:crm_practices_stats:*")
            await invalidate_cache(f"zantara:{REVENUE_GROWTH_CACHE_NAMESPACE}:*")
            await invalidate_cache("zantara:crm_clients_stats:*")
            return response_practice

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.delete("/{practice_id}")
async def delete_practice(
    request: Request,
    practice_id: int = Path(..., gt=0, description="Practice ID"),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Delete a practice (soft delete - marks as cancelled).

    Access Control:
    - Admin users: Can delete any practice
    - Team members: Can only delete practices they created or are assigned to

    The practice is marked as 'cancelled' and not permanently deleted.
    """
    try:
        user_email = current_user.get("email", "").lower()

        async with db_pool.acquire() as conn:
            # Fetch practice to check ownership
            practice_row = await conn.fetchrow(
                """
                SELECT created_by, assigned_to
                FROM practices
                WHERE id = $1
                """,
                practice_id,
            )

            if not practice_row:
                raise HTTPException(status_code=404, detail="Practice not found")

            # RBAC: Check ownership or admin status
            user_is_admin = is_crm_admin(current_user)
            if not user_is_admin:
                created_by = (practice_row.get("created_by") or "").lower()
                assigned_to = (practice_row.get("assigned_to") or "").lower()
                peers = caller_pool_peers(current_user)
                created_by_match = created_by in peers
                assigned_to_match = assigned_to in peers
                if not (created_by_match or assigned_to_match):
                    raise HTTPException(
                        status_code=403,
                        detail="You don't have permission to delete this practice",
                    )

            # Soft delete - mark as cancelled
            row = await conn.fetchrow(
                """
                UPDATE practices
                SET status = 'cancelled', updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                practice_id,
            )

            if not row:
                raise HTTPException(status_code=404, detail="Practice not found")

            # Log activity
            await conn.execute(
                """
                INSERT INTO activity_log (entity_type, entity_id, action, performed_by, description)
                VALUES ($1, $2, $3, $4, $5)
                """,
                "practice",
                practice_id,
                "deleted",
                user_email,
                "Practice marked as cancelled",
            )

            log_success(
                logger,
                "Deleted (soft) practice",
                practice_id=practice_id,
            )
            await invalidate_cache("zantara:crm_practices_stats:*")
            await invalidate_cache("zantara:crm_clients_stats:*")
            return {"success": True, "message": "Practice marked as cancelled"}

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.post("/{practice_id}/documents/add")
async def add_document_to_practice(
    request: Request,
    practice_id: int = Path(..., gt=0, description="Practice ID"),
    document_name: str = Query(...),
    drive_file_id: str = Query(...),
    uploaded_by: str = Query(...),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Add a document to a practice

    Access Control:
    - Admin users: can add documents to any practice
    - Team members: can only add documents to practices they created or
      are assigned to (mirrors update_practice; crm-mutation-scope)

    - **document_name**: Name/type of document (e.g., "Passport Copy")
    - **drive_file_id**: Google Drive file ID
    - **uploaded_by**: Email of person uploading
    """
    try:
        async with db_pool.acquire() as conn:
            # Get current documents + ownership fields (for the RBAC gate below)
            row = await conn.fetchrow(
                "SELECT documents, created_by, assigned_to FROM practices WHERE id = $1",
                practice_id,
            )

            if not row:
                raise HTTPException(status_code=404, detail="Practice not found")

            user_is_admin, created_by_match, assigned_to_match = _practice_ownership_flags(
                current_user, row.get("created_by"), row.get("assigned_to")
            )
            if not user_is_admin and not (created_by_match or assigned_to_match):
                _deny_practice_mutation(
                    practice_id,
                    current_user,
                    "You don't have permission to add documents to this practice",
                )

            documents = row["documents"] or []

            # Add new document
            new_doc = {
                "name": document_name,
                "drive_file_id": drive_file_id,
                "uploaded_at": datetime.now(tz=timezone.utc).isoformat(),
                "uploaded_by": uploaded_by,
                "status": "received",
            }

            documents.append(new_doc)

            # Update practice
            await conn.execute(
                """
                UPDATE practices
                SET documents = $1, updated_at = NOW()
                WHERE id = $2
                """,
                documents,
                practice_id,
            )

            log_success(
                logger,
                "Added document to practice",
                practice_id=practice_id,
            )

            await invalidate_cache("zantara:crm_practices_stats:*")
            return {"success": True, "document": new_doc, "total_documents": len(documents)}

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.get("/stats/overview")
@cached(ttl=CACHE_TTL_STATS_SECONDS, prefix="crm_practices_stats")
async def get_practices_stats(
    request: Any = None,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
    # Support for direct calls from dashboard_summary.py
    user_id: str | None = None,
    pool: Any | None = None,
    is_admin: bool = False,
) -> dict[str, Any]:
    """
    Get overall practice statistics

    - Counts by status
    - Counts by practice type
    - Revenue metrics

    Access Control:
    - Admin: sees global stats across all practices
    - Team member: sees only stats for practices assigned to them

    Performance: Cached for 5 minutes to reduce database load.
    """
    # Use provided pool if called directly
    if pool:
        db_pool = pool

    try:
        # Resolve effective user and admin status
        effective_user: str | None = None
        if user_id:
            effective_user = user_id.lower()
        elif isinstance(current_user, dict):
            effective_user = current_user.get("email", "").lower()
            is_admin = is_admin or not get_practices_user_filter(current_user)
        else:
            logger.debug("get_practices_stats called without user context (internal call)")

        # Build RBAC filter: non-admins see only their assigned practices
        rbac_where = ""
        rbac_params: list[Any] = []
        if not is_admin and effective_user:
            rbac_where = " AND assigned_to = ANY($1::text[])"
            # An explicit user_id is an internal caller naming one owner: no pool expansion.
            rbac_params = [[effective_user] if user_id else sorted(caller_pool_peers(current_user))]

        async with db_pool.acquire() as conn:
            # By status (filtered by user for non-admins)
            by_status_rows = await conn.fetch(
                f"""
                SELECT status, COUNT(*) as count
                FROM practices
                WHERE 1=1{rbac_where}
                GROUP BY status
                """,
                *rbac_params,
            )

            # By practice type (filtered by user for non-admins)
            by_type_rows = await conn.fetch(
                f"""
                SELECT pt.code, pt.name, COUNT(p.id) as count
                FROM practices p
                JOIN practice_types pt ON p.practice_type_id = pt.id
                WHERE 1=1{rbac_where.replace("assigned_to", "p.assigned_to")}
                GROUP BY pt.code, pt.name
                ORDER BY count DESC
                """,
                *rbac_params,
            )

            # Revenue stats (filtered by user for non-admins)
            revenue_row = await conn.fetchrow(
                f"""
                SELECT
                    SUM(actual_price) as total_revenue,
                    SUM(CASE WHEN payment_status = 'paid' THEN actual_price ELSE 0 END) as paid_revenue,
                    SUM(CASE WHEN payment_status IN ('unpaid', 'partial') THEN actual_price - COALESCE(paid_amount, 0) ELSE 0 END) as outstanding_revenue
                FROM practices
                WHERE actual_price IS NOT NULL{rbac_where}
                """,
                *rbac_params,
            )

            # Active practices count (filtered by user for non-admins)
            active_row = await conn.fetchrow(
                f"""
                SELECT COUNT(*) as count
                FROM practices
                WHERE status IN ('inquiry', 'waiting_documents', 'sending_invoice', 'on_process'){rbac_where}
                """,
                *rbac_params,
            )

            by_status = {row["status"]: row["count"] for row in by_status_rows}
            by_type = [dict(row) for row in by_type_rows]
            revenue = dict(revenue_row) if revenue_row else {}
            active_count = active_row["count"] if active_row else 0

            return {
                "total_practices": sum(by_status.values()),
                "active_practices": active_count,
                "by_status": by_status,
                "by_type": by_type,
                "revenue": revenue,
            }

    except Exception as e:
        raise handle_database_error(e)


@router.get("/stats/revenue-growth")
@cached(ttl=CACHE_TTL_STATS_SECONDS, prefix=REVENUE_GROWTH_CACHE_NAMESPACE)
async def get_practices_revenue_growth(
    request: Any = None,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Month-over-month revenue growth for the practices book.

    Contract consumed by the frontend (crm.api.ts::getRevenueGrowth →
    useDashboardStats). The FE runs this inside a Promise.all with practice
    stats + expiry alerts and has NO catch, so a 404 here previously rejected
    the whole dashboard stats block on every workspace page. The route MUST
    exist and return the shape below.

    Response shape:
        {
          "current_month":  {total_revenue, paid_revenue, outstanding_revenue},
          "previous_month": {total_revenue, paid_revenue, outstanding_revenue},
          "growth_percentage": float,   # (curr - prev) / prev * 100, 0 if prev==0
          "monthly_breakdown": [ {month, total_revenue}, ... ]  # last 6 months
        }

    Access Control:
    - Admin (full view): revenue across all practices.
    - Team member: revenue only for practices assigned to them.

    Performance: cached for 5 minutes (CACHE_TTL_STATS_SECONDS).
    """
    try:
        # RBAC: None for admins (full view), else the user's email plus client-pool peers.
        user_filter = get_practices_user_filter(current_user)

        # Build the optional RBAC clause; placeholder indices are DERIVED from
        # params length (PR #2142 class) so the admin path (empty params) and
        # the team-member path (one leading param) both bind without an
        # IndeterminateDatatypeError.
        params: list[Any] = []
        rbac_where = ""
        if user_filter:
            params.append(user_filter)
            rbac_where = f" AND assigned_to = ANY(${len(params)}::text[])"

        # Month boundaries (UTC), computed in Python so the SQL only binds dates.
        now = datetime.now(timezone.utc)
        current_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # First day of previous month.
        prev_start = (current_start - timedelta(days=1)).replace(day=1)
        next_start = (current_start + timedelta(days=32)).replace(day=1)
        # Six-month window start (for the breakdown).
        breakdown_start = current_start
        for _ in range(5):
            breakdown_start = (breakdown_start - timedelta(days=1)).replace(day=1)

        revenue_select = (
            "SUM(actual_price) AS total_revenue, "
            "SUM(CASE WHEN payment_status = 'paid' THEN actual_price ELSE 0 END) "
            "AS paid_revenue, "
            "SUM(CASE WHEN payment_status IN ('unpaid', 'partial') "
            "THEN actual_price - COALESCE(paid_amount, 0) ELSE 0 END) "
            "AS outstanding_revenue"
        )

        async with db_pool.acquire() as conn:
            # Current month.
            cur_start_idx = len(params) + 1
            cur_end_idx = len(params) + 2
            current_row = await conn.fetchrow(
                f"""
                SELECT {revenue_select}
                FROM practices
                WHERE actual_price IS NOT NULL{rbac_where}
                  AND created_at >= ${cur_start_idx}
                  AND created_at < ${cur_end_idx}
                """,
                *params,
                current_start,
                next_start,
            )

            # Previous month.
            prev_start_idx = len(params) + 1
            prev_end_idx = len(params) + 2
            previous_row = await conn.fetchrow(
                f"""
                SELECT {revenue_select}
                FROM practices
                WHERE actual_price IS NOT NULL{rbac_where}
                  AND created_at >= ${prev_start_idx}
                  AND created_at < ${prev_end_idx}
                """,
                *params,
                prev_start,
                current_start,
            )

            # Last-6-months breakdown (total revenue per month).
            bd_start_idx = len(params) + 1
            breakdown_rows = await conn.fetch(
                f"""
                SELECT
                    date_trunc('month', created_at) AS month,
                    SUM(actual_price) AS total_revenue
                FROM practices
                WHERE actual_price IS NOT NULL{rbac_where}
                  AND created_at >= ${bd_start_idx}
                GROUP BY date_trunc('month', created_at)
                ORDER BY month ASC
                """,
                *params,
                breakdown_start,
            )

        def _money(row: Any, key: str) -> float:
            value = row[key] if row and row[key] is not None else 0
            return float(value)

        current_month = {
            "total_revenue": _money(current_row, "total_revenue"),
            "paid_revenue": _money(current_row, "paid_revenue"),
            "outstanding_revenue": _money(current_row, "outstanding_revenue"),
        }
        previous_month = {
            "total_revenue": _money(previous_row, "total_revenue"),
            "paid_revenue": _money(previous_row, "paid_revenue"),
            "outstanding_revenue": _money(previous_row, "outstanding_revenue"),
        }

        prev_total = previous_month["total_revenue"]
        if prev_total > 0:
            growth_percentage = round(
                (current_month["total_revenue"] - prev_total) / prev_total * 100,
                2,
            )
        else:
            growth_percentage = 0.0

        monthly_breakdown = [
            {
                "month": row["month"].date().isoformat(),
                "total_revenue": float(row["total_revenue"] or 0),
            }
            for row in breakdown_rows
        ]

        return {
            "current_month": current_month,
            "previous_month": previous_month,
            "growth_percentage": growth_percentage,
            "monthly_breakdown": monthly_breakdown,
        }

    except Exception as e:
        raise handle_database_error(e)


@router.post("/{practice_id}/regenerate-invoice")
async def regenerate_invoice(
    request: Request,
    practice_id: int = Path(..., gt=0, description="Practice ID"),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Manually regenerate and resend invoice for a practice.

    Use cases:
    - Client lost the invoice email
    - Need to send updated invoice with corrected amount
    - Previous automation failed
    - Invoice generation was skipped (e.g., practice created before automation implemented)

    This endpoint triggers the same workflow as the automatic invoice generation,
    but can be called manually for any practice in 'sending_invoice' status.

    Returns:
        dict: Invoice generation result with file IDs, links, and notification status
    """
    try:
        user_email = current_user.get("email", "").lower()

        # Verify practice exists and check status
        async with db_pool.acquire() as conn:
            practice_row = await conn.fetchrow(
                "SELECT status FROM practices WHERE id = $1",
                practice_id,
            )

            if not practice_row:
                raise HTTPException(status_code=404, detail="Practice not found")

            if practice_row["status"] != "sending_invoice":
                raise HTTPException(
                    status_code=400,
                    detail=f"Can only regenerate invoice for practices in 'sending_invoice' status. Current status: {practice_row['status']}",
                )

        # Trigger invoice automation
        invoice_service = InvoiceAutomationService(db_pool)
        result = await invoice_service.regenerate_invoice(
            practice_id=practice_id,
            triggered_by=user_email,
        )

        if not result.get("success"):
            raise HTTPException(
                status_code=500,
                detail=f"Invoice generation failed: {result.get('error', 'Unknown error')}",
            )

        log_success(
            logger,
            "Invoice regenerated successfully",
            practice_id=practice_id,
        )

        return {
            "success": True,
            "message": regenerated_message(
                result.get("invoice_number"),
                client_email_outcome(
                    bool(result.get("email_sent")), bool(result.get("email_attempted"))
                ),
            ),
            "invoice_number": result.get("invoice_number"),
            "drive_file_id": result.get("drive_file_id"),
            "drive_link": result.get("drive_link"),
            "email_sent": result.get("email_sent"),
            "whatsapp_sent": result.get("whatsapp_sent"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to regenerate invoice for practice %s: %s", practice_id, type(e).__name__
        )
        raise handle_database_error(e)


# ================================================
# REQUIRED DOCUMENTS FOR PRACTICES
# ================================================


class RequiredDocumentCreate(BaseModel):
    document_type: str
    document_label: str
    description: str | None = None
    is_required: bool = True


class RequiredDocumentUpdate(BaseModel):
    status: str | None = None  # pending, uploaded, verified, rejected
    team_member_notes: str | None = None


class RequiredDocumentResponse(BaseModel):
    id: int
    practice_id: int
    document_type: str
    document_label: str
    description: str | None
    is_required: bool
    uploaded_by_client: bool
    uploaded_file_id: int | None
    uploaded_at: str | None
    client_notes: str | None
    team_member_notes: str | None
    status: str
    created_at: str
    updated_at: str


class ClientDocumentUploadRequest(BaseModel):
    required_doc_id: int
    file: str  # base64 encoded
    file_name: str
    notes: str | None = None


@router.get("/{practice_id}/required-documents", response_model=list[RequiredDocumentResponse])
async def get_required_documents(
    practice_id: int = Path(..., gt=0),
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> list[Any]:
    """Get all required documents for a practice (for team members)."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, practice_id, document_type, document_label, description,
                    is_required, uploaded_by_client, uploaded_file_id, uploaded_at,
                    client_notes, team_member_notes, status, created_at, updated_at
                FROM practice_required_documents
                WHERE practice_id = $1
                ORDER BY is_required DESC, document_label ASC
                """,
                practice_id,
            )

            return [
                {
                    "id": row["id"],
                    "practice_id": row["practice_id"],
                    "document_type": row["document_type"],
                    "document_label": row["document_label"],
                    "description": row["description"],
                    "is_required": row["is_required"],
                    "uploaded_by_client": row["uploaded_by_client"],
                    "uploaded_file_id": row["uploaded_file_id"],
                    "uploaded_at": row["uploaded_at"].isoformat() if row["uploaded_at"] else None,
                    "client_notes": row["client_notes"],
                    "team_member_notes": row["team_member_notes"],
                    "status": row["status"],
                    "created_at": row["created_at"].isoformat(),
                    "updated_at": row["updated_at"].isoformat(),
                }
                for row in rows
            ]
    except Exception as e:
        logger.error("Failed to get required documents: %s", type(e).__name__)
        raise handle_database_error(e)


@router.post("/{practice_id}/required-documents", response_model=RequiredDocumentResponse)
async def add_required_document(
    practice_id: int = Path(..., gt=0),
    doc: RequiredDocumentCreate = Body(...),
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> dict[str, Any]:
    """Add a required document to a practice (team member only).

    Access Control:
    - Admin users: can add required documents to any practice
    - Team members: can only add required documents to practices they
      created or are assigned to (mirrors update_practice; crm-mutation-scope)
    """
    try:
        async with db_pool.acquire() as conn:
            # Check if practice exists + fetch ownership fields for the RBAC gate below
            practice = await conn.fetchrow(
                "SELECT id, client_id, created_by, assigned_to FROM practices WHERE id = $1",
                practice_id,
            )
            if not practice:
                raise HTTPException(status_code=404, detail="Practice not found")

            user_is_admin, created_by_match, assigned_to_match = _practice_ownership_flags(
                current_user, practice.get("created_by"), practice.get("assigned_to")
            )
            if not user_is_admin and not (created_by_match or assigned_to_match):
                _deny_practice_mutation(
                    practice_id,
                    current_user,
                    "You don't have permission to add required documents to this practice",
                )

            # Insert required document
            row = await conn.fetchrow(
                """
                INSERT INTO practice_required_documents
                (practice_id, document_type, document_label, description, is_required, created_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (practice_id, document_type) DO UPDATE SET
                    document_label = EXCLUDED.document_label,
                    description = EXCLUDED.description,
                    is_required = EXCLUDED.is_required,
                    updated_at = NOW()
                RETURNING *
                """,
                practice_id,
                doc.document_type,
                doc.document_label,
                doc.description,
                doc.is_required,
                current_user.get("email", "system"),
            )

            # Mark practice as needing client notification
            await conn.execute(
                """
                UPDATE practices
                SET client_notification_sent = FALSE,
                    updated_at = NOW()
                WHERE id = $1
                """,
                practice_id,
            )

            logger.info(
                f"Added required document to practice {practice_id}",
                extra={
                    "practice_id": practice_id,
                    "doc_id": row["id"],
                },
            )

            await invalidate_cache("zantara:crm_practices_stats:*")
            return {
                "id": row["id"],
                "practice_id": row["practice_id"],
                "document_type": row["document_type"],
                "document_label": row["document_label"],
                "description": row["description"],
                "is_required": row["is_required"],
                "uploaded_by_client": row["uploaded_by_client"],
                "uploaded_file_id": row["uploaded_file_id"],
                "uploaded_at": row["uploaded_at"].isoformat() if row["uploaded_at"] else None,
                "client_notes": row["client_notes"],
                "team_member_notes": row["team_member_notes"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to add required document: %s", type(e).__name__)
        raise handle_database_error(e)


@router.delete("/{practice_id}/required-documents/{doc_id}")
async def delete_required_document(
    practice_id: int = Path(..., gt=0),
    doc_id: int = Path(..., gt=0),
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> dict[str, Any]:
    """Delete a required document from a practice.

    Access Control:
    - Admin users: can delete required documents on any practice
    - Team members: can only delete required documents on practices they
      created or are assigned to (mirrors update_practice; crm-mutation-scope)
    """
    try:
        async with db_pool.acquire() as conn:
            practice_row = await conn.fetchrow(
                "SELECT created_by, assigned_to FROM practices WHERE id = $1",
                practice_id,
            )
            if not practice_row:
                raise HTTPException(status_code=404, detail="Practice not found")

            user_is_admin, created_by_match, assigned_to_match = _practice_ownership_flags(
                current_user,
                practice_row.get("created_by"),
                practice_row.get("assigned_to"),
            )
            if not user_is_admin and not (created_by_match or assigned_to_match):
                _deny_practice_mutation(
                    practice_id,
                    current_user,
                    "You don't have permission to delete required documents on this practice",
                )

            result = await conn.execute(
                "DELETE FROM practice_required_documents WHERE id = $1 AND practice_id = $2",
                doc_id,
                practice_id,
            )

            if result == "DELETE 0":
                raise HTTPException(status_code=404, detail="Document not found")

            logger.info(
                "Deleted required document %s from practice %s",
                doc_id,
                practice_id,
                extra={
                    "practice_id": practice_id,
                    "doc_id": doc_id,
                },
            )

            await invalidate_cache("zantara:crm_practices_stats:*")
            return {"success": True, "message": "Document requirement deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete required document: %s", type(e).__name__)
        raise handle_database_error(e)


@router.patch("/{practice_id}/required-documents/{doc_id}", response_model=RequiredDocumentResponse)
async def update_required_document(
    practice_id: int = Path(..., gt=0),
    doc_id: int = Path(..., gt=0),
    update: RequiredDocumentUpdate = Body(...),
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> dict[str, Any]:
    """Update a required document (team member review).

    Access Control:
    - Admin users: can update required documents on any practice
    - Team members: can only update required documents on practices they
      created or are assigned to (mirrors update_practice; crm-mutation-scope)
    """
    try:
        async with db_pool.acquire() as conn:
            practice_row = await conn.fetchrow(
                "SELECT created_by, assigned_to FROM practices WHERE id = $1",
                practice_id,
            )
            if not practice_row:
                raise HTTPException(status_code=404, detail="Practice not found")

            user_is_admin, created_by_match, assigned_to_match = _practice_ownership_flags(
                current_user,
                practice_row.get("created_by"),
                practice_row.get("assigned_to"),
            )
            if not user_is_admin and not (created_by_match or assigned_to_match):
                _deny_practice_mutation(
                    practice_id,
                    current_user,
                    "You don't have permission to update documents on this practice",
                )

            # Build update query dynamically
            updates = []
            params = []
            param_idx = 1

            if update.status:
                updates.append(f"status = ${param_idx}")
                params.append(update.status)
                param_idx += 1

            if update.team_member_notes is not None:
                updates.append(f"team_member_notes = ${param_idx}")
                params.append(update.team_member_notes)
                param_idx += 1

            if not updates:
                raise HTTPException(status_code=400, detail="No fields to update")

            updates.append("updated_at = NOW()")
            params.extend([doc_id, practice_id])

            query = f"""
                UPDATE practice_required_documents
                SET {", ".join(updates)}
                WHERE id = ${param_idx} AND practice_id = ${param_idx + 1}
                RETURNING *
            """

            row = await conn.fetchrow(query, *params)

            if not row:
                raise HTTPException(status_code=404, detail="Document not found")

            logger.info(
                "Updated required document %s for practice %s",
                doc_id,
                practice_id,
                extra={
                    "practice_id": practice_id,
                    "doc_id": doc_id,
                },
            )

            await invalidate_cache("zantara:crm_practices_stats:*")
            return {
                "id": row["id"],
                "practice_id": row["practice_id"],
                "document_type": row["document_type"],
                "document_label": row["document_label"],
                "description": row["description"],
                "is_required": row["is_required"],
                "uploaded_by_client": row["uploaded_by_client"],
                "uploaded_file_id": row["uploaded_file_id"],
                "uploaded_at": row["uploaded_at"].isoformat() if row["uploaded_at"] else None,
                "client_notes": row["client_notes"],
                "team_member_notes": row["team_member_notes"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update required document: %s", type(e).__name__)
        raise handle_database_error(e)


@router.post("/{practice_id}/upload-client-document")
async def upload_client_document(
    practice_id: int = Path(..., gt=0),
    request: ClientDocumentUploadRequest = Body(...),
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> dict[str, Any]:
    """Client uploads a document for a required field."""
    try:
        async with db_pool.acquire() as conn:
            # Get required document info
            req_doc = await conn.fetchrow(
                """
                SELECT prd.*, p.client_id, c.full_name as client_name
                FROM practice_required_documents prd
                JOIN practices p ON prd.practice_id = p.id
                JOIN clients c ON p.client_id = c.id
                WHERE prd.id = $1 AND prd.practice_id = $2
                """,
                request.required_doc_id,
                practice_id,
            )

            if not req_doc:
                raise HTTPException(status_code=404, detail="Required document not found")

            # Verify client is uploading their own document
            client = await conn.fetchrow(
                "SELECT id FROM clients WHERE email = $1",
                current_user.get("email"),
            )

            owns_client = bool(client) and client["id"] == req_doc["client_id"]
            if not owns_client:
                # Allow team members to upload on behalf of client
                if not is_crm_admin(current_user):
                    raise HTTPException(status_code=403, detail="Not authorized")

        # Champion Round 2 scores uploaded_source='client': only a portal client
        # session uploading for its own practice earns it, never a staff session.
        is_client_upload = owns_client and normalize_role(current_user.get("role")) in CLIENT_ROLES

        upload_result = await upload_document_base64(
            client_id=req_doc["client_id"],
            data=DocumentUploadBase64(
                file=request.file.split(",")[-1] if "," in request.file else request.file,
                file_name=request.file_name,
                document_type=req_doc["document_type"],
                notes=request.notes,
                practice_id=practice_id,
                document_category=PortalDocumentsMixin._classify_document_category(
                    req_doc["document_type"], request.file_name
                ),
            ),
            pool=db_pool,
            current_user=current_user,
            background_tasks=background_tasks,
            access_already_verified=True,
            uploaded_source="client" if is_client_upload else "team",
        )
        doc_id = upload_result["document_id"]

        async with db_pool.acquire() as conn:
            # Update required document record
            await mark_required_slot_uploaded(
                conn,
                slot_id=request.required_doc_id,
                document_id=doc_id,
                notes=request.notes,
            )

            logger.info(
                "Client uploaded document for practice %s",
                practice_id,
                extra={
                    "practice_id": practice_id,
                    "required_doc_id": request.required_doc_id,
                    "document_id": doc_id,
                },
            )

            await invalidate_cache("zantara:crm_practices_stats:*")
            return {
                "success": True,
                "document_id": doc_id,
                "message": "Document uploaded successfully",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to upload client document: %s", type(e).__name__)
        raise handle_database_error(e)
