"""
Google Drive Service Account Integration

Uses Service Account credentials for server-side Google Drive access.
No user OAuth required - perfect for automated uploads.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from backend.app.core.config import settings
from backend.security.pii_log_identifier import redact_identifier_for_log
from backend.services.crm.drive_taxonomy import standard_subfolder_paths

logger = logging.getLogger(__name__)


CANONICAL_FOLDER_LOCK_CLASS = 742003


@asynccontextmanager
async def canonical_folder_lock(conn: Any, parent_id: str, name: str):
    """Serialize all application writers for a Drive parent/name pair."""
    if conn is None:
        raise RuntimeError("Distributed Drive folder lock unavailable")
    digest = hashlib.sha256(f"{parent_id}\0{name}".encode()).digest()
    key = int.from_bytes(digest[:4], byteorder="big", signed=True)
    await conn.execute("SELECT pg_advisory_lock($1, $2)", CANONICAL_FOLDER_LOCK_CLASS, key)
    try:
        yield
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1, $2)", CANONICAL_FOLDER_LOCK_CLASS, key)


async def resolve_oauth_canonical_folder(
    drive: Any,
    name: str,
    parent_id: str,
    cached_id: str | None,
    conn: Any,
) -> str:
    async with canonical_folder_lock(conn, parent_id, name):
        return await _resolve_oauth_folder_unlocked(drive, name, parent_id, cached_id)


async def _resolve_oauth_folder_unlocked(
    drive: Any,
    name: str,
    parent_id: str,
    cached_id: str | None,
) -> str:
    async def validate(file_id: str) -> None:
        meta = await drive.get_file("SYSTEM", file_id)
        if (
            meta.get("id") != file_id
            or meta.get("name") != name
            or meta.get("mimeType") != "application/vnd.google-apps.folder"
            or parent_id not in meta.get("parents", [])
            or meta.get("trashed") is not False
        ):
            raise RuntimeError("Invalid OAuth Drive destination")

    async def find() -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        token = None
        seen = set()
        while True:
            page = await drive.list_files(
                "SYSTEM", folder_id=parent_id, page_token=token, page_size=1000
            )
            matches.extend(item for item in page["files"] if item.get("name") == name)
            token = page.get("next_page_token")
            if len(matches) > 1:
                raise RuntimeError("Ambiguous OAuth Drive destination")
            if not token:
                return matches
            if token in seen:
                raise RuntimeError("Invalid OAuth Drive pagination")
            seen.add(token)

    if cached_id:
        await validate(cached_id)
    matches = await find()
    if cached_id and (not matches or matches[0].get("id") != cached_id):
        raise RuntimeError("Cached OAuth Drive destination does not match")
    if not matches:
        await drive.create_folder(user_id="SYSTEM", name=name, parent_id=parent_id)
        matches = await find()
        if not matches:
            raise RuntimeError("Created OAuth Drive folder cannot be verified")
    if matches[0].get("mimeType") != "application/vnd.google-apps.folder":
        raise RuntimeError("OAuth Drive destination is not a physical folder")
    folder_id = matches[0]["id"]
    await validate(folder_id)
    return folder_id


class DriveArchiveIntegrityError(RuntimeError):
    """Raised when a legal archive cannot prove content-bound idempotency."""


class ServiceAccountDriveService:
    """Google Drive operations using Service Account credentials."""

    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(
        self,
        *,
        root_folder_id: str | None = None,
        delegated_user: str | None = None,
        validate_folders: bool = True,
    ) -> None:
        """Initialize Service Account Drive Service.

        `validate_folders=False` skips the boot-time probe of the configured parent folders (four
        network calls): a per-request client (HR documents) must not pay them on every request.
        `build()` itself uses the bundled discovery document, so it does not touch the network.
        """
        import base64

        self.root_folder_id = root_folder_id or settings.google_drive_root_folder_id

        # Load Service Account credentials (supports raw JSON or base64-encoded JSON)
        creds_str = getattr(settings, "google_credentials_json", None)
        if not creds_str:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON not configured in settings")

        service_account_info = None

        # Try raw JSON first
        try:
            parsed = json.loads(creds_str)
            if parsed.get("type") == "service_account":
                service_account_info = parsed
                logger.info("✅ Loaded Google credentials from raw JSON")
        except json.JSONDecodeError as e:
            logger.debug("SA creds not raw JSON, trying base64: %s", type(e).__name__)

        # Try base64-encoded JSON
        if not service_account_info:
            try:
                decoded = base64.b64decode(creds_str).decode("utf-8")
                parsed = json.loads(decoded)
                if parsed.get("type") == "service_account":
                    service_account_info = parsed
                    logger.info("✅ Loaded Google credentials from base64-encoded JSON")
            except Exception as e:
                logger.debug("SA creds base64 decode failed: %s", type(e).__name__)

        if not service_account_info:
            raise ValueError(
                "Invalid Service Account credentials - must be raw JSON or base64-encoded JSON",
            )

        # Create credentials with Domain-wide Delegation
        # This allows the Service Account to impersonate a Workspace user
        # who has access to Shared Drives (AMBARADAM)
        base_credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=self.SCOPES,
        )

        # Impersonate a Workspace user with Shared Drive access
        # Domain-wide delegation configured in Google Admin Console for this SA
        self.delegated_user = delegated_user or "zero@balizero.com"
        self.credentials = base_credentials.with_subject(self.delegated_user)
        logger.info(
            "✅ Using Domain-wide delegation, impersonating: %s",
            redact_identifier_for_log(self.delegated_user),
        )

        # Build API client
        self.service = build("drive", "v3", credentials=self.credentials)

        # Validate configured parent folder IDs exist & are reachable.
        # Reason: GDRIVE_COMPANIES_FOLDER_ID pointed to a phantom (404) from Feb 2026 to May
        # 2026, silently breaking every company-client create_client_folder call. The
        # BackgroundTask exception was swallowed by crm_clients.py and never surfaced.
        # See cicatrix-scars.md (2026-05-21) for the full incident.
        if validate_folders:
            self._validate_configured_folders()

    def _validate_configured_folders(self) -> None:
        """Verify each configured parent folder ID exists and is accessible.

        Logs ERROR (and would alert via Sentry / Telegram in production) for any
        404 / inaccessible folder. Does NOT raise — the service still boots so
        other endpoints work, but operators know which folder is broken.
        """
        configured = [
            ("DRIVE_ROOT_FOLDER_ID (active)", self.root_folder_id),
            ("GOOGLE_DRIVE_ROOT_FOLDER_ID", getattr(settings, "google_drive_root_folder_id", None)),
            (
                "GDRIVE_INDIVIDUALS_FOLDER_ID",
                getattr(settings, "gdrive_individuals_folder_id", None),
            ),
            ("GDRIVE_COMPANIES_FOLDER_ID", getattr(settings, "gdrive_companies_folder_id", None)),
        ]
        for env_name, folder_id in configured:
            if not folder_id:
                logger.warning("⚠ Drive folder env %s not set — skipping validation", env_name)
                continue
            try:
                meta = (
                    self.service.files()
                    .get(
                        fileId=folder_id,
                        fields="id, name, trashed",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
                if meta.get("trashed"):
                    logger.error(
                        "🚨 Drive folder %s (%s) is TRASHED — client folder creation will fail",
                        env_name,
                        folder_id,
                    )
                else:
                    logger.info(
                        "✅ Drive folder %s OK: %s",
                        env_name,
                        folder_id,
                    )
            except Exception as e:
                logger.error(
                    "🚨 Drive folder %s (%s) UNREACHABLE — clients of the matching type "
                    "will fail to get a Drive folder. Error: %s (HTTP status: %s)",
                    env_name,
                    folder_id,
                    type(e).__name__,
                    getattr(getattr(e, "resp", None), "status", None),
                )

    async def create_folder(
        self,
        name: str,
        parent_id: str | None = None,
        user_id: str | None = None,  # Kept for API compatibility
        app_properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a new folder in Google Drive."""
        if not parent_id:
            parent_id = self.root_folder_id

        folder_metadata: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id] if parent_id else [],
        }
        if app_properties:
            folder_metadata["appProperties"] = dict(app_properties)

        # Build request
        request = self.service.files().create(
            body=folder_metadata,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )

        # Execute non-blocking
        import asyncio

        folder = await asyncio.to_thread(request.execute)

        logger.info("✅ Created folder (ID: %s)", folder["id"])
        return folder

    async def get_file_metadata(self, file_id: str, *, checksums: bool = False) -> dict[str, Any]:
        """Fetch file/folder metadata via Drive API v3.

        Returns the dict from Drive API (name, parents, mimeType, size, ...).
        Caller paths: backend/services/crm/drive_poll_service.py:266 + :314.
        """
        fields = "id, name, mimeType, parents, trashed, size, modifiedTime, webViewLink"
        if checksums:
            fields += ", md5Checksum, sha256Checksum"
        request = self.service.files().get(
            fileId=file_id,
            fields=fields,
            supportsAllDrives=True,
        )
        return await asyncio.to_thread(request.execute)

    async def download_file_content(self, file_id: str) -> bytes:
        """Fetch a file's bytes via Drive API v3 `alt=media`.

        The Service Account is the only Drive credential this backend still
        has: the SYSTEM OAuth token has been deliberately dead since
        2026-05-10 (`GoogleDriveService._refresh_token` returns None for
        SYSTEM by design). Every download proxy therefore has to come
        through here.
        """
        request = self.service.files().get_media(
            fileId=file_id,
            supportsAllDrives=True,
        )
        return await asyncio.to_thread(request.execute)

    async def get_file_metadata_detailed(self, file_id: str) -> dict[str, Any]:
        """Fetch read-only metadata needed by CRM Guardian evidence scans."""
        fields = (
            "id, name, mimeType, owners(emailAddress,displayName), parents, driveId, "
            "ownedByMe, shared, trashed, createdTime, modifiedTime, webViewLink, "
            "shortcutDetails, capabilities(canCopy,canDownload,canEdit,"
            "canMoveItemIntoTeamDrive,canMoveItemOutOfDrive,canMoveItemWithinDrive)"
        )
        request = self.service.files().get(
            fileId=file_id,
            fields=fields,
            supportsAllDrives=True,
        )
        return await asyncio.to_thread(request.execute)

    async def get_folder_structure(
        self,
        root_folder_id: str,
        user_id: str | None = None,  # Kept for API compatibility
    ) -> dict[str, Any]:
        """Get folder structure (list subfolders and stats)."""
        # Get root folder info
        root_request = self.service.files().get(
            fileId=root_folder_id,
            fields="id, name",
            supportsAllDrives=True,
        )

        import asyncio

        root_folder = await asyncio.to_thread(root_request.execute)

        # List subfolders
        query = f"'{root_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folders_request = self.service.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        results = await asyncio.to_thread(folders_request.execute)

        folders = results.get("files", [])

        # Count files
        file_query = f"'{root_folder_id}' in parents and mimeType!='application/vnd.google-apps.folder' and trashed=false"
        files_request = self.service.files().list(
            q=file_query,
            fields="files(id, size)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        file_results = await asyncio.to_thread(files_request.execute)

        files = file_results.get("files", [])
        total_files = len(files)
        total_size_bytes = sum(int(f.get("size", 0)) for f in files if f.get("size"))

        return {
            "root_id": root_folder["id"],
            "root_name": root_folder["name"],
            "folders": folders,
            "total_files": total_files,
            "total_size_bytes": total_size_bytes,
        }

    async def upload_file_to_folder(
        self,
        folder_id: str,
        file_content: bytes,
        file_name: str,
        mime_type: str | None = None,
        user_id: str | None = None,  # Kept for API compatibility
        app_properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Upload a file to Google Drive folder."""
        if not mime_type:
            mime_type = "application/octet-stream"
            if file_name.lower().endswith(".pdf"):
                mime_type = "application/pdf"
            elif file_name.lower().endswith((".png", ".jpg", ".jpeg")):
                mime_type = "image/jpeg"

        file_metadata: dict[str, Any] = {"name": file_name, "parents": [folder_id]}
        if app_properties:
            file_metadata["appProperties"] = dict(app_properties)

        # Create file stream
        file_stream = BytesIO(file_content)
        media = MediaIoBaseUpload(file_stream, mimetype=mime_type, resumable=True)

        # Build request (don't execute yet)
        request = self.service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink, size, md5Checksum",
            supportsAllDrives=True,
        )

        # Execute in thread pool to avoid blocking event loop
        file = await asyncio.to_thread(request.execute)

        logger.info("✅ Uploaded (ID: %s, %s bytes)", file.get("id"), file.get("size"))
        return file

    # Advisory-lock class for per-client Drive folder creation, used as
    # pg_advisory_lock(DRIVE_FOLDER_LOCK_CLASS, client_id). Serializes creation
    # across processes (api + rag groups share the same Postgres), closing the
    # race where POST /api/clients' background task and the passport upload
    # endpoint both saw google_drive_folder_id IS NULL and each created a root.
    DRIVE_FOLDER_LOCK_CLASS = 742001
    DRIVE_ARCHIVE_LOCK_CLASS = 742002
    DRIVE_COMPANY_FOLDER_LOCK_CLASS = 742003
    _archive_locks: dict[str, asyncio.Lock] = {}

    STANDARD_SUBFOLDERS = standard_subfolder_paths()

    def _parent_folder_for_client_type(self, client_type: str) -> str:
        """Resolve the configured parent folder for a client type."""
        if client_type == "individual":
            parent_folder_id = getattr(settings, "gdrive_individuals_folder_id", None)
        elif client_type == "company":
            parent_folder_id = getattr(settings, "gdrive_companies_folder_id", None)
        else:
            parent_folder_id = None
        return parent_folder_id or settings.google_drive_root_folder_id

    async def find_folder(self, name: str, parent_id: str) -> dict[str, Any] | None:
        """Return one physical folder, rejecting ambiguity and shortcuts."""
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and '{parent_id}' in parents and trashed = false"
        files = []
        page_token = None
        seen = set()
        for _ in range(100):
            params = {
                "q": query,
                "fields": "nextPageToken, files(id, name, mimeType, parents, trashed, webViewLink)",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "pageSize": 2,
            }
            if page_token:
                params["pageToken"] = page_token
            result = await asyncio.to_thread(self.service.files().list(**params).execute)
            files.extend(result.get("files", []))
            if len(files) > 1:
                raise RuntimeError("Ambiguous Drive folder name")
            page_token = result.get("nextPageToken")
            if not page_token:
                break
            if page_token in seen:
                raise RuntimeError("Invalid Drive folder pagination")
            seen.add(page_token)
        else:
            raise RuntimeError("Drive folder pagination limit exceeded")
        if files and files[0].get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError("Drive destination is not a physical folder")
        return files[0] if files else None

    async def resolve_canonical_folder(
        self,
        name: str,
        root_id: str,
        cached_id: str | None = None,
        *,
        conn: Any = None,
    ) -> str:
        """Validate and serialize the destination by parent and folder name."""
        async with canonical_folder_lock(conn, root_id, name):
            return await self._resolve_canonical_folder_unlocked(name, root_id, cached_id)

    async def _resolve_canonical_folder_unlocked(
        self,
        name: str,
        root_id: str,
        cached_id: str | None,
    ) -> str:
        if cached_id:
            meta = await self.get_file_metadata(cached_id)
            if (
                meta.get("id") != cached_id
                or meta.get("name") != name
                or meta.get("mimeType") != "application/vnd.google-apps.folder"
                or root_id not in meta.get("parents", [])
                or meta.get("trashed") is not False
            ):
                raise RuntimeError("Invalid cached Drive folder")
        found = await self.find_folder(name, root_id)
        if cached_id and (not found or found.get("id") != cached_id):
            raise RuntimeError("Cached Drive folder does not match canonical destination")
        if not found:
            await self.create_folder(name=name, parent_id=root_id)
            found = await self.find_folder(name, root_id)
            if not found:
                raise RuntimeError("Created Drive folder cannot be verified")
        meta = await self.get_file_metadata(found["id"])
        if (
            meta.get("id") != found["id"]
            or meta.get("name") != name
            or meta.get("mimeType") != "application/vnd.google-apps.folder"
            or root_id not in meta.get("parents", [])
            or meta.get("trashed") is not False
        ):
            raise RuntimeError("Invalid canonical Drive folder")
        return found["id"]

    async def find_file(self, name: str, parent_id: str) -> dict[str, Any] | None:
        """Find one non-trashed file by exact name under a known parent."""
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{escaped}' "
            "and mimeType != 'application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        request = self.service.files().list(
            q=query,
            fields="files(id, name, webViewLink, size, md5Checksum)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=2,
        )
        result = await asyncio.to_thread(request.execute)
        files = result.get("files", [])
        if len(files) > 1:
            raise DriveArchiveIntegrityError(
                f"Multiple Drive archive files share the name {name!r}"
            )
        return files[0] if files else None

    @staticmethod
    def _archive_lock_key(folder_id: str, file_name: str) -> int:
        """Return a stable signed int32 key for a Postgres advisory lock."""
        digest = hashlib.sha256(f"{folder_id}\0{file_name}".encode()).digest()
        return int.from_bytes(digest[:4], byteorder="big", signed=True)

    async def _archive_file_unlocked(
        self,
        *,
        folder_id: str,
        file_content: bytes,
        file_name: str,
        mime_type: str,
    ) -> tuple[dict[str, Any], str]:
        """Find or upload one archive file while the caller holds the name lock."""
        source_md5 = hashlib.md5(file_content).hexdigest()
        existing = await self.find_file(name=file_name, parent_id=folder_id)
        if existing:
            if existing.get("md5Checksum") != source_md5:
                raise DriveArchiveIntegrityError(
                    "Drive archive name collision or unverifiable checksum"
                )
            return existing, "reused"

        uploaded = await self.upload_file_to_folder(
            folder_id=folder_id,
            file_content=file_content,
            file_name=file_name,
            mime_type=mime_type,
        )
        if uploaded.get("md5Checksum") != source_md5:
            raise DriveArchiveIntegrityError("Drive archive upload checksum was not verified")
        return uploaded, "uploaded"

    async def archive_file_idempotent(
        self,
        *,
        folder_id: str,
        file_content: bytes,
        file_name: str,
        mime_type: str,
        db_pool: Any | None,
        require_distributed_lock: bool = False,
    ) -> tuple[dict[str, Any], str]:
        """Archive a file once, serialized by name across workers when DB is available."""
        if db_pool is None:
            if require_distributed_lock:
                raise DriveArchiveIntegrityError(
                    "Distributed archive lock unavailable for historical legal source"
                )
            lock_name = f"{folder_id}\0{file_name}"
            lock = self._archive_locks.setdefault(lock_name, asyncio.Lock())
            async with lock:
                return await self._archive_file_unlocked(
                    folder_id=folder_id,
                    file_content=file_content,
                    file_name=file_name,
                    mime_type=mime_type,
                )

        lock_key = self._archive_lock_key(folder_id, file_name)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_advisory_lock($1, $2)",
                self.DRIVE_ARCHIVE_LOCK_CLASS,
                lock_key,
            )
            try:
                return await self._archive_file_unlocked(
                    folder_id=folder_id,
                    file_content=file_content,
                    file_name=file_name,
                    mime_type=mime_type,
                )
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock($1, $2)",
                    self.DRIVE_ARCHIVE_LOCK_CLASS,
                    lock_key,
                )

    async def ensure_client_folder(
        self,
        client_id: int,
        client_name: str,
        client_type: str = "individual",
        db_pool: Any | None = None,
        conn: Any | None = None,
    ) -> dict[str, Any]:
        """Idempotent chokepoint for client Drive folder creation.

        Every caller that may run concurrently (client-create background task,
        document upload, ensure-drive-folder endpoint, client.changed event
        handler) MUST go through here. Guarantees at most ONE root folder per
        client via:
          1. pg_advisory_lock(DRIVE_FOLDER_LOCK_CLASS, client_id)
          2. re-read of clients.google_drive_folder_id inside the lock
          3. Drive-side reuse of an existing "{id}_{name}" folder (heals
             orphan twins created before this fix)
          4. persisting the root folder id IMMEDIATELY (before subfolders)

        Subfolder creation happens outside the lock and only when the root was
        newly created here.
        """
        if conn is None:
            if db_pool is None:
                raise RuntimeError("Distributed Drive folder lock unavailable")
            async with db_pool.acquire() as acquired:
                return await self.ensure_client_folder(
                    client_id, client_name, client_type, db_pool=db_pool, conn=acquired
                )

        root_folder_name = f"{client_id}_{client_name}"
        parent_folder_id = self._parent_folder_for_client_type(client_type)
        created = False

        await conn.execute(
            "SELECT pg_advisory_lock($1, $2)",
            self.DRIVE_FOLDER_LOCK_CLASS,
            client_id,
        )
        try:
            existing = await conn.fetchval(
                "SELECT google_drive_folder_id FROM clients WHERE id = $1",
                client_id,
            )
            if existing:
                return {
                    "success": True,
                    "created": False,
                    "root_folder_id": existing,
                    "root_folder_url": f"https://drive.google.com/drive/folders/{existing}",
                    "subfolders": {},
                }

            async with canonical_folder_lock(conn, parent_folder_id, root_folder_name):
                found = await self.find_folder(root_folder_name, parent_folder_id)

                if found:
                    root_folder_id = found["id"]
                    root_folder_url = found.get(
                        "webViewLink",
                        f"https://drive.google.com/drive/folders/{found['id']}",
                    )
                    logger.info(
                        "♻️ Reusing existing Drive folder for client %s: %s",
                        client_id,
                        root_folder_id,
                    )
                else:
                    root_folder = await self.create_folder(
                        name=root_folder_name,
                        parent_id=parent_folder_id,
                    )
                    root_folder_id = root_folder["id"]
                    root_folder_url = root_folder.get("webViewLink", "")
                    created = True

            # Persist BEFORE building subfolders so concurrent callers see
            # the folder as soon as the lock releases.
            await conn.execute(
                "UPDATE clients SET google_drive_folder_id = $1 WHERE id = $2",
                root_folder_id,
                client_id,
            )
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock($1, $2)",
                self.DRIVE_FOLDER_LOCK_CLASS,
                client_id,
            )

        subfolders: dict[str, Any] = {}
        if created:
            subfolders = await self._create_standard_subfolders(
                root_folder_id,
                client_id=client_id,
                db_pool=db_pool,
                client_type=client_type,
                conn=conn,
            )
            logger.info(
                "✅ Created client folder structure for client %s: %s", client_id, root_folder_id
            )

        return {
            "success": True,
            "created": created,
            "root_folder_id": root_folder_id,
            "root_folder_url": root_folder_url,
            "subfolders": subfolders,
        }

    async def create_client_folder(
        self,
        client_id: int,
        client_name: str,
        client_type: str = "individual",
        db_pool: Any | None = None,
    ) -> dict[str, Any]:
        """Provision via the same distributed locks used by concurrent callers."""
        if db_pool is None:
            raise RuntimeError("Distributed Drive folder lock unavailable")
        return await self.ensure_client_folder(client_id, client_name, client_type, db_pool)

    async def ensure_company_folder(
        self,
        company_id: int,
        company_name: str | None = None,
        db_pool: Any | None = None,
        conn: Any | None = None,
    ) -> dict[str, Any]:
        """Idempotent chokepoint for company Drive root + subfolder provisioning."""
        if conn is None:
            if db_pool is None:
                raise RuntimeError("Distributed Drive folder lock unavailable")
            async with db_pool.acquire() as acquired:
                return await self.ensure_company_folder(
                    company_id,
                    company_name,
                    db_pool=db_pool,
                    conn=acquired,
                )

        parent_folder_id = self._parent_folder_for_client_type("company")
        created = False

        await conn.execute(
            "SELECT pg_advisory_lock($1, $2)",
            self.DRIVE_COMPANY_FOLDER_LOCK_CLASS,
            company_id,
        )
        try:
            row = await conn.fetchrow(
                "SELECT company_name, google_drive_folder_id "
                "FROM companies WHERE id = $1 AND COALESCE(status, '') != 'deleted'",
                company_id,
            )
            if row is None:
                raise RuntimeError(f"Company {company_id} not found or deleted")
            existing = row["google_drive_folder_id"]
            if existing:
                return {
                    "success": True,
                    "created": False,
                    "root_folder_id": existing,
                    "root_folder_url": f"https://drive.google.com/drive/folders/{existing}",
                    "folder_id": existing,
                    "folder_url": f"https://drive.google.com/drive/folders/{existing}",
                    "subfolders": {},
                }

            clean_name = str(company_name or row["company_name"] or f"Company_{company_id}").strip()
            root_folder_name = f"[COMPANY #{company_id}] {clean_name}"

            async with canonical_folder_lock(conn, parent_folder_id, root_folder_name):
                found = await self.find_folder(root_folder_name, parent_folder_id)
                if found:
                    root_folder_id = found["id"]
                    root_folder_url = found.get(
                        "webViewLink",
                        f"https://drive.google.com/drive/folders/{found['id']}",
                    )
                    logger.info(
                        "♻️ Reusing existing Drive folder for company %s: %s",
                        company_id,
                        root_folder_id,
                    )
                else:
                    root_folder = await self.create_folder(
                        name=root_folder_name,
                        parent_id=parent_folder_id,
                    )
                    root_folder_id = root_folder["id"]
                    root_folder_url = root_folder.get(
                        "webViewLink",
                        f"https://drive.google.com/drive/folders/{root_folder_id}",
                    )
                    created = True

            await conn.execute(
                "UPDATE companies SET google_drive_folder_id = $1, updated_at = NOW() WHERE id = $2",
                root_folder_id,
                company_id,
            )
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock($1, $2)",
                self.DRIVE_COMPANY_FOLDER_LOCK_CLASS,
                company_id,
            )

        subfolders: dict[str, Any] = {}
        if created:
            subfolders = await self._create_standard_subfolders(
                root_folder_id,
                client_id=company_id,
                db_pool=db_pool,
                client_type="company",
                conn=conn,
                persist_client_subfolders=False,
            )
            logger.info(
                "✅ Created company folder structure for company %s: %s",
                company_id,
                root_folder_id,
            )

        return {
            "success": True,
            "created": created,
            "root_folder_id": root_folder_id,
            "root_folder_url": root_folder_url,
            "folder_id": root_folder_id,
            "folder_url": root_folder_url,
            "subfolders": subfolders,
        }

    async def _create_standard_subfolders(
        self,
        root_folder_id: str,
        client_id: int,
        db_pool: Any | None = None,
        client_type: str = "individual",
        conn: Any | None = None,
        persist_client_subfolders: bool = True,
    ) -> dict[str, Any]:
        """Create the standard subfolder tree under a client or company root folder."""
        if conn is None:
            if db_pool is None:
                raise RuntimeError("Distributed Drive folder lock unavailable")
            async with db_pool.acquire() as acquired:
                return await self._create_standard_subfolders(
                    root_folder_id,
                    client_id,
                    db_pool,
                    client_type,
                    conn=acquired,
                    persist_client_subfolders=persist_client_subfolders,
                )
        subfolders: dict[str, Any] = {}
        folder_id_cache = {"": root_folder_id}
        for subfolder_path in standard_subfolder_paths(client_type):
            try:
                parts = subfolder_path.split("/")
                if len(parts) == 1:
                    parent_id = root_folder_id
                    name = parts[0]
                else:
                    parent_path = parts[0]
                    name = parts[1]
                    parent_id = folder_id_cache[parent_path]

                folder_id = await self.resolve_canonical_folder(name, parent_id, conn=conn)
                subfolders[subfolder_path] = {"id": folder_id, "url": ""}
                folder_id_cache[subfolder_path] = folder_id
            except Exception as e:
                logger.error(
                    "Failed to create subfolder under root %s for client %s: %s (HTTP status: %s)",
                    root_folder_id,
                    client_id,
                    type(e).__name__,
                    getattr(getattr(e, "resp", None), "status", None),
                )
                continue

        # Persist subfolder IDs to DB so DrivePollService can match files to clients
        if db_pool and persist_client_subfolders:
            try:
                for subfolder_path, subfolder_data in subfolders.items():
                    if "/" not in subfolder_path:  # top-level only
                        await conn.execute(
                            """INSERT INTO client_drive_subfolders
                               (client_id, subfolder_name, subfolder_id, created_at)
                               VALUES ($1, $2, $3, NOW())
                               ON CONFLICT DO NOTHING""",
                            client_id,
                            subfolder_path,
                            subfolder_data["id"],
                        )
                logger.info("✅ Drive folder IDs persisted to DB for client %s", client_id)
            except Exception as e:
                logger.error(
                    "Failed to persist drive folder IDs for client %s: %s",
                    client_id,
                    type(e).__name__,
                )

        return subfolders

    async def move_file(
        self,
        file_id: str,
        from_parent_id: str,
        to_parent_id: str,
    ) -> dict[str, Any]:
        """
        Move a file from one Drive folder to another.
        Uses files.update with addParents/removeParents.
        """
        request = self.service.files().update(
            fileId=file_id,
            addParents=to_parent_id,
            removeParents=from_parent_id,
            fields="id, name, parents",
            supportsAllDrives=True,
        )
        result = await asyncio.to_thread(request.execute)
        logger.info("Moved file %s from %s to %s", file_id, from_parent_id, to_parent_id)
        return result

    async def find_folders_by_app_property(
        self, key: str, value: str, strict: bool = False
    ) -> list[dict[str, Any]]:
        """Every live (not trashed) physical folder carrying appProperties[key] == value. The
        lookup reads at most 20 pages; with `strict` a lookup that stopped there with a page still
        pending raises instead of returning a part."""
        esc = lambda s: s.replace("\\", "\\\\").replace("'", "\\'")  # noqa: E731
        query = (
            f"appProperties has {{ key='{esc(key)}' and value='{esc(value)}' }} "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        found: list[dict[str, Any]] = []
        page_token = None
        for _ in range(20):
            params: dict[str, Any] = {
                "q": query,
                "fields": "nextPageToken, files(id, name, parents)",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            result = await asyncio.to_thread(self.service.files().list(**params).execute)
            found.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        if strict and page_token:
            raise RuntimeError("lookup truncated")
        return found

    async def list_files_by_app_properties(
        self,
        props: dict[str, str],
        parent_id: str,
        mimes: Sequence[str] | None,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        """Live (not trashed) files directly inside `parent_id` whose appProperties equal ALL of
        `props` and whose mimeType is one of `mimes` (`None`: any type but a folder). Every input
        is one the caller resolved itself (a member id, a slot, a folder id Drive returned, a
        closed set of types); nothing read from stored data may be passed here. Returns id,
        mimeType, parents and appProperties. The listing reads at most 20 pages; with `strict` a
        listing that stopped there with a page still pending raises instead of returning a part."""
        esc = lambda v: str(v).replace("\\", "\\\\").replace("'", "\\'")  # noqa: E731
        clauses = [
            f"appProperties has {{ key='{esc(k)}' and value='{esc(v)}' }}" for k, v in props.items()
        ]
        clauses.append(f"'{esc(parent_id)}' in parents")
        clauses.append("trashed = false")
        if mimes is None:
            clauses.append("mimeType != 'application/vnd.google-apps.folder'")
        else:
            clauses.append("(" + " or ".join(f"mimeType = '{esc(m)}'" for m in mimes) + ")")
        found: list[dict[str, Any]] = []
        page_token = None
        for _ in range(20):
            params: dict[str, Any] = {
                "q": " and ".join(clauses),
                "fields": "nextPageToken, files(id, mimeType, parents, appProperties)",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            result = await asyncio.to_thread(self.service.files().list(**params).execute)
            found.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        if strict and page_token:
            raise RuntimeError("listing truncated")
        return found

    async def copy_file(
        self, file_id: str, parent_id: str, name: str, app_properties: dict[str, str]
    ) -> dict[str, Any]:
        """`files.copy` of `file_id` into `parent_id`, named `name`, with `app_properties`. The
        source is only read. Returns the COPY's id, mimeType and parents; logs nothing."""
        request = self.service.files().copy(
            fileId=file_id,
            body={"name": name, "parents": [parent_id], "appProperties": dict(app_properties)},
            fields="id, mimeType, parents",
            supportsAllDrives=True,
        )
        return await asyncio.to_thread(request.execute)

    async def rename_file(self, file_id: str, new_name: str) -> dict[str, Any]:
        """Rename a file in Google Drive (Shared Drive safe)."""
        request = self.service.files().update(
            fileId=file_id,
            body={"name": new_name},
            fields="id, name, mimeType, size, modifiedTime, webViewLink",
            supportsAllDrives=True,
        )
        updated = await asyncio.to_thread(request.execute)
        logger.info("Renamed Drive file %s", file_id)
        return updated

    async def update_file_content(
        self,
        file_id: str,
        file_content: bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Replace a file's binary content in-place (preserving Drive file ID and revisions)."""
        effective_mime = mime_type or "application/octet-stream"
        body: dict[str, Any] = {}
        if file_name:
            body["name"] = file_name
        media = MediaIoBaseUpload(BytesIO(file_content), mimetype=effective_mime, resumable=True)
        request = self.service.files().update(
            fileId=file_id,
            body=body,
            media_body=media,
            fields="id, name, mimeType, size, modifiedTime, webViewLink",
            supportsAllDrives=True,
        )
        updated = await asyncio.to_thread(request.execute)
        logger.info("Replaced Drive file content %s (%s bytes)", file_id, len(file_content))
        return updated

    async def trash_file(self, file_id: str) -> None:
        """Move one file to the Drive trash (recoverable). Never a permanent delete."""
        request = self.service.files().update(
            fileId=file_id, body={"trashed": True}, fields="id, trashed", supportsAllDrives=True
        )
        await asyncio.to_thread(request.execute)
        logger.info("Trashed Drive file %s", file_id)

    async def get_start_page_token(self) -> str:
        """Get the current start page token for changes tracking."""
        request = self.service.changes().getStartPageToken(
            supportsAllDrives=True,
        )
        result = await asyncio.to_thread(request.execute)
        return result["startPageToken"]

    async def list_changes_since(
        self,
        page_token: str,
        *,
        max_pages: int | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """
        List file changes since the given page token.

        Returns:
            {
                "changes": [{"fileId": "...", "file": {...}, "removed": bool}],
                "new_page_token": "...",
                "more_pages": bool,
                "pages_fetched": int,
            }
        """
        bounded_page_size = max(1, min(page_size, 1000))
        bounded_max_pages = max(1, max_pages) if max_pages is not None else None
        all_changes: list[dict[str, Any]] = []
        current_token = page_token
        pages_fetched = 0
        more_pages = False
        next_page_token: str | None = None
        new_page_token = page_token

        while True:
            request = self.service.changes().list(
                pageToken=current_token,
                fields="nextPageToken, newStartPageToken, changes(fileId, file(id, name, mimeType, parents, trashed), removed)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=bounded_page_size,
            )
            result = await asyncio.to_thread(request.execute)
            pages_fetched += 1

            changes = result.get("changes", [])
            all_changes.extend(changes)

            next_page_token = result.get("nextPageToken")
            if not next_page_token:
                new_page_token = result.get("newStartPageToken", page_token)
                break

            if bounded_max_pages is not None and pages_fetched >= bounded_max_pages:
                more_pages = True
                new_page_token = next_page_token
                break

            current_token = next_page_token

        return {
            "changes": all_changes,
            "new_page_token": new_page_token,
            "more_pages": more_pages,
            "pages_fetched": pages_fetched,
            "next_page_token": next_page_token,
        }
