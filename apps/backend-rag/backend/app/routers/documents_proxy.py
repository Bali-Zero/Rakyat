"""
Document Proxy Router

Serves Google Drive documents/images through backend proxy to avoid Google branding.
Used for passport and document previews in CRM.
"""

import asyncio
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from google.auth.transport import requests as google_auth_requests

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.utils.service_accounts import CLIENT_ROLES, EXTERNAL_ROLES, normalize_role
from backend.services.integrations.service_account_drive_service import (
    ServiceAccountDriveService,
)
from backend.services.portal._document_visibility import document_visibility_clause

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["Documents Proxy"])

# A Google Drive file ID is an opaque base64url-ish token. Anchoring the shape
# is the first half of the fix: `file_id` is interpolated straight into
# `https://www.googleapis.com/drive/v3/files/{file_id}`, so a value carrying
# `/`, `?` or `..` does not address a FILE — it rewrites the API path and can
# reach other Drive endpoints entirely (CodeQL py/partial-ssrf).
_DRIVE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,256}$")

# Fields the metadata call must return. `parents` is load-bearing: it is what
# ties a file to the client folder that authorises reading it.
_METADATA_FIELDS = "mimeType,name,size,parents"

_MAX_PROXY_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MB memory/OOM bound
_SAFE_INLINE_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/webp",
    }
)
_SAFE_FILENAME_CHAR_RE = re.compile(r"[^A-Za-z0-9._ -]")


def _sanitize_disposition_filename(raw_name: str | None) -> str:
    """Return an ASCII-safe basename stripped of Content-Disposition injection chars."""
    base = (raw_name or "").replace("\\", "/").split("/")[-1]
    cleaned = ["_" if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in base]
    safe = _SAFE_FILENAME_CHAR_RE.sub("_", "".join(cleaned)).strip(" .")
    return safe[:150] or "document"


def _resolve_proxy_content_headers(
    mime_type: str | None,
    file_name: str | None,
) -> tuple[str, dict[str, str]]:
    """Force active/unknown MIME types to download as attachment with nosniff."""
    essence = (mime_type or "application/octet-stream").split(";", 1)[
        0
    ].strip().lower() or "application/octet-stream"
    safe_name = _sanitize_disposition_filename(file_name)
    if essence in _SAFE_INLINE_MIMES:
        media_type = essence
        disposition = f'inline; filename="{safe_name}"'
    else:
        media_type = "application/octet-stream"
        disposition = f'attachment; filename="{safe_name}"'

    headers = {
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
    }
    return media_type, headers


