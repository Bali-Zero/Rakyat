"""Pro→Fly CRM delivery for committed intake documents.

When a reviewer APPROVES an intake proposal with the writer armed, the local
(Pro Postgres) commit is the source of truth for the approve outcome. This
module is the best-effort DELIVERY leg that runs AFTER that commit: it pushes
the original blob to the existing Fly base64 upload endpoint
(``POST /api/crm/clients/{client_id}/documents/upload`` for reviewer JWTs, or
``POST /api/crm/internal/clients/{client_id}/documents/upload`` for scoped WA
Mirror service writes), which resolves/creates the client's Google Drive folder,
uploads the file, INSERTs the canonical ``documents`` row on the Fly Postgres
(the CRM kita reads) and dispatches OCR.

PII note: the blob transits Pro→Fly→Google Drive. That is the CRM's sanctioned
storage path — identical to a manual upload from the kita frontend — so this
does not widen the PII surface beyond what every manual document upload
already does (Law 2: the intake REVIEW data itself stays on the Pro; only the
approved document travels, authenticated with the reviewer's own JWT).

Failure policy: :func:`push_committed_document` NEVER raises for delivery
failures — it always returns a :class:`CrmPushResult`. The caller treats the
local commit as final and surfaces the push status in the approve response +
audit trail.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from backend.phone_lock import phone_core
from backend.security.pii_log_identifier import redact_identifier_for_log

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://nuzantara-rag.fly.dev"
DEFAULT_MAX_MB = 9.0  # raw bytes; base64 inflates ~33% against the endpoint's ~10MB class limit
_REQUEST_TIMEOUT_SECONDS = 60.0

# Google Drive webViewLink → file id (best-effort: the Fly endpoint's response
# carries only document_id + file_url, not the bare Drive file id).
_DRIVE_FILE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{10,})")

# A 429 is the server asking for a pause, not a verdict on the request: it gets ONE retry
# after a short wait, out of the same single retry a transport error or a 5xx gets. The
# wait is the response's Retry-After kept within 1..5 seconds; a missing or unreadable
# value waits the default. Bounded on purpose: a delivery never sleeps through a whole
# rate-limit window, and a retry that is refused again ends as a refused request always
# ended here.
_RATE_LIMIT_WAIT_DEFAULT_SECONDS = 2.0
_RATE_LIMIT_WAIT_MAX_SECONDS = 5.0


# The longest run of digits that is ever converted to a number. The cap is far below
# a thousand, so a longer number is over it whatever it is; and int() refuses a run of
# more than 4,300 digits, which a header is free to carry.
_RATE_LIMIT_WAIT_MAX_DIGITS = 3


def _rate_limit_wait_seconds(resp: httpx.Response) -> float:
    """Seconds to wait before the one retry a 429 gets. Never raises."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not (raw.isascii() and raw.isdigit()):
        return _RATE_LIMIT_WAIT_DEFAULT_SECONDS
    digits = raw.lstrip("0")
    if not digits:
        return _RATE_LIMIT_WAIT_DEFAULT_SECONDS
    if len(digits) > _RATE_LIMIT_WAIT_MAX_DIGITS:
        return _RATE_LIMIT_WAIT_MAX_SECONDS
    return float(min(int(digits), _RATE_LIMIT_WAIT_MAX_SECONDS))


def _http_failure_detail(status_code: int) -> str:
    """The detail of a failed CRM answer: its status and fixed text.

    The body of the answer is never read into a detail or a log line: an error page can quote
    the uploaded file's name or the server's exception text.
    """
    if status_code in (401, 403):
        reason = "the CRM refused the credential"
    elif status_code == 429:
        reason = "the CRM is rate limiting"
    elif status_code >= 500:
        reason = "the CRM failed to answer"
    else:
        reason = "the CRM rejected the request"
    return f"HTTP {status_code}: {reason}"


def _transport_failure_detail(exc: BaseException) -> str:
    """The detail of a call that got no answer: the error's type and fixed text, never its text."""
    return f"{type(exc).__name__}: the CRM could not be reached"


