"""Accounting router — cash-control endpoints for Asya (the agency accountant).

P0: read-only views (cashout log, bank transactions, cashbook summary).
P1 adds: statement upload, reconciliation matching, payment confirm.

RBAC: gated to CRM admins (asya@balizero.com is already in CRM_EXTRA_ADMIN_EMAILS)
via is_crm_admin(). Money is IDR. See migration 238_accounting_asya.sql.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

import asyncpg
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.utils.crm_utils import is_crm_admin
from backend.services.accounting import (
    bank_import_service,
    cashout_import_service,
    cashout_service,
    reconcile_service,
)
from backend.services.accounting.bank_import_service import BankImportError
from backend.services.accounting.bank_parser import BankParseError
from backend.services.accounting.bank_pdf_readers import (
    parse_and_preview_statement,
    preview_statement,
)
from backend.services.accounting.companies import (
    OTHER_KEY,
    company_key_for_account,
    company_key_for_issuer,
    load_companies,
)
from backend.services.accounting.done_today_service import BUSINESS_TZ, load_done_today
from backend.services.accounting.lights import classify_transfer
from backend.services.accounting.matcher import MatchCandidate, OpenInvoice, rank_candidates
from backend.services.accounting.outgoing_justify import (
    OutgoingError,
    justify_outgoing,
    undo_justification,
)
from backend.services.accounting.outgoings import load_outgoings
from backend.services.accounting.reconcile_service import ReconcileError

# 8 MB is generous for a digital cashout worksheet PDF (Asya's are < 1 MB).
_MAX_CASHOUT_PDF_BYTES = 8 * 1024 * 1024

# A full year of the real CIMB export is under 1 MB; 2 MB is twice that (spec S8).
_MAX_BANK_STATEMENT_BYTES = 2 * 1024 * 1024

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crm/accounting", tags=["crm-accounting"])


def _parse_cashout_pdf_bytes_sync(pdf_bytes: bytes) -> list[Any]:
    """Write PDF bytes to a private temp file for pdfplumber and clean up."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)
        return cashout_import_service.parse_cashout_pdf_with_weeks(tmp_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _require_accounting_access(current_user: dict[str, Any]) -> None:
    """Gate to CRM admins (includes Asya). Raises 403 otherwise."""
    if not is_crm_admin(current_user):
        raise HTTPException(status_code=403, detail="Accounting access denied")


@router.get("/cashout")
async def get_cashout(
    week_label: str | None = Query(None, description="Filter by week label, e.g. '16 - 23 JAN 26'"),
    type_filter: str | None = Query(None, alias="type", description="Filter by movement type"),
    limit: int = Query(200, ge=1, le=1000),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Weekly cashout rows (mirrors Asya's sheet), newest first."""
    _require_accounting_access(current_user)
    async with db_pool.acquire() as conn:
        return await cashout_service.list_cashout(
            conn, week_label=week_label, type_filter=type_filter, limit=limit
        )


@router.get("/bank-transactions")
async def get_bank_transactions(
    reconciled_status: str | None = Query(
        None, description="unmatched | matched | manual | ignored"
    ),
    limit: int = Query(200, ge=1, le=1000),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Parsed bank statement transactions, newest first."""
    _require_accounting_access(current_user)
    if reconciled_status is not None and reconciled_status not in (
        "unmatched",
        "matched",
        "manual",
        "ignored",
    ):
        raise HTTPException(status_code=422, detail="invalid reconciled_status")
    async with db_pool.acquire() as conn:
        return await cashout_service.list_bank_transactions(
            conn, reconciled_status=reconciled_status, limit=limit
        )


@router.get("/summary")
async def get_summary(
    period_start: date | None = Query(None),
    period_end: date | None = Query(None),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Cash-basis P&L summary over the cashout log."""
    _require_accounting_access(current_user)
    async with db_pool.acquire() as conn:
        return await cashout_service.cashbook_summary(
            conn, period_start=period_start, period_end=period_end
        )


# ── Cashout PDF import (Asya's weekly GABUNGAN worksheet) ────────────────────


@router.post("/import-cashout")
async def import_cashout_pdf(
    file: UploadFile = File(..., description="Asya's GABUNGAN cashout worksheet PDF"),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Import a weekly cashout worksheet PDF into the cashout log.

    The file is Asya's already-processed 9-column worksheet (NAME/PROCESS/PNBP/
    URGENT/RPTKA-IMTA/MARGIN BS/FINAL PRICE/NOTE), digitally exported (parsed with
    pdfplumber — no OCR), usually stacking several weeks each opened by a
    "NEW CASHOUT DD MONTH YYYY" title row.

    Idempotent: re-uploading the same file replaces that week's prior import rows
    instead of duplicating them, and never touches reconciliation-linked rows.
    """
    _require_accounting_access(current_user)

    filename = file.filename or "upload.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="only .pdf cashout worksheets are accepted")

    raw_size = getattr(file, "size", None)
    if isinstance(raw_size, int) and raw_size > _MAX_CASHOUT_PDF_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 8 MB)")

    data = await file.read(_MAX_CASHOUT_PDF_BYTES + 1)
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    if len(data) > _MAX_CASHOUT_PDF_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 8 MB)")

    imported_by = (current_user.get("email") or "unknown").lower()

    try:
        rows = await asyncio.to_thread(_parse_cashout_pdf_bytes_sync, data)
    except ValueError as exc:
        # undated / non-cashout PDF — loud, not a silent empty import (superscar #2)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not rows:
        raise HTTPException(
            status_code=422,
            detail="no cashout rows found in the PDF (parsed 0 client rows)",
        )

    async with db_pool.acquire() as conn:
        result = await cashout_import_service.persist_cashout_rows(
            conn, rows, imported_by=imported_by, source_filename=filename
        )

    # Cashout log changed → drop the same CRM caches the confirm path drops.
    try:
        from backend.core.cache import invalidate_cache

        await invalidate_cache("zantara:crm_clients_stats:*")
    except Exception as cache_exc:  # cache failure must not fail the import
        logger.warning("accounting import-cashout cache invalidation failed: %s", cache_exc)

    return {
        "source_filename": filename,
        "imported": result["imported"],
        "weeks": result["weeks"],
        "replaced": result["deleted"],
    }


# ── Bank statement preview (CIMB CSV, Mandiri PDF, Permata PDF) — read-only ───────────────────


async def _read_statement_upload(file: UploadFile) -> bytes:
    """The upload checks shared by the preview and the import handler (spec I8), in order:
    `.csv` or `.pdf` filename, read up to the limit plus one byte, empty, 413 over the limit.
    The format is then detected from the bytes, not the name. Nothing of the file or its
    name goes into an error."""
    # No default name: an empty filename is refused like any other name without the suffix.
    filename = (file.filename or "").lower()
    if not filename.endswith((".csv", ".pdf")):
        raise HTTPException(status_code=422, detail="only .csv or .pdf bank statements are accepted")

    # limit + 1 byte: enough to detect an oversized upload without loading it whole
    data = await file.read(_MAX_BANK_STATEMENT_BYTES + 1)
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    if len(data) > _MAX_BANK_STATEMENT_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 2 MB)")
    return data


@router.post("/bank-statement/preview")
async def preview_bank_statement(
    file: UploadFile = File(..., description="CIMB Niaga CSV, or Bank Mandiri, BNI or Bank Permata PDF statement"),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Parse a bank statement and return a summary report. Writes nothing."""
    _require_accounting_access(current_user)
    data = await _read_statement_upload(file)

    try:
        # off the event loop: parse + report in one total call
        return await asyncio.to_thread(preview_statement, data)
    except BankParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/bank-statement/import")
async def import_bank_statement(
    file: UploadFile = File(..., description="CIMB Niaga CSV, or Bank Mandiri, BNI or Bank Permata PDF statement"),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Store a bank statement: the preview report plus the import result.

    All or nothing, and a transaction is never stored twice (see bank_import_service).
    The only database write goes through `import_statement`.
    """
    _require_accounting_access(current_user)
    data = await _read_statement_upload(file)

    try:
        statement, report = await asyncio.to_thread(parse_and_preview_statement, data)
    except BankParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        async with db_pool.acquire() as conn:
            result = await bank_import_service.import_statement(
                conn,
                statement,
                uploaded_by=(current_user.get("email") or "unknown").lower(),
                source_filename=file.filename,
            )
    except BankImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # total: the driver's own text can quote the statement's values
        logger.error("accounting bank import: storing failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="bank statement could not be stored") from None

    return {
        **report,
        "import": {
            "statement_id": result.statement_id,
            "rows_inserted": result.rows_inserted,
            "rows_duplicate": result.rows_duplicate,
            "history_segments": result.history_segments,
            "nothing_new": result.nothing_new,
        },
    }


# ── P1: reconciliation ──────────────────────────────────────────────────────


# An invoice is OPEN while its practice's payment status is not `paid`, RECORDED once it is.
_OPEN_POOL_SQL = "COALESCE(p.payment_status, 'unpaid') <> 'paid'"
_RECORDED_POOL_SQL = "COALESCE(p.payment_status, 'unpaid') = 'paid'"


def _finite_int(value: Any) -> int:
    """A stored amount as an int; NULL, NaN and Infinity become 0 (the matcher never matches 0)."""
    if value is None:
        return 0
    try:
        number = value if isinstance(value, Decimal) else Decimal(value)
    except (ArithmeticError, TypeError, ValueError):
        return 0
    return int(number) if number.is_finite() else 0


def _iso(value: Any) -> str | None:
    """An ISO 8601 string when the stored value has `isoformat`, else None."""
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else None


async def _fetch_invoice_pool(conn: asyncpg.Connection, *, recorded: bool) -> list[OpenInvoice]:
    """The invoices of one pool (a SELECT; the predicate is one of the two constants above)."""
    rows = await conn.fetch(
        f"""
        SELECT i.id AS invoice_id, i.practice_id, i.amount_idr,
               i.generated_at, COALESCE(i.pph_expected_idr, 0) AS pph_expected_idr,
               c.full_name AS client_name,
               i.invoice_number, pt.name AS service_name, i.issuer_entity_key
        FROM invoices i
        JOIN practices p ON i.practice_id = p.id
        LEFT JOIN practice_types pt ON pt.id = p.practice_type_id
        LEFT JOIN clients c ON p.client_id = c.id
        WHERE {_RECORDED_POOL_SQL if recorded else _OPEN_POOL_SQL}
        """
    )
    return [
        OpenInvoice(
            invoice_id=r["invoice_id"],
            practice_id=r["practice_id"],
            client_name=r["client_name"] or "",
            amount_idr=_finite_int(r["amount_idr"]),
            generated_at=r["generated_at"].date() if r["generated_at"] else None,
            pph_expected_idr=_finite_int(r["pph_expected_idr"]),
            invoice_number=r.get("invoice_number"),
            invoice_issued_at=_iso(r["generated_at"]),
            service_name=r.get("service_name"),
            issuer_entity_key=r.get("issuer_entity_key"),
        )
        for r in rows
    ]


def _own_company(invoices: list[OpenInvoice], company_key: str | None) -> list[OpenInvoice]:
    """The invoices issued by the company that owns the receiving account (owner rule d).

    A transfer into one company's account never settles another company's invoice, and a
    transfer into an account no company owns settles none.
    """
    if company_key is None:
        return []
    return [i for i in invoices if company_key_for_issuer(i.issuer_entity_key) == company_key]


def _candidate_json(c: MatchCandidate) -> dict[str, Any]:
    return {
        "invoice_id": c.invoice_id,
        "practice_id": c.practice_id,
        "client_name": c.client_name,
        "invoice_amount_idr": c.invoice_amount_idr,
        "confidence": c.confidence,
        "reasons": c.reasons,
        "amount_delta_idr": c.amount_delta_idr,
        "likely_withheld": c.likely_withheld,
        "invoice_number": c.invoice_number,
        "invoice_issued_at": c.invoice_issued_at,
        "service_name": c.service_name,
    }


@router.get("/match-candidates/{txn_id}")
async def match_candidates(
    txn_id: int,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Propose invoice candidates for an incoming (credit) bank transaction.

    The accountant picks the right one and calls /confirm. Never auto-confirms.
    """
    _require_accounting_access(current_user)
    async with db_pool.acquire() as conn:
        tx = await conn.fetchrow(
            """
            SELECT bt.id, bt.txn_date, bt.amount_idr, bt.direction, bt.description,
                   bs.account_number
            FROM bank_transactions bt
            LEFT JOIN bank_statements bs ON bs.id = bt.statement_id
            WHERE bt.id = $1
            """,
            txn_id,
        )
        if tx is None:
            raise HTTPException(status_code=404, detail="bank transaction not found")
        if tx["direction"] != "credit":
            raise HTTPException(status_code=422, detail="only incoming (credit) transactions are matched")

        company_key = company_key_for_account(tx["account_number"])
        open_invoices = _own_company(await _fetch_invoice_pool(conn, recorded=False), company_key)
        recorded_invoices = _own_company(await _fetch_invoice_pool(conn, recorded=True), company_key)
        candidates = rank_candidates(
            transfer_amount_idr=int(tx["amount_idr"]),
            transfer_date=tx["txn_date"],
            payer_memo=tx["description"] or "",
            open_invoices=open_invoices,
        )
        recorded_candidates = rank_candidates(
            transfer_amount_idr=int(tx["amount_idr"]),
            transfer_date=tx["txn_date"],
            payer_memo=tx["description"] or "",
            open_invoices=recorded_invoices,
        )
        return {
            "txn_id": txn_id,
            "transfer_amount_idr": int(tx["amount_idr"]),
            "candidates": [_candidate_json(c) for c in candidates],
            "recorded_candidates": [_candidate_json(c) for c in recorded_candidates],
        }


class ConfirmRequest(BaseModel):
    bank_txn_id: int | None = None
    practice_id: int
    invoice_id: int | None = None
    amount_applied_idr: int = Field(ge=0)
    new_status: str = "paid"
    payment_method: str = "bank_transfer"
    payment_reference: str | None = None
    pph_withheld_idr: int = 0
    bank_fee_idr: int = 0
    pnbp_idr: int = 0
    urgent_idr: int = 0
    rptka_imta_idr: int = 0
    margin_idr: int = 0
    movement_date: date | None = None
    # Historical mode: a payment that arrived in the past, reconciled now. The same rows are
    # written; the practice listener sends no email to the client or the team for it.
    historical: bool = False


@router.post("/confirm")
async def confirm_reconciliation(
    payload: ConfirmRequest,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Confirm a payment match. Single writer of payment_status (superscar #9).

    Paid in full, a case in sending_invoice moves to on_process in the same transaction (owner
    rule a, 2026-11-18, replacing the earlier "second explicit click"); historical
    confirmations never move it.
    """
    _require_accounting_access(current_user)
    confirmed_by = (current_user.get("email") or "unknown").lower()
    async with db_pool.acquire() as conn:
        try:
            result = await reconcile_service.confirm_payment(
                conn,
                bank_txn_id=payload.bank_txn_id,
                practice_id=payload.practice_id,
                invoice_id=payload.invoice_id,
                amount_applied_idr=payload.amount_applied_idr,
                new_status=payload.new_status,
                confirmed_by=confirmed_by,
                payment_method=payload.payment_method,
                payment_reference=payload.payment_reference,
                pph_withheld_idr=payload.pph_withheld_idr,
                bank_fee_idr=payload.bank_fee_idr,
                pnbp_idr=payload.pnbp_idr,
                urgent_idr=payload.urgent_idr,
                rptka_imta_idr=payload.rptka_imta_idr,
                margin_idr=payload.margin_idr,
                movement_date=payload.movement_date,
                historical=payload.historical,
            )
        except ReconcileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Same cache keys the CRM uses (superscar #9) — payment_status changed.
    try:
        from backend.core.cache import invalidate_cache

        await invalidate_cache("zantara:crm_practices_stats:*")
        await invalidate_cache("zantara:crm_clients_stats:*")
        # actual_price can be filled by the confirm: the revenue report reads it.
        from backend.app.routers.crm_practices import REVENUE_GROWTH_CACHE_NAMESPACE

        await invalidate_cache(f"zantara:{REVENUE_GROWTH_CACHE_NAMESPACE}:*")
    except Exception as cache_exc:  # cache failure must not fail the write
        logger.warning("accounting confirm cache invalidation failed: %s", cache_exc)

    return {
        "practice_id": result.practice_id,
        "invoice_id": result.invoice_id,
        "cashout_id": result.cashout_id,
        "reconciliation_log_id": result.reconciliation_log_id,
        "status_before": result.status_before,
        "status_after": result.status_after,
        "historical": payload.historical,
    }


# ── Lights: the reconciliation inbox and the three actions that move no money ───────────


def _classify_inbox(
    txns: list[dict[str, Any]],
    open_invoices: list[OpenInvoice],
    recorded_invoices: list[OpenInvoice],
) -> list[dict[str, Any]]:
    """Pure CPU work over in-memory rows; run off the event loop.

    Each transfer is classified against the invoices of the company that owns its account only.
    """
    pools: dict[str, tuple[list[OpenInvoice], list[OpenInvoice]]] = {}
    items: list[dict[str, Any]] = []
    for tx in txns:
        key = tx["company_key"]
        if key not in pools:
            owner = None if key == OTHER_KEY else key
            pools[key] = (
                _own_company(open_invoices, owner),
                _own_company(recorded_invoices, owner),
            )
        own_open, own_recorded = pools[key]
        res = classify_transfer(
            transfer_amount_idr=tx["amount_idr"],
            transfer_date=tx["txn_date"],
            payer_memo=tx["description"],
            open_invoices=own_open,
            recorded_invoices=own_recorded,
        )
        items.append(
            {
                "txn_id": tx["id"],
                "txn_date": tx["txn_date"],
                "amount_idr": tx["amount_idr"],
                "description": tx["description"],
                "company_key": tx["company_key"],
                "light": res.light,
                "open_candidates": res.open_candidates,
                "open_strong": res.open_strong,
                "recorded_candidates": res.recorded_candidates,
                "recorded_strong": res.recorded_strong,
                "suggestion": _candidate_json(res.suggestion) if res.suggestion else None,
            }
        )
    return items


@router.get("/reconciliation-inbox")
async def reconciliation_inbox(
    limit: int = Query(200, ge=1, le=1000),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Unmatched incoming transfers, newest first, each with its light and company. Advice only."""
    _require_accounting_access(current_user)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT bt.id, bt.txn_date, bt.amount_idr, bt.description, bs.account_number
            FROM bank_transactions bt
            LEFT JOIN bank_statements bs ON bs.id = bt.statement_id
            WHERE bt.direction = 'credit' AND bt.reconciled_status = 'unmatched'
            ORDER BY bt.txn_date DESC, bt.id DESC
            LIMIT $1
            """,
            limit,
        )
        open_invoices = await _fetch_invoice_pool(conn, recorded=False)
        recorded_invoices = await _fetch_invoice_pool(conn, recorded=True)
    txns = [
        {
            "id": r["id"],
            "txn_date": r["txn_date"],
            "amount_idr": int(r["amount_idr"]),
            "description": r["description"] or "",
            # Same rule as the companies strip: an account no company owns is "other".
            "company_key": company_key_for_account(r["account_number"]) or OTHER_KEY,
        }
        for r in rows
    ]
    return await asyncio.to_thread(_classify_inbox, txns, open_invoices, recorded_invoices)


@router.get("/outgoings")
async def outgoings(
    limit: int = Query(1000, ge=1, le=5000),
    state: Literal["to_justify", "justified"] | None = Query(None),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Debits newest first, each with its state and justification, bank text read apart."""
    _require_accounting_access(current_user)
    async with db_pool.acquire() as conn:
        if state is None:
            return await load_outgoings(conn, limit=limit)
        return await load_outgoings(conn, limit=limit, state=state)


class JustifyLine(BaseModel):
    amount_idr: int = Field(gt=0)
    category: str
    note: str
    practice_id: int | None = Field(None, gt=0)


class JustifyOutgoingRequest(BaseModel):
    category: str
    practice_id: int | None = Field(None, gt=0)
    recipient: str | None = None
    note: str | None = None
    lines: list[JustifyLine] | None = None


@router.post("/outgoings/{txn_id}/justify")
async def justify_outgoing_route(
    txn_id: int,
    payload: JustifyOutgoingRequest,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Justify one bank debit (append-only log). Writes nothing to the cash-out."""
    _require_accounting_access(current_user)
    try:
        async with db_pool.acquire() as conn:
            return await justify_outgoing(
                conn,
                txn_id=txn_id,
                category=payload.category,
                practice_id=payload.practice_id,
                recipient=payload.recipient,
                note=payload.note,
                lines=[ln.model_dump() for ln in payload.lines] if payload.lines else None,
                justified_by=(current_user.get("email") or "unknown").lower(),
            )
    except OutgoingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except asyncpg.UniqueViolationError as exc:  # a parallel justify won the race
        raise HTTPException(
            status_code=409, detail="this debit is already justified: undo it first"
        ) from exc
    except Exception as exc:  # total: a driver's own text can quote row values
        _fail_action(exc)


@router.post("/outgoings/{txn_id}/undo")
async def undo_outgoing_justification(
    txn_id: int,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Take back a justification made today (WITA), as undo-confirmation does. Deletes nothing."""
    _require_accounting_access(current_user)
    try:
        async with db_pool.acquire() as conn:
            return await undo_justification(
                conn, txn_id=txn_id, undone_by=(current_user.get("email") or "unknown").lower()
            )
    except OutgoingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except Exception as exc:  # total: a driver's own text can quote row values
        _fail_action(exc)


@router.get("/companies")
async def companies(
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """The issuing companies with bank, account and how far their imported statements reach."""
    _require_accounting_access(current_user)
    async with db_pool.acquire() as conn:
        return await load_companies(conn)


def _wita_now() -> dt.datetime:
    return dt.datetime.now(BUSINESS_TZ)


@router.get("/done-today")
async def done_today(
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Transfers handled today (WITA): counts by action and the latest items. Read-only."""
    _require_accounting_access(current_user)
    now = _wita_now()
    async with db_pool.acquire() as conn:
        return await load_done_today(conn, now)


# JSON integers only (A8): a boolean, a string or a number with a fraction is a 422.
_StrictId = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
_StrictBigId = Annotated[int, Field(strict=True, ge=1, le=9_223_372_036_854_775_807)]


class LinkRecordedRequest(BaseModel):
    bank_txn_id: _StrictBigId
    practice_id: _StrictId
    invoice_id: _StrictId


class IgnoreTransactionRequest(BaseModel):
    bank_txn_id: _StrictBigId
    # No length or pattern here on purpose: the framework echoes a constrained value that
    # fails, and the service refuses an unfit reason with fixed text instead.
    reason: str


class ReopenTransactionRequest(BaseModel):
    bank_txn_id: _StrictBigId


_ACTION_FAILED = "reconciliation action could not be completed"


def _fail_action(exc: Exception) -> NoReturn:
    logger.error("accounting reconciliation action failed (%s)", type(exc).__name__)
    raise HTTPException(status_code=500, detail=_ACTION_FAILED) from None


def _action_json(result: reconcile_service.ActionResult) -> dict[str, Any]:
    return {
        "bank_txn_id": result.bank_txn_id,
        "reconciled_status": result.reconciled_status,
        "reconciliation_log_id": result.reconciliation_log_id,
    }


@router.post("/link-recorded")
async def link_recorded(
    payload: LinkRecordedRequest,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Link an unmatched credit to a payment its practice already records; it enters the cash-out."""
    _require_accounting_access(current_user)
    try:
        async with db_pool.acquire() as conn:
            result = await reconcile_service.link_recorded(
                conn,
                bank_txn_id=payload.bank_txn_id,
                practice_id=payload.practice_id,
                invoice_id=payload.invoice_id,
                linked_by=(current_user.get("email") or "unknown").lower(),
            )
    except ReconcileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # total: a driver's own text can quote row values
        _fail_action(exc)
    return _action_json(result)


@router.post("/ignore-transaction")
async def ignore_transaction(
    payload: IgnoreTransactionRequest,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Mark an unmatched transfer as not a client payment. Moves no money."""
    _require_accounting_access(current_user)
    try:
        async with db_pool.acquire() as conn:
            result = await reconcile_service.ignore_transaction(
                conn,
                bank_txn_id=payload.bank_txn_id,
                reason=payload.reason,
                ignored_by=(current_user.get("email") or "unknown").lower(),
            )
    except ReconcileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # total: a driver's own text can quote row values
        _fail_action(exc)
    return _action_json(result)


@router.post("/reopen-transaction")
async def reopen_transaction(
    payload: ReopenTransactionRequest,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Put a linked or ignored transfer back to unmatched. Never reopens a confirmed one."""
    _require_accounting_access(current_user)
    try:
        async with db_pool.acquire() as conn:
            result = await reconcile_service.reopen_transaction(
                conn,
                bank_txn_id=payload.bank_txn_id,
                reopened_by=(current_user.get("email") or "unknown").lower(),
            )
    except ReconcileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # total: a driver's own text can quote row values
        _fail_action(exc)
    return _action_json(result)


@router.post("/undo-confirmation")
async def undo_confirmation(
    payload: ReopenTransactionRequest,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Owner rule (b): take back a payment confirmed today (WITA). Sends no email."""
    _require_accounting_access(current_user)
    try:
        async with db_pool.acquire() as conn:
            result = await reconcile_service.undo_confirmation(
                conn,
                bank_txn_id=payload.bank_txn_id,
                undone_by=(current_user.get("email") or "unknown").lower(),
            )
    except ReconcileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # total: a driver's own text can quote row values
        _fail_action(exc)
    return _action_json(result)


# ── P3: Google Sheets export ────────────────────────────────────────────────

# The service account cannot CREATE a Drive file (no Drive quota): Zero creates
# the target spreadsheet, shares it with the SA email as Editor, and sets its id
# in ACCOUNTING_EXPORT_SHEET_ID. The app only writes into that existing sheet.
EXPORT_SHEET_ID_ENV = "ACCOUNTING_EXPORT_SHEET_ID"
# Which tab to write into. Defaults to "Cashout"; override via env for a fresh
# sheet whose tab is still "Sheet1"/"Foglio1" (no need to rename the tab).
EXPORT_SHEET_TAB_ENV = "ACCOUNTING_EXPORT_SHEET_TAB"
EXPORT_SHEET_TAB_DEFAULT = "Cashout"


def _export_range() -> str:
    """Build the A1 write anchor for the configured tab.

    Tab names with spaces or special chars must be single-quoted in A1 notation
    (e.g. 'Weekly Cashout'!A1); embedded single quotes are doubled per the API.
    """
    tab = (os.environ.get(EXPORT_SHEET_TAB_ENV, "") or EXPORT_SHEET_TAB_DEFAULT).strip()
    if not tab:
        tab = EXPORT_SHEET_TAB_DEFAULT
    safe = tab.replace("'", "''")
    return f"'{safe}'!A1"


@router.post("/export-sheet")
async def export_to_sheet(
    week_label: str | None = Query(None, description="Optional week filter"),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """On-demand push of the weekly cashout to Zero's Google Sheet.

    NOT auto-sync (Zero's decision #4): Asya clicks Export. The target sheet is
    pre-created+shared by Zero; we write into it via the existing SheetsService.
    """
    _require_accounting_access(current_user)

    spreadsheet_id = os.environ.get(EXPORT_SHEET_ID_ENV, "").strip()
    if not spreadsheet_id:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{EXPORT_SHEET_ID_ENV} is not configured. Zero must create the "
                "Google Sheet, share it with the service-account email as Editor, "
                f"and set {EXPORT_SHEET_ID_ENV}."
            ),
        )

    export_range = _export_range()

    async with db_pool.acquire() as conn:
        rows = await cashout_service.list_cashout(
            conn, week_label=week_label, limit=1000
        )
    values = cashout_service.build_export_rows(rows)

    try:
        from backend.services.integrations.sheets_service import SheetsService

        svc = SheetsService()
        result = await svc.write_range(spreadsheet_id, export_range, values)
    except FileNotFoundError as exc:
        # SA credentials not present in this environment.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # surface the Sheets API failure as 502
        logger.error("accounting export-sheet failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=(
                "Google Sheets write failed. Verify the sheet is shared with the "
                f"service account and the tab {export_range.split('!')[0]} exists "
                f"(override with {EXPORT_SHEET_TAB_ENV}). ({exc})"
            ),
        ) from exc

    # values includes the header row; data rows = len-1.
    return {
        "spreadsheet_id": spreadsheet_id,
        "rows_exported": max(len(values) - 1, 0),
        "updated_cells": result.get("updatedCells", 0),
        "range": export_range,
    }