def _parse_positive_int(raw_value: Any) -> int | None:
    """Best-effort conversion of Drive size / Content-Length header to non-negative int."""
    if raw_value is None:
        return None
    try:
        parsed = int(raw_value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _assert_file_id_shape(file_id: str) -> None:
    """Reject anything that is not a bare Drive file ID, before any network call."""
    if not _DRIVE_FILE_ID_RE.match(file_id or ""):
        raise HTTPException(status_code=400, detail="Malformed Drive file id")


# Half one of the authorisation predicate: is this ID one the CRM registered?
#
# Registry FIRST, folder second — and that order is a measurement, not a style
# choice. Client documents do NOT live directly in the client folder: the Drive
# layout is `<client folder>/01_Immigration/Actual Visa/…`
# (`service_account_drive_service.py:286-288`), so a document's immediate
# `parents` is a SUBFOLDER, which is not registered anywhere. A guard that asked
# only "is a parent a known client folder?" would 403 nearly every real document
# in the CRM viewer. Measured 2026-07-31 on the live DB: 18,749 of 18,762
# `documents` rows carry `file_id`, 56,975 of 56,987 `company_documents` rows
# carry `google_drive_file_id` — the registry is the surface that actually
# answers.
#
# The URL branches cover the 10 `documents` rows whose ID survives only inside a
# URL, and they are ANCHORED to `/d/` and `id=` — the two forms the frontend
# itself parses. A bare `strpos(url, $1)` would authorise any ID that merely
# occurs inside a stored URL, which is a shorter ID passing on the strength of a
# longer one. (Anchored `strpos`, never `LIKE '%'||$1||'%'`: a Drive ID
# legitimately contains `_`, which LIKE reads as a single-character wildcard.)
# Verified on live data: all 10 rows still authorise under the anchored form.
_REGISTERED_FILE_SQL = """
    SELECT 1
    WHERE EXISTS (SELECT 1 FROM documents WHERE file_id = $1)
       OR EXISTS (SELECT 1 FROM company_documents WHERE google_drive_file_id = $1)
       OR EXISTS (SELECT 1 FROM invoices WHERE drive_file_id = $1)
       OR EXISTS (SELECT 1 FROM lkpm_receipts WHERE file_drive_id = $1)
       OR EXISTS (SELECT 1 FROM weekly_cashout WHERE receipt_drive_file_id = $1)
       OR EXISTS (
            SELECT 1 FROM documents
             WHERE strpos(coalesce(google_drive_file_url, ''), '/d/' || $1) > 0
                OR strpos(coalesce(google_drive_file_url, ''), 'id=' || $1) > 0
                OR strpos(coalesce(file_url, ''), '/d/' || $1) > 0
                OR strpos(coalesce(file_url, ''), 'id=' || $1) > 0
          )
       OR EXISTS (
            SELECT 1 FROM company_documents
             WHERE strpos(coalesce(google_drive_file_url, ''), '/d/' || $1) > 0
                OR strpos(coalesce(google_drive_file_url, ''), 'id=' || $1) > 0
          )
"""


# Folders a file may legitimately descend from. `tax_dept_folder_id` is here
# because the first version of this guard missed it: the column enumeration that
# built the registry filtered column names on `drive`/`file_id`/`file_url`, and
# a folder column named `tax_dept_folder_id` matches none of the three. The
# search shape, not the schema, was the blind spot — a SPT/PPN/LKPM file under a
# company's tax folder would have been refused.
_FOLDER_REGISTERED_SQL = """
    SELECT 1
    WHERE EXISTS (
            SELECT 1 FROM clients
             WHERE google_drive_folder_id = ANY($1::text[])
                OR drive_folder_id = ANY($1::text[])
          )
       OR EXISTS (
            SELECT 1 FROM companies
             WHERE google_drive_folder_id = ANY($1::text[])
                OR tax_dept_folder_id = ANY($1::text[])
          )
"""

# How far up the folder tree to look for a registered client/company folder, and
# how many Drive metadata calls that search may spend in total.
#
# Bounded by the layout, not by taste: `STANDARD_SUBFOLDERS` nests at most two
# levels under the client folder (`03_Tax/SPT company`), so a file is at most
# three hops from it. Five leaves margin for a folder someone added by hand. The
# call budget bounds the amplification — an unauthorised request must not be
# able to spend an unbounded number of Google calls.
_MAX_ANCESTOR_HOPS = 5
_MAX_ANCESTOR_LOOKUPS = 8


async def _assert_file_is_ours(
    file_id: str,
    parents: list[str],
    db_pool: Any,
    fetch_parents: Any = None,
) -> None:
    """The file must be one this CRM registered, or sit in a folder it registered.

    WHY THIS EXISTS (CodeQL alerts #791/#792, triaged 2026-07-31 and confirmed by
    an independent cross-family review). Before this check, `GET /proxy/{file_id}`
    took an arbitrary Drive ID from the URL, fetched it with the SERVICE ACCOUNT's
    token, and returned the bytes. The only guard was `Depends(get_current_user)`,
    i.e. "any authenticated user", and the injected `db_pool` was never queried —
    there was not a single SELECT in this module. So any holder of a valid JWT
    could read ANY file the service account can see, and that account carries the
    full `drive` scope.

    WHAT THIS DOES AND DOES NOT CLOSE — stated plainly, because a guard trusted
    beyond its reach is worse than no guard. It bounds the blast radius from
    "every file in the Workspace the service account can reach" down to "files
    Bali Zero already holds in its CRM". Two gaps remain open ON PURPOSE:

    1. It is not a per-user gate: a team member can still read a client they are
       not assigned to. That half is blocked on data, not on code — of 12,037 rows
       in `clients`, only 2,061 carry `assigned_to` (17%, measured 2026-07-31).
       Gating on the assignee tonight would revoke document access for the other
       83% and break the CRM for every non-admin. It needs an `assigned_to`
       backfill first: an owner decision about who holds those clients.

    2. The registry is self-authorising for a caller who can WRITE. `POST
       /api/crm/clients/{id}/documents` stores `data.file_id` from the request
       body verbatim (`crm_enhanced_documents.py:123-165`), so someone with write
       access to any one client can register an arbitrary Drive ID and then read
       it here. That is a far smaller population than "any authenticated user",
       and it leaves a `documents` row behind, but it is not nothing. Closing it
       means validating provenance at WRITE time — tracked, not silently assumed.
    """
    if await _file_is_registered(file_id, db_pool):
        return
    await _assert_folder_ancestry_is_ours(file_id, parents, db_pool, fetch_parents)


async def _file_is_registered(file_id: str, db_pool: Any) -> bool:
    """Half one, and the only half the CRM viewer ever needs. Pure DB, no Drive
    call — which is why the handlers ask it BEFORE minting a token."""
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(_REGISTERED_FILE_SQL, file_id) is not None


async def _assert_folder_ancestry_is_ours(
    file_id: str,
    parents: list[str],
    db_pool: Any,
    fetch_parents: Any = None,
) -> None:
    """Half two: the file descends from a folder this CRM registered."""
    async with db_pool.acquire() as conn:
        if parents and await conn.fetchrow(_FOLDER_REGISTERED_SQL, parents) is not None:
            return

        # Not a direct child of a known folder either — walk up.
        #
        # This branch is what keeps two real features working, and neither has a
        # row in any registry: the CRM's Drive BROWSER hands back a proxy URL for
        # every file it lists (`google_drive_service.py:819-822`), and a freshly
        # UPLOADED file gets one before anything registers it (`:1060`). Both
        # live in `01_Immigration/…`-style subfolders, so a registry-plus-direct-
        # parent guard would 403 them and the feature would be reverted, not the
        # hole. The walk costs nothing on the path that carries the traffic — the
        # viewer's documents answer from the registry, without a single extra
        # Google call.
        if fetch_parents is None:
            frontier: list[str] = []
        else:
            frontier = [p for p in parents if p]
        seen: set[str] = set()
        lookups = 0

        for _hop in range(_MAX_ANCESTOR_HOPS):
            if not frontier or lookups >= _MAX_ANCESTOR_LOOKUPS:
                break
            grandparents: list[str] = []
            for folder_id in frontier:
                if folder_id in seen or lookups >= _MAX_ANCESTOR_LOOKUPS:
                    continue
                seen.add(folder_id)
                lookups += 1
                grandparents.extend(await fetch_parents(folder_id))
            frontier = [f for f in grandparents if f and f not in seen]
            if not frontier:
                break
            if await conn.fetchrow(_FOLDER_REGISTERED_SQL, frontier) is not None:
                return

    logger.warning(
        "[PROXY] denied: file %s is not registered in the CRM and no ancestor "
        "folder within %d hops belongs to a client or company",
        file_id,
        _MAX_ANCESTOR_HOPS,
    )
    raise HTTPException(status_code=403, detail="File is not in a known client folder")


def _drive_parent_fetcher(client: httpx.AsyncClient, access_token: str) -> Any:
    """Return an `async (folder_id) -> list[str]` that asks Drive for a folder's
    parents. Any failure answers "no parents", so the walk fails CLOSED rather
    than treating an API error as permission."""

    async def _fetch(folder_id: str) -> list[str]:
        if not _DRIVE_FILE_ID_RE.match(folder_id or ""):
            return []
        try:
            response = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{folder_id}",
                params={"fields": "parents"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError:
            return []
        if response.status_code != 200:
            return []
        return response.json().get("parents") or []

    return _fetch


async def _get_drive_access_token() -> str:
    """Return a fresh Bearer token from the Service Account credentials.

    Replaces the legacy `GoogleDriveService.get_valid_token(SYSTEM_USER_ID)`
    path: that path was permanently disabled (logs `OAuth SYSTEM disabled —
    Drive operations use ServiceAccountDriveService`) and returned None,
    so every thumbnail / proxy call 503'd with 'Google Drive not connected'.
    Service Account + Domain-wide Delegation is the canonical drive auth
    for the backend — same pattern as crm_enhanced.py:46-48.
    """
    drive_service = ServiceAccountDriveService()
    # google-auth's `credentials.refresh()` is sync — offload to a thread
    # so we don't block the event loop on the TLS handshake to oauth2.googleapis.com.
    if not drive_service.credentials.token:
        await asyncio.to_thread(drive_service.credentials.refresh, google_auth_requests.Request())
    token = drive_service.credentials.token
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Google Drive Service Account token refresh failed",
        )
    return token


# Half two of `_assert_file_is_ours`, scoped down for a WRITE instead of a read.
#
# `_FOLDER_REGISTERED_SQL` answers "is this file ours" against ANY client/company
# — correct for the read proxy, where the caller already holds a row that put it
# there. It is the wrong question at write time: the write endpoints below let a
# caller with access to client A submit a bare Drive `file_id` with no proof it
# was ever theirs, and "ours" would happily authorise a file that belongs to
# client B. These two queries add the missing WHERE — same shape, scoped to the
# ONE client/company the caller is actually writing to (PENDING-ARMS 2026-08-01,
# "the registry is self-authorising for a caller who can WRITE").
_CLIENT_OWNS_FOLDER_SQL = """
    SELECT 1 FROM clients
     WHERE id = $1 AND deleted_at IS NULL
       AND lower(coalesce(status, '')) <> 'archived'
       AND (google_drive_folder_id = ANY($2::text[]) OR drive_folder_id = ANY($2::text[]))
"""


# Historical/archived references still conflict: do not pick a survivor from a
# shared root. This predicate only applies to writes, never the staff reader.
_CLIENT_FOREIGN_FOLDER_SQL = """
    SELECT 1 WHERE EXISTS (
        SELECT 1 FROM clients WHERE id <> $1
          AND (google_drive_folder_id = ANY($2::text[]) OR drive_folder_id = ANY($2::text[]))
    ) OR EXISTS (
        SELECT 1 FROM companies
         WHERE google_drive_folder_id = ANY($2::text[]) OR tax_dept_folder_id = ANY($2::text[])
    )
"""

_CLIENT_ACTIVE_SQL = """
    SELECT 1 FROM clients WHERE id = $1 AND deleted_at IS NULL
      AND lower(coalesce(status, '')) <> 'archived'
"""

# Explicit file ownership outranks folder ancestry. Unknown/organizational
# references conflict with an individual-client registration. URL IDs are exact,
# so a prefix of another registered ID cannot establish ownership.
_CLIENT_FILE_OWNERSHIP_SQL = """
    WITH owners AS (
        SELECT client_id FROM documents
         WHERE file_id = $1
            OR substring(google_drive_file_url from '/d/([A-Za-z0-9_-]+)') = $1
            OR substring(google_drive_file_url from '[?&]id=([A-Za-z0-9_-]+)') = $1
            OR substring(file_url from '/d/([A-Za-z0-9_-]+)') = $1
            OR substring(file_url from '[?&]id=([A-Za-z0-9_-]+)') = $1
        UNION ALL SELECT client_id FROM invoices WHERE drive_file_id = $1
        UNION ALL SELECT client_id FROM practices
            WHERE documents->'invoice'->>'invoice_drive_id' = $1
        UNION ALL SELECT NULL::integer FROM company_documents
         WHERE google_drive_file_id = $1
            OR substring(google_drive_file_url from '/d/([A-Za-z0-9_-]+)') = $1
            OR substring(google_drive_file_url from '[?&]id=([A-Za-z0-9_-]+)') = $1
        UNION ALL SELECT NULL::integer FROM lkpm_receipts WHERE file_drive_id = $1
        UNION ALL SELECT NULL::integer FROM weekly_cashout WHERE receipt_drive_file_id = $1
    )
    SELECT coalesce(bool_or(client_id = $2), false) AS owned,
           coalesce(bool_or(client_id IS DISTINCT FROM $2), false) AS conflict
      FROM owners
"""

_COMPANY_OWNS_FOLDER_SQL = """
    SELECT 1 FROM companies
     WHERE id = $1
       AND (google_drive_folder_id = ANY($2::text[]) OR tax_dept_folder_id = ANY($2::text[]))
"""


async def _assert_file_descends_from_owner(
    file_id: str,
    conn: Any,
    *,
    owner_sql: str,
    owner_id: int,
) -> None:
    """Refuse to authorise `file_id` unless it (or an ancestor folder, within the
    same hop/lookup budget as the read guard) resolves to the folder registered
    for THIS SPECIFIC owner row — never "any client/company CRM already holds".

    Takes an ALREADY-OPEN connection, not a pool: every call site invokes this
    from inside its own `pool.acquire()` block (same shape as
    `verify_client_access(client_id, current_user, conn, ...)` in the same
    files). Re-acquiring a second connection from the same bounded pool while
    the caller's connection sits idle across a Drive HTTP round-trip is a real
    deadlock at pool saturation, not just added latency: at `max_size`
    concurrent requests each holding an outer connection, every nested acquire
    blocks forever waiting for a connection only a `release()` — which never
    happens, because the holder is itself blocked — can free.

    Runs its own metadata fetch + hop-walk rather than reusing
    `_assert_folder_ancestry_is_ours`: that helper's DB predicate is fixed to
    "any registered folder" and is shared, tested, security-critical code on
    the hot read path — narrowing it in place risks the read guard regressing
    to reject legitimate cross-client Drive-browser/upload flows it exists to
    allow. Write traffic is comparatively rare, so the extra Drive round-trip
    here costs nothing that matters.
    """
    _assert_file_id_shape(file_id)
    access_token = await _get_drive_access_token()

    async def _owned(folder_ids: list[str]) -> bool:
        if (
            owner_sql == _CLIENT_OWNS_FOLDER_SQL
            and await conn.fetchrow(_CLIENT_FOREIGN_FOLDER_SQL, owner_id, folder_ids) is not None
        ):
            raise HTTPException(status_code=403, detail="Drive folder ownership is ambiguous")
        return await conn.fetchrow(owner_sql, owner_id, folder_ids) is not None

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            meta_response = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"fields": _METADATA_FIELDS},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError:
            # Same fail-closed posture as `_drive_parent_fetcher`: a transport
            # error is never permission, and must not surface as a raw 500.
            logger.warning(
                "[PROXY-WRITE] denied: file %s metadata fetch raised a transport error",
                file_id,
            )
            raise HTTPException(
                status_code=403, detail="Drive file could not be verified"
            ) from None
        if meta_response.status_code != 200:
            # Same fail-closed posture as `_drive_parent_fetcher`: an API error
            # or a file the service account cannot see is never permission.
            logger.warning(
                "[PROXY-WRITE] denied: file %s metadata fetch returned %s",
                file_id,
                meta_response.status_code,
            )
            raise HTTPException(status_code=403, detail="Drive file could not be verified")

        parents = meta_response.json().get("parents") or []
        if parents and await _owned(parents):
            return

        fetch_parents = _drive_parent_fetcher(client, access_token)
        frontier = [p for p in parents if p]
        seen: set[str] = set()
        lookups = 0

        for _hop in range(_MAX_ANCESTOR_HOPS):
            if not frontier or lookups >= _MAX_ANCESTOR_LOOKUPS:
                break
            grandparents: list[str] = []
            for folder_id in frontier:
                if folder_id in seen or lookups >= _MAX_ANCESTOR_LOOKUPS:
                    continue
                seen.add(folder_id)
                lookups += 1
                grandparents.extend(await fetch_parents(folder_id))
            frontier = [f for f in grandparents if f and f not in seen]
            if not frontier:
                break
            if await _owned(frontier):
                return

    logger.warning(
        "[PROXY-WRITE] denied: file %s does not descend from owner_id=%s within %d hops",
        file_id,
        owner_id,
        _MAX_ANCESTOR_HOPS,
    )
    raise HTTPException(
        status_code=403,
        detail="Drive file is not in this client's own folder tree",
    )