def push_enabled() -> bool:
    """Kill-switch — ``INTAKE_CRM_PUSH_ENABLED`` (default ON)."""
    return os.environ.get("INTAKE_CRM_PUSH_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _push_base_url() -> str:
    return os.environ.get("INTAKE_CRM_PUSH_BASE_URL", "").strip() or DEFAULT_BASE_URL


def _max_push_bytes() -> int:
    """Raw-blob size guard in bytes (env ``INTAKE_CRM_PUSH_MAX_MB``, default 9 MB)."""
    raw = os.environ.get("INTAKE_CRM_PUSH_MAX_MB", "").strip()
    try:
        mb = float(raw) if raw else DEFAULT_MAX_MB
    except ValueError:
        mb = DEFAULT_MAX_MB
    return int(mb * 1024 * 1024)


# --------------------------------------------------------------------------- #
# Persistent HTTP client (Golden Rule 10) — lazy module singleton.
# --------------------------------------------------------------------------- #
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS))
    return _client


async def close_client() -> None:
    """Close the module HTTP client (called from the app lifespan shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass
class CrmPushResult:
    """Outcome of one Pro→Fly document delivery attempt."""

    ok: bool
    status: str  # pushed | too_large | denied_rbac | rejected | server_error |
    #              unreachable | no_token | missing_blob | error |
    #              identity_unresolved | practice_identity_unresolved |
    #              company_identity_unresolved
    fly_doc_id: int | None = None
    fly_company_doc_id: int | None = None
    file_id: str | None = None
    file_url: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def crm_write_key_from_env() -> str | None:
    """Scoped CRM service-write key from the environment, if configured.

    Needed even on the bearer path: identity RESOLUTION (upsert-by-phone) is a
    service-key operation regardless of which token authorizes the upload.
    """
    for name in ("INTAKE_CRM_PUSH_WRITE_KEY", "WA_MIRROR_CRM_WRITE_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _parse_drive_file_id(file_url: str | None) -> str | None:
    if not file_url:
        return None
    m = _DRIVE_FILE_ID_RE.search(file_url)
    return m.group(1) if m else None


async def _ensure_client_on_fly(
    *,
    crm_write_key: str,
    sender_phone: str,
    full_name: str | None,
    base_url: str,
) -> int | None:
    """Resolve/create the Fly client for a WA sender phone, returning its FLY id.

    The local Pro ``nuzantara_dev`` DB and the Fly ``nuzantara_rag`` DB do NOT
    share a client_id namespace: leads minted locally by the wa-mirror sweeper
    (id ≥ ~80k) have no row on Fly, so addressing the upload endpoint with the
    LOCAL pk yields ``404 Client not found``. This calls the idempotent,
    phone-keyed Fly upsert (``POST /api/crm/clients/upsert-by-phone``, scoped
    ``X-CRM-Write-Key``) which returns the client's id ON FLY — the only id valid
    against the upload endpoint. Only derived contact fields (normalized phone +
    name) cross the boundary; no raw WhatsApp content (Law 2 — the upsert model's
    own contract enforces this).

    Returns the Fly client_id, or ``None`` if the upsert could not resolve/create
    one (no phone, denied, unreachable). Never raises — delivery stays best-effort.
    """
    digits = re.sub(r"\D", "", sender_phone or "")
    if not (6 <= len(digits) <= 20):
        return None
    body: dict[str, Any] = {
        "phone_normalized": digits,
        "create_if_missing": True,
        "lead_source": "whatsapp_auto",
        # Resolution must be mutation-safe on shared phones: the endpoint
        # refuses BEFORE any restore/rename when >1 rows match (F10).
        "reject_ambiguous": True,
        # NEVER resurrect an archived Fly row during identity resolution —
        # the archived owner of this phone may be a DIFFERENT person than
        # the locally-selected client (Codex round 8, F11 archive gap).
        "restore_if_archived": False,
    }
    if full_name and full_name.strip():
        body["full_name"] = full_name.strip()
    url = f"{base_url}/api/crm/clients/upsert-by-phone"
    headers = {"X-CRM-Write-Key": crm_write_key}
    client = _get_client()
    for attempt in (0, 1):
        try:
            resp = await client.post(url, json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                "intake.crm_push.upsert_unreachable phone=%s err=%s",
                redact_identifier_for_log(digits),
                type(exc).__name__,
            )
            return None
        if resp.status_code != 429 or attempt == 1:
            break
        wait = _rate_limit_wait_seconds(resp)
        logger.warning("intake.crm_push.upsert_rate_limited — retrying once in %.0fs", wait)
        await asyncio.sleep(wait)
    if resp.status_code >= 400:
        logger.warning("intake.crm_push.upsert_failed status=%s", resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    fly_id = data.get("client_id") if isinstance(data, dict) else None
    if fly_id is None:
        return None
    if isinstance(data, dict) and data.get("was_archived"):
        # The sole Fly match is a soft-deleted row (restore disabled above):
        # an archived card cannot prove it is the locally-selected person —
        # uploading would resurrect-and-attach to a possibly different
        # identity. Fail closed (Codex round 8, F11 archive gap).
        logger.warning(
            "intake.crm_push.upsert_archived_match fly_client=%s — refusing delivery",
            fly_id,
        )
        return None
    matched_count = data.get("matched_count") if isinstance(data, dict) else None
    if isinstance(matched_count, int) and matched_count > 1:
        # The phone is SHARED (spouse/agent/office line — 51 live groups on the
        # Fly book): the endpoint picked one row arbitrarily, which is exactly
        # the wrong-client delivery vector. Fail closed — a shared phone cannot
        # resolve an identity (Codex 2026-07-19 round 5, F7).
        logger.warning(
            "intake.crm_push.upsert_ambiguous matched=%s — refusing delivery "
            "(shared phone cannot resolve a single Fly client)",
            matched_count,
        )
        return None
    logger.info(
        "intake.crm_push.upsert_ok fly_client=%s created=%s matched=%s",
        fly_id,
        (data.get("was_created") if isinstance(data, dict) else None),
        matched_count,
    )
    return int(fly_id)


async def push_committed_document(
    *,
    bearer_token: str | None,
    crm_write_key: str | None = None,
    client_id: int,
    file_name: str,
    document_type: str,
    blob_path: str,
    practice_id: int | None = None,
    practice_uuid: str | None = None,
    company_id: int | None = None,
    company_uuid: str | None = None,
    intake_idempotency_key: str | None = None,
    document_category: str | None = None,
    mime_type: str | None = None,
    expiry_date: str | None = None,
    notes: str | None = None,
    family_member_id: int | None = None,
    base_url: str | None = None,
    sender_phone: str | None = None,
    client_full_name: str | None = None,
    client_visible: bool | None = None,
) -> CrmPushResult:
    """Deliver one committed intake blob to the Fly CRM upload endpoint.

    Matches ``DocumentUploadBase64`` (crm_enhanced_documents.py) exactly:
    ``file`` (base64), ``file_name``, ``document_type`` + the optional
    ``mime_type`` / ``notes`` / ``document_category`` / ``expiry_date`` /
    ``family_member_id`` / ``practice_id`` / ``company_id`` /
    ``intake_idempotency_key`` / ``client_visible``. Fields the endpoint does
    not accept (e.g. extracted_fields) are deliberately NOT sent.

    A practice or company attachment requires its canonical UUID from the
    replica. The UUID string travels on ``practice_id`` / ``company_id`` so an
    older integer-only endpoint rejects the request before uploading.

    Auth: prefer the reviewer's own ``Authorization: Bearer <jwt>``. Headless
    auto-attach can instead pass ``crm_write_key`` for the scoped internal upload
    endpoint gated by ``WA_MIRROR_CRM_WRITE_ENABLED``.

    Retry: ONE retry on transient failures (connect/read errors, 5xx).
    401/403 → ``denied_rbac`` (no retry). Other 4xx → ``rejected``.
    NEVER raises for delivery failures — always returns a CrmPushResult.
    """
    if not bearer_token and not crm_write_key:
        return CrmPushResult(
            ok=False,
            status="no_token",
            detail="no bearer token or CRM service-write key available",
        )

    if practice_id is not None and practice_uuid is None:
        return CrmPushResult(
            ok=False,
            status="practice_identity_unresolved",
            detail="a local numeric practice id cannot identify a canonical practice",
        )
    if practice_uuid is not None:
        try:
            practice_uuid = str(UUID(str(practice_uuid)))
        except ValueError:
            return CrmPushResult(
                ok=False,
                status="practice_identity_unresolved",
                detail="invalid canonical practice UUID",
            )

    if company_id is not None and company_uuid is None:
        return CrmPushResult(
            ok=False,
            status="company_identity_unresolved",
            detail="a local numeric company id cannot identify a canonical company",
        )
    if company_uuid is not None:
        try:
            company_uuid = str(UUID(str(company_uuid)))
        except ValueError:
            return CrmPushResult(
                ok=False,
                status="company_identity_unresolved",
                detail="invalid canonical company UUID",
            )

    path = Path(blob_path)
    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        return CrmPushResult(
            ok=False, status="missing_blob", detail=f"{type(exc).__name__}: blob could not be read"
        )

    max_bytes = _max_push_bytes()
    if len(raw) > max_bytes:
        return CrmPushResult(
            ok=False,
            status="too_large",
            detail=f"blob {len(raw)} bytes exceeds limit {max_bytes} bytes",
        )

    payload: dict[str, Any] = {
        "file": base64.b64encode(raw).decode("ascii"),
        "file_name": file_name,
        "document_type": document_type,
    }
    optional_fields: dict[str, Any] = {
        "mime_type": mime_type,
        "notes": notes,
        "document_category": document_category,
        "expiry_date": expiry_date,
        "family_member_id": family_member_id,
        # Keep existing wire keys: old integer-only servers reject a UUID
        # before upload instead of silently ignoring an added field.
        "practice_id": practice_uuid,
        "company_id": company_uuid,
        "intake_idempotency_key": intake_idempotency_key,
        "client_visible": client_visible,
    }
    payload.update({key: value for key, value in optional_fields.items() if value is not None})

    base = (base_url or _push_base_url()).rstrip("/")

    def _build_request(cid: int) -> tuple[str, dict[str, str]]:
        if bearer_token:
            return (
                f"{base}/api/crm/clients/{cid}/documents/upload",
                {"Authorization": f"Bearer {bearer_token}"},
            )
        return (
            f"{base}/api/crm/internal/clients/{cid}/documents/upload",
            {"X-CRM-Write-Key": crm_write_key or ""},
        )

    if not sender_phone:
        return CrmPushResult(
            ok=False,
            status="identity_unresolved",
            detail=(
                "no sender phone to resolve the Fly client — refusing to "
                "address Fly by the LOCAL pk"
            ),
        )
    resolution_key = crm_write_key or crm_write_key_from_env()
    if not resolution_key:
        return CrmPushResult(
            ok=False,
            status="identity_unresolved",
            detail=(
                "no CRM service key available to resolve the Fly identity — "
                "refusing to address Fly by the LOCAL pk"
            ),
        )
    fly_cid = await _ensure_client_on_fly(
        crm_write_key=resolution_key,
        sender_phone=sender_phone,
        full_name=client_full_name,
        base_url=base,
    )
    if fly_cid is None:
        return CrmPushResult(
            ok=False,
            status="identity_unresolved",
            detail="phone-upsert could not resolve an UNAMBIGUOUS Fly client id",
        )
    target_client_id = fly_cid

    _expected_core = phone_core(sender_phone)
    if _expected_core:
        payload["expected_phone_core"] = _expected_core

    url, headers = _build_request(target_client_id)
    client = _get_client()

    try:
        last_detail: str | None = None
        for attempt in (0, 1):
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_detail = _transport_failure_detail(exc)
                if attempt == 0:
                    logger.warning(
                        "intake.crm_push.transient client=%s err=%s — retrying once",
                        client_id,
                        last_detail,
                    )
                    continue
                return CrmPushResult(ok=False, status="unreachable", detail=last_detail)

            if resp.status_code in (401, 403):
                return CrmPushResult(
                    ok=False,
                    status="denied_rbac",
                    detail=_http_failure_detail(resp.status_code),
                )
            if resp.status_code == 429 and attempt == 0:
                wait = _rate_limit_wait_seconds(resp)
                logger.warning(
                    "intake.crm_push.rate_limited client=%s — retrying once in %.0fs",
                    client_id,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 500:
                last_detail = _http_failure_detail(resp.status_code)
                if attempt == 0:
                    logger.warning(
                        "intake.crm_push.5xx client=%s detail=%s — retrying once",
                        client_id,
                        last_detail,
                    )
                    continue
                return CrmPushResult(ok=False, status="server_error", detail=last_detail)
            if resp.status_code == 409:
                try:
                    error = resp.json()
                except ValueError:
                    error = None
                detail_str = str(error.get("detail") or "") if isinstance(error, dict) else ""
                if detail_str == "practice_identity_unresolved":
                    return CrmPushResult(
                        ok=False,
                        status="practice_identity_unresolved",
                        detail="canonical practice identity could not be verified for this client",
                    )
                if detail_str.startswith("company_identity_unresolved"):
                    return CrmPushResult(
                        ok=False,
                        status="company_identity_unresolved",
                        detail="canonical company identity could not be verified for this client",
                    )
            if resp.status_code >= 400:
                return CrmPushResult(
                    ok=False,
                    status="rejected",
                    detail=_http_failure_detail(resp.status_code),
                )

            # 2xx — response shape: {"success", "document_id", "company_document_id", "file_url", ...}
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            fly_doc_id = data.get("document_id")
            fly_company_doc_id = data.get("company_document_id")
            file_url = data.get("file_url")
            logger.info(
                "intake.crm_push.pushed client=%s fly_doc=%s fly_company_doc=%s file_url=%s",
                client_id,
                fly_doc_id,
                fly_company_doc_id,
                bool(file_url),
            )
            return CrmPushResult(
                ok=True,
                status="pushed",
                fly_doc_id=int(fly_doc_id) if fly_doc_id is not None else None,
                fly_company_doc_id=(
                    int(fly_company_doc_id) if fly_company_doc_id is not None else None
                ),
                file_id=data.get("file_id") or _parse_drive_file_id(file_url),
                file_url=file_url,
            )
        # Unreachable: the loop always returns. Defensive fallthrough.
        return CrmPushResult(ok=False, status="error", detail=last_detail or "exhausted retries")
    except Exception as exc:  # delivery must never break the approve
        # The type alone: a traceback ends with the exception's text, which can quote a path.
        logger.error("intake.crm_push.unexpected client=%s err=%s", client_id, type(exc).__name__)
        return CrmPushResult(
            ok=False, status="error", detail=f"{type(exc).__name__}: unexpected delivery error"
        )


async def push_committed_company_document(
    *,
    bearer_token: str | None,
    crm_write_key: str | None = None,
    company_id: int | None = None,
    company_uuid: str | None,
    file_name: str,
    document_type: str,
    blob_path: str,
    document_category: str | None = None,
    document_number: str | None = None,
    mime_type: str | None = None,
    expiry_date: str | None = None,
    notes: str | None = None,
    base_url: str | None = None,
    client_visible: bool | None = None,
    intake_idempotency_key: str | None = None,
) -> CrmPushResult:
    """Deliver a standalone company document via canonical company UUID on Fly."""
    if not bearer_token and not crm_write_key:
        return CrmPushResult(
            ok=False,
            status="no_token",
            detail="no bearer token or CRM service-write key available",
        )
    if not company_uuid:
        return CrmPushResult(
            ok=False,
            status="company_identity_unresolved",
            detail="local company lacks canonical UUID",
        )
    try:
        canonical_uuid = str(UUID(str(company_uuid)))
    except ValueError:
        return CrmPushResult(
            ok=False,
            status="company_identity_unresolved",
            detail="invalid canonical company UUID",
        )

    path = Path(blob_path)
    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        return CrmPushResult(
            ok=False, status="missing_blob", detail=f"{type(exc).__name__}: blob could not be read"
        )

    max_bytes = _max_push_bytes()
    if len(raw) > max_bytes:
        return CrmPushResult(
            ok=False,
            status="too_large",
            detail=f"blob {len(raw)} bytes exceeds limit {max_bytes} bytes",
        )

    payload: dict[str, Any] = {
        "file": base64.b64encode(raw).decode("ascii"),
        "file_name": file_name,
        "document_type": document_type,
    }
    optional_fields: dict[str, Any] = {
        "mime_type": mime_type,
        "notes": notes,
        "document_category": document_category,
        "document_number": document_number,
        "expiry_date": expiry_date,
        "client_visible": client_visible,
        "intake_idempotency_key": intake_idempotency_key,
    }
    payload.update({key: value for key, value in optional_fields.items() if value is not None})

    base = (base_url or _push_base_url()).rstrip("/")
    using_bearer = bool(bearer_token)
    if using_bearer:
        url = f"{base}/api/crm/companies/{canonical_uuid}/documents/upload"
        headers = {"Authorization": f"Bearer {bearer_token}"}
    else:
        url = f"{base}/api/crm/internal/companies/{canonical_uuid}/documents/upload"
        headers = {"X-CRM-Write-Key": crm_write_key or ""}

    client = _get_client()
    try:
        last_detail: str | None = None
        fallback_service_key_used = False
        attempt = 0
        while attempt < 2:
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_detail = _transport_failure_detail(exc)
                if attempt == 0:
                    logger.warning(
                        "intake.crm_push.company_transient company=%s err=%s — retrying once",
                        company_id,
                        last_detail,
                    )
                    attempt += 1
                    continue
                return CrmPushResult(ok=False, status="unreachable", detail=last_detail)

            if resp.status_code in (401, 403):
                if using_bearer and crm_write_key and not fallback_service_key_used:
                    fallback_service_key_used = True
                    using_bearer = False
                    url = f"{base}/api/crm/internal/companies/{canonical_uuid}/documents/upload"
                    headers = {"X-CRM-Write-Key": crm_write_key}
                    continue
                return CrmPushResult(
                    ok=False,
                    status="denied_rbac",
                    detail=_http_failure_detail(resp.status_code),
                )
            if resp.status_code == 429 and attempt == 0:
                wait = _rate_limit_wait_seconds(resp)
                logger.warning(
                    "intake.crm_push.company_rate_limited company=%s — retrying once in %.0fs",
                    company_id,
                    wait,
                )
                await asyncio.sleep(wait)
                attempt += 1
                continue
            if resp.status_code >= 500:
                last_detail = _http_failure_detail(resp.status_code)
                if attempt == 0:
                    logger.warning(
                        "intake.crm_push.company_5xx company=%s detail=%s — retrying once",
                        company_id,
                        last_detail,
                    )
                    attempt += 1
                    continue
                return CrmPushResult(ok=False, status="server_error", detail=last_detail)
            if resp.status_code == 409:
                try:
                    error = resp.json()
                except ValueError:
                    error = None
                detail_str = str(error.get("detail") or "") if isinstance(error, dict) else ""
                if detail_str.startswith("company_identity_unresolved"):
                    return CrmPushResult(
                        ok=False,
                        status="company_identity_unresolved",
                        detail="canonical company identity could not be verified",
                    )
            if resp.status_code >= 400:
                return CrmPushResult(
                    ok=False,
                    status="rejected",
                    detail=_http_failure_detail(resp.status_code),
                )

            try:
                data = resp.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            fly_company_doc_id = data.get("company_document_id")
            file_url = data.get("file_url")
            file_id = data.get("file_id") or _parse_drive_file_id(file_url)
            logger.info(
                "intake.crm_push.company_pushed company=%s fly_company_doc=%s file_url=%s",
                company_id,
                fly_company_doc_id,
                bool(file_url),
            )
            return CrmPushResult(
                ok=True,
                status="pushed",
                fly_company_doc_id=(
                    int(fly_company_doc_id) if fly_company_doc_id is not None else None
                ),
                file_id=file_id,
                file_url=file_url,
            )
        return CrmPushResult(ok=False, status="error", detail=last_detail or "exhausted retries")
    except Exception as exc:
        logger.error(
            "intake.crm_push.company_unexpected company=%s err=%s",
            company_id,
            type(exc).__name__,
        )
        return CrmPushResult(
            ok=False, status="error", detail=f"{type(exc).__name__}: unexpected delivery error"
        )
