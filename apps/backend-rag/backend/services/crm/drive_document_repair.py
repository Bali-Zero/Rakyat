"""Bounded operator repair: register an existing Drive file hidden, never move it.

Manifest and output contain IDs/hashes only. Identity and ACL values stay in memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Any

import httpx
from google.auth.transport.requests import Request

from backend.services.crm.drive_taxonomy import folder_for_category
from backend.services.intake.writer import write_client_document

SOURCE = "drive-repair-v1"
MAX_BYTES = 20 * 1024 * 1024
FOLDER_MIME = "application/vnd.google-apps.folder"
KEYS = {
    "client_id",
    "file_id",
    "root_id",
    "source_parent_id",
    "target_folder_id",
    "document_type",
    "document_category",
    "source_md5",
    "source_size",
    "source_version",
    "passport_sha256",
    "source_content_sha256",
    "name_sha256",
    "dob_sha256",
}


class RepairRejected(ValueError):
    """Safe code only: never include CRM values or provider exception messages."""


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def identity_hash(value: Any, kind: str) -> str:
    if value is None:
        raise RepairRejected("IDENTITY_EMPTY")
    if kind == "number":
        normalized = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    elif kind == "name":
        normalized = re.sub(
            r"[^A-Z0-9]",
            "",
            unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().upper(),
        )
    elif kind == "dob":
        normalized = str(value)[:10]
        try:
            if date.fromisoformat(normalized).isoformat() != normalized:
                raise ValueError
        except ValueError:
            raise RepairRejected("IDENTITY_DOB_INVALID") from None
    else:
        raise RepairRejected("IDENTITY_KIND_INVALID")
    if not normalized:
        raise RepairRejected("IDENTITY_EMPTY")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_manifest(manifest: Any) -> list[dict]:
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "records"}
        or manifest["schema_version"] != 1
    ):
        raise RepairRejected("MANIFEST_INVALID")
    records = manifest["records"]
    if not isinstance(records, list) or not 1 <= len(records) <= 20:
        raise RepairRejected("MANIFEST_COUNT")
    seen = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != KEYS:
            raise RepairRejected("MANIFEST_FIELDS")
        if type(row["client_id"]) is not int or row["client_id"] <= 0:
            raise RepairRejected("MANIFEST_CLIENT")
        if row["document_type"] not in {"passport", "itap"} or row["document_category"] not in {
            "personal",
            "immigration",
        }:
            raise RepairRejected("MANIFEST_CLASSIFICATION")
        for key in ("file_id", "root_id", "source_parent_id", "target_folder_id"):
            if not isinstance(row[key], str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{10,256}", row[key]
            ):
                raise RepairRejected("MANIFEST_ID")
        for key in (
            "passport_sha256",
            "source_content_sha256",
            "name_sha256",
            "dob_sha256",
            "source_md5",
        ):
            length = 32 if key == "source_md5" else 64
            if not isinstance(row[key], str) or not re.fullmatch(
                rf"[0-9a-f]{{{length}}}", row[key]
            ):
                raise RepairRejected("MANIFEST_HASH")
        if (
            type(row["source_size"]) is not int
            or not 0 < row["source_size"] <= MAX_BYTES
            or not re.fullmatch(r"[0-9]+", str(row["source_version"]))
        ):
            raise RepairRejected("MANIFEST_CONTENT")
        if row["file_id"] in seen or row["source_parent_id"] == row["target_folder_id"]:
            raise RepairRejected("MANIFEST_DUPLICATE_OR_ALREADY_MOVED")
        seen.add(row["file_id"])
    return records


class ReadOnlyDrive:
    """Only GET/list operations; no upload, move, permission or deletion method."""

    def __init__(self, drive):
        from backend.app.core.config import settings

        self.drive = drive
        self.individual_root = settings.gdrive_individuals_folder_id

    async def metadata(self, file_id):
        request = self.drive.service.files().get(
            fileId=file_id,
            supportsAllDrives=True,
            fields="id,name,mimeType,parents,trashed,md5Checksum,size,version,shortcutDetails",
        )
        return await asyncio.to_thread(request.execute)

    async def content(self, file_id):
        credentials = self.drive.credentials
        if not credentials.valid:
            await asyncio.to_thread(credentials.refresh, Request())
        sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "GET",
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"alt": "media", "supportsAllDrives": "true"},
                headers={
                    "Authorization": f"Bearer {credentials.token}",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.status_code != 200:
                    raise RepairRejected("CONTENT_UNAVAILABLE")
                async for chunk in response.aiter_raw(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise RepairRejected("CONTENT_SIZE_LIMIT")
                    sha.update(chunk)
                    md5.update(chunk)
        return {"size": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}

    async def permissions(self, file_id):
        result, token = [], None
        for _ in range(20):
            request = self.drive.service.permissions().list(
                fileId=file_id,
                supportsAllDrives=True,
                pageSize=100,
                pageToken=token,
                fields="nextPageToken,permissions(id,type,role,deleted,allowFileDiscovery,expirationTime)",
            )
            page = await asyncio.to_thread(request.execute)
            result.extend(page.get("permissions", []))
            token = page.get("nextPageToken")
            if not token:
                return result
        raise RepairRejected("ACL_PAGE_LIMIT")

    async def target_ids(self, root, name):
        # Both inputs are controlled: validated opaque ID and canonical taxonomy name.
        request = self.drive.service.files().list(
            q=f"'{root}' in parents and name='{name}' and trashed=false",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=2,
            fields="files(id),nextPageToken",
        )
        page = await asyncio.to_thread(request.execute)
        if page.get("nextPageToken"):
            raise RepairRejected("TARGET_AMBIGUOUS")
        return [item["id"] for item in page.get("files", [])]


def acl_fingerprint(permissions: list[dict]) -> str:
    effective = []
    for permission in permissions:
        if permission.get("deleted"):
            continue
        if not all(permission.get(key) for key in ("id", "type", "role")):
            raise RepairRejected("ACL_INCOMPLETE")
        effective.append(
            {
                key: permission.get(key)
                for key in ("id", "type", "role", "allowFileDiscovery", "expirationTime")
            }
        )
    if not effective:
        raise RepairRejected("ACL_EMPTY")
    return digest(sorted(effective, key=lambda row: json.dumps(row, sort_keys=True)))


_CLIENT_SQL = """SELECT id, passport_number, full_name, date_of_birth,
    client_type, status, deleted_at, google_drive_folder_id, drive_folder_id FROM clients
    WHERE id=$1 OR google_drive_folder_id=$2 OR drive_folder_id=$2"""
_DOCUMENT_SQL = """SELECT * FROM documents WHERE file_id=$1
    OR substring(google_drive_file_url from '/d/([A-Za-z0-9_-]+)')=$1
    OR substring(google_drive_file_url from '[?&]id=([A-Za-z0-9_-]+)')=$1
    OR substring(file_url from '/d/([A-Za-z0-9_-]+)')=$1
    OR substring(file_url from '[?&]id=([A-Za-z0-9_-]+)')=$1"""
_CONFLICT_SQL = """SELECT
    EXISTS(SELECT 1 FROM company_documents WHERE google_drive_file_id=$1
      OR substring(google_drive_file_url from '/d/([A-Za-z0-9_-]+)')=$1
      OR substring(google_drive_file_url from '[?&]id=([A-Za-z0-9_-]+)')=$1)
    OR EXISTS(SELECT 1 FROM invoices WHERE drive_file_id=$1)
    OR EXISTS(SELECT 1 FROM practices p CROSS JOIN LATERAL
      jsonb_path_query(p.documents::jsonb, '$.** ? (@.type() == "string")') AS value
      WHERE value #>> '{}' = $1
        OR substring(value #>> '{}' from '/d/([A-Za-z0-9_-]+)') = $1
        OR substring(value #>> '{}' from '[?&]id=([A-Za-z0-9_-]+)') = $1)
    OR EXISTS(SELECT 1 FROM lkpm_receipts WHERE file_drive_id=$1)
    OR EXISTS(SELECT 1 FROM weekly_cashout WHERE receipt_drive_file_id=$1)"""


def provenance(row: dict[str, Any], *, keep_in_place: bool = False) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "evidence_sha256": digest(row),
        "original_parent_id": row["source_parent_id"],
        "planned_target_id": None if keep_in_place else row["target_folder_id"],
        "move_status": "not_planned" if keep_in_place else "pending",
    }


async def inspect_record(conn, drive, row, *, snapshot=None, keep_in_place=False):
    clients = await conn.fetch(
        _CLIENT_SQL + (" FOR UPDATE" if snapshot is not None else ""),
        row["client_id"],
        row["root_id"],
    )
    if len(clients) != 1:
        raise RepairRejected("ROOT_AMBIGUOUS")
    owner = clients[0]
    if (
        owner["id"] != row["client_id"]
        or owner["deleted_at"] is not None
        or (owner["status"] or "").lower() == "archived"
        or (owner["client_type"] or "individual") != "individual"
        or owner["google_drive_folder_id"] != row["root_id"]
    ):
        raise RepairRejected("OWNER_CHANGED")
    for field, expected, kind in (
        ("passport_number", "passport_sha256", "number"),
        ("full_name", "name_sha256", "name"),
        ("date_of_birth", "dob_sha256", "dob"),
    ):
        if identity_hash(owner[field], kind) != row[expected]:
            raise RepairRejected("IDENTITY_MISMATCH")
    matches = await conn.fetch(
        """SELECT id FROM clients WHERE deleted_at IS NULL
        AND lower(coalesce(status,'')) <> 'archived'
        AND upper(regexp_replace(coalesce(passport_number,''),'[^A-Za-z0-9]','','g'))=$1""",
        re.sub(r"[^A-Za-z0-9]", "", owner["passport_number"]).upper(),
    )
    if [m["id"] for m in matches] != [row["client_id"]]:
        raise RepairRejected("IDENTITY_AMBIGUOUS")
    if await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM companies
        WHERE google_drive_folder_id=ANY($1::text[]) OR tax_dept_folder_id=ANY($1::text[]))
        OR EXISTS(SELECT 1 FROM client_drive_subfolders WHERE subfolder_id=ANY($1::text[]) AND client_id<>$2)
        OR EXISTS(SELECT 1 FROM clients WHERE id<>$2 AND
           (google_drive_folder_id=$3 OR drive_folder_id=$3))""",
        [row["root_id"], row["target_folder_id"]],
        row["client_id"],
        row["target_folder_id"],
    ):
        raise RepairRejected("ROOT_REFERENCE_CONFLICT")
    if await conn.fetchval(_CONFLICT_SQL, row["file_id"]):
        raise RepairRejected("FILE_REFERENCE_CONFLICT")
    existing = await conn.fetch(
        _DOCUMENT_SQL + (" FOR UPDATE" if snapshot is not None else ""), row["file_id"]
    )
    expected_notes = json.dumps(provenance(row, keep_in_place=keep_in_place), sort_keys=True)
    expected_key = "repair:" + digest({"record": row, "keep_in_place": keep_in_place})
    if existing:
        if len(existing) != 1 or any(
            existing[0][k] != v
            for k, v in {
                "client_id": row["client_id"],
                "file_id": row["file_id"],
                "document_type": row["document_type"],
                "document_category": row["document_category"],
                "intake_idempotency_key": expected_key,
                "intake_proposal_id": None,
                "notes": expected_notes,
                "file_name": "Recovered document",
                "ocr_status": "skipped",
                "file_url": f"https://drive.google.com/file/d/{row['file_id']}/view",
                "google_drive_file_url": f"https://drive.google.com/file/d/{row['file_id']}/view",
                "family_member_id": None,
                "practice_id": None,
                "storage_type": "google_drive",
            }.items()
        ):
            raise RepairRejected("DOCUMENT_REFERENCE_CONFLICT")
    if snapshot is not None:
        return snapshot, existing[0]["id"] if existing else None
    source, root, target = [
        await drive.metadata(row[k]) for k in ("file_id", "root_id", "target_folder_id")
    ]
    if (
        source.get("id") != row["file_id"]
        or source.get("trashed") is not False
        or source.get("mimeType") not in {"application/pdf", "image/jpeg"}
        or source.get("shortcutDetails")
        or source.get("parents") != [row["source_parent_id"]]
        or source.get("md5Checksum") != row["source_md5"]
        or str(source.get("size")) != str(row["source_size"])
        or str(source.get("version")) != str(row["source_version"])
    ):
        raise RepairRejected("SOURCE_DRIFT")
    content = await drive.content(row["file_id"])
    if (
        content["size"] != row["source_size"]
        or content["sha256"] != row["source_content_sha256"]
        or content["md5"] != row["source_md5"]
    ):
        raise RepairRejected("CONTENT_MISMATCH")
    del content
    if await drive.metadata(row["file_id"]) != source:
        raise RepairRejected("SOURCE_DRIFT")
    canonical = folder_for_category(row["document_category"])
    for item, fid, parent in (
        (root, row["root_id"], drive.individual_root),
        (target, row["target_folder_id"], row["root_id"]),
    ):
        if (
            not parent
            or item.get("id") != fid
            or item.get("mimeType") != FOLDER_MIME
            or item.get("trashed") is not False
            or item.get("parents") != [parent]
            or item.get("shortcutDetails")
        ):
            raise RepairRejected("TARGET_TOPOLOGY")
    if target.get("name") != canonical or await drive.target_ids(row["root_id"], canonical) != [
        row["target_folder_id"]
    ]:
        raise RepairRejected("TARGET_AMBIGUOUS")
    source_acl = await drive.permissions(row["file_id"])
    acl_hash = acl_fingerprint(source_acl)
    if not keep_in_place and acl_hash != acl_fingerprint(
        await drive.permissions(row["target_folder_id"])
    ):
        raise RepairRejected("ACL_MISMATCH")
    snapshot = {
        "evidence_sha256": digest(row),
        "acl_sha256": acl_hash,
        "root_sha256": digest(root),
        "target_sha256": digest(target),
    }
    return snapshot, existing[0]["id"] if existing else None


async def run_repair(
    conn, drive, manifest, *, apply=False, expected_plan_sha256=None, keep_in_place=False
):
    records = validate_manifest(manifest)
    snapshots = [
        await inspect_record(conn, drive, row, keep_in_place=keep_in_place) for row in records
    ]
    plan_sha = digest(
        {
            "manifest": manifest,
            "snapshots": [s[0] for s in snapshots],
            "keep_in_place": keep_in_place,
        }
    )
    if not apply:
        return {
            "mode": "plan",
            "plan_sha256": plan_sha,
            "count": len(records),
            "writes": 0,
            "move_status": "not_planned" if keep_in_place else "pending",
        }
    if expected_plan_sha256 != plan_sha:
        raise RepairRejected("PLAN_STALE")
    # Refresh external snapshots before the short DB transaction. The command
    # never mutates Drive; the separate move must still compare its source version.
    refreshed = [
        await inspect_record(conn, drive, row, keep_in_place=keep_in_place) for row in records
    ]
    if (
        digest(
            {
                "manifest": manifest,
                "snapshots": [s[0] for s in refreshed],
                "keep_in_place": keep_in_place,
            }
        )
        != plan_sha
    ):
        raise RepairRejected("PLAN_DRIFT")
    results = []
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        keys = sorted(
            {f"client:{r['client_id']}" for r in records}
            | {f"file:{r['file_id']}" for r in records}
        )
        for key in keys:
            lock_id = int.from_bytes(
                hashlib.sha256((SOURCE + key).encode()).digest()[:8], "big", signed=True
            )
            await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_id)
        current = [
            await inspect_record(conn, drive, row, snapshot=prior[0], keep_in_place=keep_in_place)
            for row, prior in zip(records, refreshed, strict=True)
        ]
        for row, (_, existing_id) in zip(records, current, strict=True):
            if existing_id is not None:
                results.append(
                    {"file_id": row["file_id"], "document_id": existing_id, "reused": True}
                )
                continue
            doc_id = await write_client_document(
                conn,
                row["client_id"],
                {
                    "document_type": row["document_type"],
                    "document_category": row["document_category"],
                    "file_name": "Recovered document",
                    "file_id": row["file_id"],
                    "file_url": f"https://drive.google.com/file/d/{row['file_id']}/view",
                    "google_drive_file_url": f"https://drive.google.com/file/d/{row['file_id']}/view",
                    "client_visible": False,
                    "ocr_status": "skipped",
                    "notes": json.dumps(
                        provenance(row, keep_in_place=keep_in_place), sort_keys=True
                    ),
                },
                proposal_id=None,
                idempotency_key="repair:" + digest({"record": row, "keep_in_place": keep_in_place}),
                registration_source=SOURCE,
            )
            results.append({"file_id": row["file_id"], "document_id": doc_id, "reused": False})
    return {
        "mode": "registered_hidden",
        "plan_sha256": plan_sha,
        "records": results,
        "move_status": "not_planned" if keep_in_place else "pending",
    }