async def assert_drive_file_belongs_to_client(
    file_id: str, client_id: int, conn: Any, *, require_ancestry: bool = False
) -> None:
    """Write-time guard for `POST .../clients/{client_id}/documents` (+ bulk
    twin) and `PATCH .../clients/{client_id}/documents/{doc_id}`: refuse a
    `file_id` with incompatible ownership. An unambiguous existing registration
    to this active client is sufficient; otherwise require its own unique Drive
    folder ancestry. Takes the caller's ALREADY-OPEN connection — see
    `_assert_file_descends_from_owner`'s docstring for why."""
    _assert_file_id_shape(file_id)
    if await conn.fetchrow(_CLIENT_ACTIVE_SQL, client_id) is None:
        raise HTTPException(status_code=403, detail="Client cannot receive documents")
    ownership = await conn.fetchrow(_CLIENT_FILE_OWNERSHIP_SQL, file_id, client_id)
    if ownership["conflict"]:
        raise HTTPException(
            status_code=403, detail="Drive file has incompatible registered ownership"
        )
    if ownership["owned"] and not require_ancestry:
        return
    await _assert_file_descends_from_owner(
        file_id, conn, owner_sql=_CLIENT_OWNS_FOLDER_SQL, owner_id=client_id
    )


async def assert_drive_file_belongs_to_company(file_id: str, company_id: int, conn: Any) -> None:
    """Write-time guard for `POST .../companies/{company_id}/documents`: refuse
    a `file_id` that does not descend from THIS company's own registered Drive
    folder. Takes the caller's ALREADY-OPEN connection — see
    `_assert_file_descends_from_owner`'s docstring for why."""
    await _assert_file_descends_from_owner(
        file_id, conn, owner_sql=_COMPANY_OWNS_FOLDER_SQL, owner_id=company_id
    )


