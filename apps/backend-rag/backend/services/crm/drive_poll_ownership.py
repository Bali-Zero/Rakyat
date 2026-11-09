"""Fail-closed ownership and billing-reference checks for automatic Drive ingestion."""

from collections import defaultdict
from typing import Any

from googleapiclient.errors import HttpError

from backend.app.core.config import settings
from backend.services.crm.drive_taxonomy import (
    canonical_folders,
    canonicalize_folder,
    physical_root_layout,
)

FOLDER_MIME = "application/vnd.google-apps.folder"


def ownership_maps(subfolders: list, clients: list) -> tuple[dict, dict]:
    """Keep historical references when detecting ambiguity; never choose a survivor."""
    owners: dict[str, set] = defaultdict(set)
    names: dict[str, set] = defaultdict(set)
    original_names: dict[str, set] = defaultdict(set)
    by_id = {c["id"]: c for c in clients}
    for client in clients:
        for key in ("google_drive_folder_id", "drive_folder_id"):
            if client.get(key):
                owners[client[key]].add(client["id"])
    for folder in subfolders:
        owners[folder["subfolder_id"]].add(folder["client_id"])
        original_names[folder["subfolder_id"]].add(folder["subfolder_name"])
        names[folder["subfolder_id"]].add(
            canonicalize_folder(canonicalize_folder(folder["subfolder_name"], "company"))
        )
    eligible = {
        cid: c["google_drive_folder_id"]
        for cid, c in by_id.items()
        if c.get("deleted_at") is None
        and c.get("status") != "archived"
        and (c.get("client_type") or "individual") in ("individual", "company")
        and c.get("google_drive_folder_id")
        and owners[c["google_drive_folder_id"]] == {cid}
    }
    roots = {root: cid for cid, root in eligible.items()}
    folders = {}
    for folder_id, labels in names.items():
        ids = owners[folder_id]
        if len(ids) == len(labels) == 1:
            cid = next(iter(ids))
            if cid in eligible and folder_id != eligible[cid]:
                original = original_names[folder_id]
                name = next(iter(original)) if len(original) == 1 else next(iter(labels))
                folders[folder_id] = (cid, name)
    return folders, roots


async def validate_client_root(
    conn: Any, drive: Any, client_id: int, root_id: str
) -> tuple[str, str]:
    """Validate ownership and return (stored client type, existing physical layout)."""
    rows = await conn.fetch(
        """SELECT id, google_drive_folder_id, drive_folder_id, client_type, deleted_at, status
           FROM clients
           WHERE id = $1 OR google_drive_folder_id = $2 OR drive_folder_id = $2""",
        client_id,
        root_id,
    )
    if len(rows) != 1:
        raise ValueError("Ambiguous Drive root owner")
    row = rows[0]
    client_type = row["client_type"] or "individual"
    if (
        row["id"] != client_id
        or row["deleted_at"] is not None
        or row.get("status") == "archived"
        or not root_id
        or row["google_drive_folder_id"] != root_id
        or client_type not in ("individual", "company")
    ):
        raise ValueError("Inactive or changed Drive root owner")
    root = await drive.get_file_metadata(root_id)
    layout = physical_root_layout(
        root,
        root_id,
        client_type,
        individuals_parent=settings.gdrive_individuals_folder_id,
        companies_parent=settings.gdrive_companies_folder_id,
    )
    return client_type, layout


async def revalidate_poll_owner(
    conn: Any, drive: Any, client_id: int, root_id: str, folder_id: str, folder_name: str
) -> bool:
    """Read current ownership and ancestry again at the write boundary."""
    try:
        client_type, layout = await validate_client_root(conn, drive, client_id, root_id)
        path = []
        current = folder_id
        for _ in range(8):
            if current == root_id:
                break
            if current in path:
                return False
            path.append(current)
            meta = await drive.get_file_metadata(current)
            if (
                meta.get("id") != current
                or meta.get("mimeType") != FOLDER_MIME
                or meta.get("trashed") is not False
                or len(meta.get("parents", [])) != 1
                or (
                    current == folder_id
                    and canonicalize_folder(meta.get("name", ""), layout)
                    != canonicalize_folder(folder_name.rsplit("/", 1)[-1], layout)
                )
            ):
                return False
            if meta["parents"] == [root_id] and canonicalize_folder(
                meta.get("name", ""), layout
            ) not in canonical_folders(layout):
                return False
            current = meta["parents"][0]
        if current != root_id:
            return False
        refs = await conn.fetch(
            """SELECT client_id, subfolder_name, subfolder_id FROM client_drive_subfolders
               WHERE subfolder_id = ANY($1::text[])""",
            [*path, root_id],
        )
        labels: dict[str, set] = defaultdict(set)
        for ref in refs:
            labels[ref["subfolder_id"]].add(canonicalize_folder(ref["subfolder_name"], layout))
        if any(len(names) != 1 for names in labels.values()) or any(
            ref["client_id"] != client_id for ref in refs
        ):
            return False
        if any(
            ref["subfolder_id"] == folder_id
            and canonicalize_folder(ref["subfolder_name"], layout)
            != canonicalize_folder(folder_name, layout)
            for ref in refs
        ):
            return False
        return not await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM clients
                 WHERE (google_drive_folder_id = ANY($1::text[])
                   OR drive_folder_id = ANY($1::text[])) AND id <> $2)
               OR EXISTS(SELECT 1 FROM companies WHERE google_drive_folder_id = ANY($1::text[])
                 OR tax_dept_folder_id = ANY($1::text[]))
               OR NOT EXISTS(SELECT 1 FROM clients WHERE id = $2
                 AND google_drive_folder_id = $3 AND deleted_at IS NULL
                 AND status IS DISTINCT FROM 'archived'
                 AND COALESCE(client_type, 'individual') = $4)""",
            [*path, root_id],
            client_id,
            root_id,
            client_type,
        )
    except HttpError as exc:
        if exc.resp.status == 404:
            return False
        raise
    except (ValueError, KeyError):
        return False


async def is_billing_file(conn: Any, file_id: str) -> bool:
    """Billing ownership is an exact ID reference, never a filename heuristic."""
    return bool(
        await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM invoices WHERE drive_file_id = $1)
           OR EXISTS(SELECT 1 FROM practices
                     WHERE documents->'invoice'->>'invoice_drive_id' = $1)""",
            file_id,
        )
    )