_CLIENT_REGISTERED_FILE_SQL = f"""
    SELECT 1
    WHERE EXISTS (
            SELECT 1 FROM documents d
             WHERE d.client_id = $2
               AND {document_visibility_clause("d")}
               AND (
                   d.file_id = $1
                   OR strpos(coalesce(d.google_drive_file_url, ''), '/d/' || $1) > 0
                   OR strpos(coalesce(d.google_drive_file_url, ''), 'id=' || $1) > 0
                   OR strpos(coalesce(d.file_url, ''), '/d/' || $1) > 0
                   OR strpos(coalesce(d.file_url, ''), 'id=' || $1) > 0
               )
          )
       OR EXISTS (
            SELECT 1 FROM invoices i
             WHERE i.client_id = $2 AND i.drive_file_id = $1
          )
"""

# A portal caller is resolved by its login id alone, never by the address its session
# carries: an address can be reused by another login, a login id cannot.
_RESOLVE_PORTAL_CLIENT_ID_SQL = """
    SELECT tm.linked_client_id
      FROM team_members tm
      JOIN clients c ON c.id = tm.linked_client_id AND c.deleted_at IS NULL
     WHERE tm.id::text = $1
       AND tm.role = 'client'
       AND tm.active = true
       AND tm.portal_access = true
     LIMIT 1
"""


_MAX_CLIENT_ID = 2_147_483_647
_CLIENT_ID_TEXT = re.compile(r"[1-9][0-9]{0,9}")


def _claimed_client_id(current_user: dict[str, Any]) -> tuple[bool, int | None]:
    """Read the context's `client_id` claim as (well_formed, value).

    No key, or None, is no claim: (True, None). A claim is an int that is not a bool, or a
    string of digits with no leading zero, between 1 and 2147483647; anything else is
    malformed: (False, None).
    """
    claim = current_user.get("client_id")
    if claim is None:
        return True, None
    if isinstance(claim, bool):
        return False, None
    if isinstance(claim, int):
        value = claim
    elif isinstance(claim, str) and _CLIENT_ID_TEXT.fullmatch(claim):
        value = int(claim)
    else:
        return False, None
    if not 1 <= value <= _MAX_CLIENT_ID:
        return False, None
    return True, value


async def _assert_portal_caller_owns_file(
    file_id: str,
    current_user: Any,
    db_pool: Any,
) -> None:
    """Enforce per-client ownership when a portal role (`client`, `partner`)
    calls the document proxy directly (#42).

    Staff and service callers are unaffected (returns immediately with zero DB
    queries). External partners (`partner`) have no client document scope and
    are refused outright. Client portal callers (`client`) may only access a
    file registered to their own `client_id`.
    """
    if not isinstance(current_user, dict):
        return
    role = normalize_role(current_user.get("role"))
    if role not in (CLIENT_ROLES | EXTERNAL_ROLES):
        return
    if role in EXTERNAL_ROLES:
        raise HTTPException(
            status_code=403,
            detail="This account type cannot access this resource",
        )

    # The database is the authority for the caller's client, always. A claim in the context
    # can only narrow it: it must be well formed and equal to the client the login resolves to.
    well_formed, claimed = _claimed_client_id(current_user)
    login_id = str(current_user.get("user_id") or current_user.get("id") or "")
    if not well_formed or not login_id:
        raise HTTPException(status_code=403, detail="File is not in a known client folder")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(_RESOLVE_PORTAL_CLIENT_ID_SQL, login_id)
        client_id = None
        if row is not None:
            client_id = row[0] if isinstance(row, tuple) else row.get("linked_client_id")
        if client_id is None or (claimed is not None and int(client_id) != claimed):
            raise HTTPException(status_code=403, detail="File is not in a known client folder")
        owns = await conn.fetchrow(_CLIENT_REGISTERED_FILE_SQL, file_id, int(client_id))
        if owns is None:
            raise HTTPException(status_code=403, detail="File is not in a known client folder")


@router.get("/proxy/{file_id}")
async def proxy_drive_file(
    file_id: str,
    current_user: dict = Depends(get_current_user),
    db_pool=Depends(get_database_pool),
) -> Response:
    """
    Proxy Google Drive file through backend.
    Returns binary image/document data without Google branding.

    Args:
        file_id: Google Drive file ID (extracted from drive URL)

    Returns:
        Binary file content with appropriate mime type
    """
    _assert_file_id_shape(file_id)
    await _assert_portal_caller_owns_file(file_id, current_user, db_pool)
    # Ask the database BEFORE minting a token. This is the path that carries the
    # traffic, and answering it here means an unregistered ID cannot use us as an
    # existence oracle for the whole Workspace (see the 404 handling below).
    registered = await _file_is_registered(file_id, db_pool)
    access_token = await _get_drive_access_token()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # First get file metadata to determine mime type
            meta_response = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"fields": _METADATA_FIELDS},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            if meta_response.status_code == 404:
                # Only a caller who already holds a registered ID learns that it
                # is gone. For anything else "absent" and "present but none of
                # your business" must be the same answer, or the endpoint reports
                # on the existence of files across the whole Workspace.
                raise HTTPException(
                    status_code=404 if registered else 403,
                    detail=(
                        "File not found in Google Drive"
                        if registered
                        else "File is not in a known client folder"
                    ),
                )

            if meta_response.status_code != 200:
                logger.error(f"[PROXY] Metadata fetch failed: {meta_response.status_code}")
                raise HTTPException(status_code=500, detail="Failed to fetch file metadata")

            metadata = meta_response.json()
            mime_type = metadata.get("mimeType", "application/octet-stream")
            file_name = metadata.get("name", "document")

            # Authorise BEFORE the download leg: the metadata call is the cheap
            # probe, the download is the disclosure.
            if not registered:
                await _assert_folder_ancestry_is_ours(
                    file_id,
                    metadata.get("parents") or [],
                    db_pool,
                    _drive_parent_fetcher(client, access_token),
                )

            meta_size = _parse_positive_int(metadata.get("size"))
            if meta_size is not None and meta_size > _MAX_PROXY_DOWNLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="File exceeds maximum proxy size (25 MB)",
                )

            # Download file content with streaming byte bound to prevent worker OOM
            if hasattr(type(client), "stream") and callable(type(client).stream):
                async with client.stream(
                    "GET",
                    f"https://www.googleapis.com/drive/v3/files/{file_id}",
                    params={"alt": "media"},
                    headers={"Authorization": f"Bearer {access_token}"},
                ) as download_response:
                    if download_response.status_code != 200:
                        logger.error(f"[PROXY] Download failed: {download_response.status_code}")
                        raise HTTPException(status_code=500, detail="Failed to download file")

                    content_length = _parse_positive_int(
                        download_response.headers.get("Content-Length")
                    )
                    if content_length is not None and content_length > _MAX_PROXY_DOWNLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="File exceeds maximum proxy size (25 MB)",
                        )

                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in download_response.aiter_bytes():
                        if not chunk:
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > _MAX_PROXY_DOWNLOAD_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail="File exceeds maximum proxy size (25 MB)",
                            )
                        chunks.append(chunk)
                    content_bytes = b"".join(chunks)
            else:
                download_response = await client.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}",
                    params={"alt": "media"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )

                if download_response.status_code != 200:
                    logger.error(f"[PROXY] Download failed: {download_response.status_code}")
                    raise HTTPException(status_code=500, detail="Failed to download file")

                content_bytes = download_response.content
                if len(content_bytes) > _MAX_PROXY_DOWNLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="File exceeds maximum proxy size (25 MB)",
                    )

            response_media_type, response_headers = _resolve_proxy_content_headers(
                mime_type, file_name
            )
            return Response(
                content=content_bytes,
                media_type=response_media_type,
                headers=response_headers,
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Google Drive request timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[PROXY] Unexpected error: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get("/thumbnail/{file_id}")
async def get_drive_thumbnail(
    file_id: str,
    width: int = Query(400, ge=100, le=1600, description="Thumbnail width in pixels"),
    current_user: dict = Depends(get_current_user),
    db_pool=Depends(get_database_pool),
) -> Any:
    """
    Get thumbnail of Google Drive file.
    Smaller, faster-loading preview image for document cards.

    Args:
        file_id: Google Drive file ID
        width: Desired thumbnail width (100-1600px)

    Returns:
        JPEG thumbnail image
    """
    _assert_file_id_shape(file_id)
    await _assert_portal_caller_owns_file(file_id, current_user, db_pool)
    registered = await _file_is_registered(file_id, db_pool)
    access_token = await _get_drive_access_token()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get thumbnail URL from file metadata. `parents` is requested here
            # for the same reason as in the proxy leg — a thumbnail of a file we
            # may not read is still a disclosure.
            response = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"fields": "thumbnailLink,mimeType,parents"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            if response.status_code == 404:
                raise HTTPException(
                    status_code=404 if registered else 403,
                    detail=(
                        "File not found" if registered else "File is not in a known client folder"
                    ),
                )

            if response.status_code != 200:
                raise HTTPException(status_code=500, detail="Failed to get file metadata")

            data = response.json()
            if not registered:
                await _assert_folder_ancestry_is_ours(
                    file_id,
                    data.get("parents") or [],
                    db_pool,
                    _drive_parent_fetcher(client, access_token),
                )
            thumbnail_url = data.get("thumbnailLink")

            if not thumbnail_url:
                # No thumbnail available - return placeholder or proxy full image
                # For images, we can use the full file as fallback
                mime_type = data.get("mimeType", "")
                if mime_type.startswith("image/"):
                    # Redirect to full proxy endpoint
                    return await proxy_drive_file(file_id, current_user, db_pool)

                raise HTTPException(
                    status_code=404,
                    detail="No thumbnail available for this file type",
                )

            # Modify thumbnail URL for custom size
            # Google thumbnail URLs contain =s{size} parameter
            thumbnail_url = re.sub(r"=s\d+", f"=s{width}", thumbnail_url)

            # Fetch thumbnail
            thumb_response = await client.get(thumbnail_url, timeout=10.0)

            if thumb_response.status_code != 200:
                raise HTTPException(status_code=500, detail="Failed to fetch thumbnail")

            if len(thumb_response.content) > _MAX_PROXY_DOWNLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Thumbnail exceeds maximum proxy size (25 MB)",
                )

            return Response(
                content=thumb_response.content,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "private, max-age=3600",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Thumbnail request timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[THUMBNAIL] Unexpected error: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error") from e


def extract_file_id_from_url(url: str) -> str | None:
    """
    Extract Google Drive file ID from various URL formats.

    Supported formats:
    - https://drive.google.com/file/d/{FILE_ID}/view
    - https://drive.google.com/open?id={FILE_ID}
    - https://drive.google.com/uc?id={FILE_ID}

    Args:
        url: Google Drive URL

    Returns:
        File ID or None if not found
    """
    if not url:
        return None

    # Format: /file/d/{FILE_ID}/
    match = re.search(r"/file/d/([^/]+)", url)
    if match:
        return match.group(1)

    # Format: ?id={FILE_ID}
    match = re.search(r"[?&]id=([^&]+)", url)
    if match:
        return match.group(1)

    return None
