"""Bounded CRM Drive mapping reconciliation. No Drive writes.

Clients with client_type=company are outside both cohorts; company roots use
the companies table. Client mapping created_at defaults in migration 062.
"""

import hashlib
import json
import re

from backend.services.crm.drive_taxonomy import canonical_folders

FOLDER = "application/vnd.google-apps.folder"
META_FIELDS = "id,name,mimeType,parents,trashed,version,modifiedTime"
MAX_ROOTS = 100
MAX_PAGES = 20
PAGE_SIZE = 1000
_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class RepairError(Exception):
    """A deliberately nonspecific failure, safe to report as a code."""


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def valid_ids(values):
    return (
        bool(values)
        and len(values) <= MAX_ROOTS
        and len(set(values)) == len(values)
        and all(_ID.fullmatch(v) for v in values)
    )


def _metadata(item):
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise RepairError("incomplete_inventory")
    # Missing trash/type/parent information cannot establish a safe destination.
    if (
        not isinstance(item.get("name"), str)
        or not isinstance(item.get("mimeType"), str)
        or not isinstance(item.get("parents"), list)
        or not all(isinstance(p, str) for p in item["parents"])
        or not isinstance(item.get("trashed"), bool)
    ):
        raise RepairError("incomplete_inventory")
    return {
        key: sorted(item[key]) if key == "parents" else item.get(key)
        for key in ("id", "name", "mimeType", "parents", "trashed", "version", "modifiedTime")
    }


def inventory_root(drive, root_id, parent_id, kind):
    files = drive.files()
    root = _metadata(
        files.get(fileId=root_id, fields=META_FIELDS, supportsAllDrives=True).execute()
    )
    if (
        root["id"] != root_id
        or root["mimeType"] != FOLDER
        or root["trashed"] is not False
        or root["parents"] != [parent_id]
    ):
        return {"reason": "invalid_root", "snapshot": [root], "destinations": {}, "other_count": 0}
    items, token, seen = [], None, set()
    for _ in range(MAX_PAGES):
        page = files.list(
            q=f"'{root_id}' in parents and trashed = false",
            fields=f"nextPageToken,incompleteSearch,files({META_FIELDS})",
            pageSize=PAGE_SIZE,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        if (
            not isinstance(page, dict)
            or page.get("incompleteSearch") is True
            or not isinstance(page.get("files"), list)
            or len(page["files"]) > PAGE_SIZE
        ):
            raise RepairError("incomplete_inventory")
        items.extend(_metadata(item) for item in page["files"])
        token = page.get("nextPageToken")
        if not token:
            break
        if not isinstance(token, str) or token in seen:
            raise RepairError("incomplete_inventory")
        seen.add(token)
    else:
        raise RepairError("incomplete_inventory")
    if len({item["id"] for item in items}) != len(items):
        raise RepairError("incomplete_inventory")
    groups = {name: [] for name in canonical_folders(kind)}
    other = 0
    for item in items:
        if item["name"] in groups:
            groups[item["name"]].append(item)
        else:
            other += 1
    reason = "ready"
    destinations = {}
    for name, group in groups.items():
        if len(group) > 1 or (
            group
            and (
                group[0]["mimeType"] != FOLDER
                or group[0]["trashed"] is not False
                or group[0]["parents"] != [root_id]
            )
        ):
            reason = "ambiguous_canonical"
        elif not group and reason == "ready":
            reason = "missing_canonical"
        elif group:
            destinations[name] = group[0]["id"]
    return {
        "reason": reason,
        "snapshot": [root] + sorted(items, key=lambda x: x["id"]),
        "destinations": destinations,
        "other_count": other,
    }


_REQUIRED = {
    "clients": {"id", "google_drive_folder_id", "deleted_at", "client_type"},
    "companies": {"id", "google_drive_folder_id", "tax_dept_folder_id"},
    "client_drive_subfolders": {"client_id", "subfolder_name", "subfolder_id"},
    "company_drive_subfolders": {"company_id", "folder_name", "drive_folder_id", "updated_at"},
}


async def check_schema(conn, extra=None):
    required = {**_REQUIRED, **(extra or {})}
    rows = await conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ANY($1::text[])",
        list(required),
    )
    actual = {(r["table_name"], r["column_name"]) for r in rows}
    if any((table, col) not in actual for table, cols in required.items() for col in cols):
        raise RepairError("schema_missing")


async def _bindings(conn, root, kind, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    if kind == "individual":
        rows = await conn.fetch(
            "SELECT id FROM clients WHERE google_drive_folder_id = $1 AND deleted_at IS NULL AND COALESCE(client_type, 'individual') = 'individual' ORDER BY id"
            + suffix,
            root,
        )
    else:
        rows = await conn.fetch(
            "SELECT id FROM companies WHERE google_drive_folder_id = $1 ORDER BY id" + suffix, root
        )
    secondary = await conn.fetch(
        "SELECT id FROM companies WHERE tax_dept_folder_id = $1 ORDER BY id", root
    )
    return [r["id"] for r in rows], [r["id"] for r in secondary]


async def _maps(conn, ids, kind):
    if not ids:
        return []
    if kind == "individual":
        query = "SELECT client_id AS entity_id, subfolder_name AS name, subfolder_id AS folder_id FROM client_drive_subfolders WHERE client_id = ANY($1::int[]) ORDER BY client_id, subfolder_name"
    else:
        query = "SELECT company_id AS entity_id, folder_name AS name, drive_folder_id AS folder_id FROM company_drive_subfolders WHERE company_id = ANY($1::int[]) ORDER BY company_id, folder_name"
    return [dict(row) for row in await conn.fetch(query, ids)]


async def _reverse_maps(conn, folder_ids, lock=False):
    if not folder_ids:
        return []
    suffix = " FOR UPDATE" if lock else ""
    results = []
    queries = (
        (
            "individual",
            "SELECT client_id AS entity_id, subfolder_name AS name, subfolder_id AS folder_id FROM client_drive_subfolders WHERE subfolder_id = ANY($1::text[]) ORDER BY client_id, subfolder_name",
        ),
        (
            "company",
            "SELECT company_id AS entity_id, folder_name AS name, drive_folder_id AS folder_id FROM company_drive_subfolders WHERE drive_folder_id = ANY($1::text[]) ORDER BY company_id, folder_name",
        ),
    )
    for kind, query in queries:
        rows = await conn.fetch(query + suffix, sorted(folder_ids))
        results.extend({"kind": kind, **dict(row)} for row in rows)
    return sorted(results, key=lambda r: (r["kind"], r["entity_id"], r["name"], r["folder_id"]))


def _changes(entry):
    if entry["reason"] != "ready":
        return []
    old_maps = {(row["entity_id"], row["name"]): row["folder_id"] for row in entry["maps"]}
    changes = []
    for entity in entry["entity_ids"]:
        for name, folder_id in entry["inventory"]["destinations"].items():
            old_id = old_maps.get((entity, name))
            if old_id == folder_id:
                continue
            inverse = {
                "operation": "restore_old_id"
                if old_id is not None
                else "remove_created_row_manually",
                "expected_current_id": folder_id,
            }
            if old_id is not None:
                inverse["restore_id"] = old_id
            changes.append(
                {
                    "entity_id": entity,
                    "folder_name": name,
                    "action": "update" if old_id is not None else "insert",
                    "old_folder_id": old_id,
                    "new_folder_id": folder_id,
                    "inverse": inverse,
                }
            )
    return changes


async def build_plan(conn, drive, roots, parent, kind, lock_reverse=False):
    if (
        kind not in ("individual", "company")
        or not valid_ids(roots)
        or not parent
        or not _ID.fullmatch(parent)
    ):
        raise RepairError("invalid_arguments")
    entries = []
    for root in sorted(roots):
        ids, secondary = await _bindings(conn, root, kind)
        maps = await _maps(conn, ids, kind)
        inventory = inventory_root(drive, root, parent, kind)
        destinations = inventory["destinations"]
        reverse = await _reverse_maps(conn, set(destinations.values()), lock=lock_reverse)
        if len(ids) > 1:
            reason = "ambiguous_owner"
        elif not ids:
            reason = "unbound_root"
        else:
            expected = {(folder_id, kind, ids[0], name) for name, folder_id in destinations.items()}
            actual = {
                (row["folder_id"], row["kind"], row["entity_id"], row["name"]) for row in reverse
            }
            reason = inventory["reason"] if not actual - expected else "conflicting_mapping"
        entries.append(
            {
                "root": root,
                "entity_ids": ids,
                "tax_secondary_ids": secondary,
                "maps": maps,
                "reverse_maps": reverse,
                "inventory": inventory,
                "reason": reason,
            }
        )
    return {"kind": kind, "parent": parent, "roots": entries}


def public_plan(plan):
    result = []
    for entry in plan["roots"]:
        inv = entry["inventory"]
        result.append(
            {
                "root_ref": digest(entry["root"]),
                "entity_ids": entry["entity_ids"],
                "tax_secondary_ids": entry["tax_secondary_ids"],
                "reason": entry["reason"],
                "other_count": inv["other_count"],
                "canonical_count": len(inv["destinations"]),
                "mapping_count": len(entry["maps"]),
                "shared_root": len(entry["entity_ids"]) > 1,
                "changes": _changes(entry),
            }
        )
    return {"plan_sha256": digest(plan), "entity_kind": plan["kind"], "roots": result}


async def _lock_cohort(conn, kind, entity_ids):
    # SHARE conflicts with rebinding UPDATE while remaining compatible with the
    # KEY SHARE normally acquired by foreign-key inserts into child tables.
    if kind == "individual":
        query = "SELECT id FROM clients WHERE id = ANY($1::int[]) ORDER BY id FOR SHARE"
        maps = "SELECT client_id FROM client_drive_subfolders WHERE client_id = ANY($1::int[]) ORDER BY client_id, subfolder_name FOR UPDATE"
    else:
        query = "SELECT id FROM companies WHERE id = ANY($1::int[]) ORDER BY id FOR SHARE"
        maps = "SELECT company_id FROM company_drive_subfolders WHERE company_id = ANY($1::int[]) ORDER BY company_id, folder_name FOR UPDATE"
    locked = await conn.fetch(query, entity_ids)
    if [row["id"] for row in locked] != entity_ids:
        raise RepairError("state_changed")
    await conn.fetch(maps, entity_ids)


_UPSERTS = {
    "individual": "INSERT INTO client_drive_subfolders (client_id, subfolder_name, subfolder_id) VALUES ($1, $2, $3) ON CONFLICT (client_id, subfolder_name) DO UPDATE SET subfolder_id = EXCLUDED.subfolder_id WHERE client_drive_subfolders.subfolder_id IS NOT DISTINCT FROM $4 RETURNING 1",
    "company": "INSERT INTO company_drive_subfolders (company_id, folder_name, drive_folder_id) VALUES ($1, $2, $3) ON CONFLICT (company_id, folder_name) DO UPDATE SET drive_folder_id = EXCLUDED.drive_folder_id, updated_at = now() WHERE company_drive_subfolders.drive_folder_id IS NOT DISTINCT FROM $4 RETURNING 1",
}


async def validate_upsert_sql(conn, kind):
    # EXPLAIN without ANALYZE plans the fixed statement but does not execute it.
    # PostgreSQL checks ON CONFLICT's supporting unique index while planning.
    try:
        await conn.fetch(
            "EXPLAIN " + _UPSERTS[kind], 0, canonical_folders(kind)[0], "dry-run", None
        )
    except Exception:
        raise RepairError("sql_validation_failed") from None


async def _apply_root(conn, drive, entry, parent, kind):
    async with conn.transaction(isolation="read_committed"):
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            "crm-drive-repair:" + entry["root"],
        )
        await _lock_cohort(conn, kind, entry["entity_ids"])
        current = (await build_plan(conn, drive, [entry["root"]], parent, kind, lock_reverse=True))[
            "roots"
        ][0]
        if current != entry:
            raise RepairError("state_changed")
        changes = _changes(entry)
        for change in changes:
            query = _UPSERTS[kind]
            if (
                await conn.fetchval(
                    query,
                    change["entity_id"],
                    change["folder_name"],
                    change["new_folder_id"],
                    change["old_folder_id"],
                )
                != 1
            ):
                raise RepairError("state_changed")
        after = (await build_plan(conn, drive, [entry["root"]], parent, kind, lock_reverse=True))[
            "roots"
        ][0]
        expected_maps = {(row["entity_id"], row["name"]): row["folder_id"] for row in entry["maps"]}
        expected_reverse = {
            (row["kind"], row["entity_id"], row["name"], row["folder_id"])
            for row in entry["reverse_maps"]
        }
        for change in changes:
            entity, name, old, new = (
                change["entity_id"],
                change["folder_name"],
                change["old_folder_id"],
                change["new_folder_id"],
            )
            expected_maps[(entity, name)] = new
            expected_reverse.discard((kind, entity, name, old))
            expected_reverse.add((kind, entity, name, new))
        actual_maps = {(row["entity_id"], row["name"]): row["folder_id"] for row in after["maps"]}
        actual_reverse = {
            (row["kind"], row["entity_id"], row["name"], row["folder_id"])
            for row in after["reverse_maps"]
        }
        if (
            {k: v for k, v in after.items() if k not in ("maps", "reverse_maps")}
            != {k: v for k, v in entry.items() if k not in ("maps", "reverse_maps")}
            or actual_maps != expected_maps
            or actual_reverse != expected_reverse
        ):
            raise RepairError("state_changed")
    return len(changes)


async def reconcile(pool, drive, roots, parent, kind, expected=None):
    async with pool.acquire() as conn:
        await check_schema(conn)
        if expected is not None and not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise RepairError("invalid_arguments")
        if expected is None:
            if kind not in _UPSERTS:
                raise RepairError("invalid_arguments")
            await validate_upsert_sql(conn, kind)
        before = await build_plan(conn, drive, roots, parent, kind)
        report = public_plan(before)
        if expected is None:
            return report
        if digest(before) != expected:
            raise RepairError("plan_changed")
        results = []
        for entry in before["roots"]:
            root_ref = digest(entry["root"])
            if entry["reason"] != "ready":
                results.append({"root_ref": root_ref, "status": entry["reason"]})
                continue
            try:
                count = await _apply_root(conn, drive, entry, parent, kind)
                results.append(
                    {
                        "root_ref": root_ref,
                        "status": "applied",
                        "upsert_attempts": count,
                        "changes": _changes(entry),
                    }
                )
            except RepairError as exc:
                results.append(
                    {
                        "root_ref": root_ref,
                        "status": "state_changed"
                        if str(exc) == "state_changed"
                        else "operation_failed",
                    }
                )
            except Exception:
                results.append({"root_ref": root_ref, "status": "operation_failed"})
        report["apply_results"] = results
        return report


_CATEGORIES = {"family", "personal", "immigration", "pma", "company", "tax", "other"}
_TYPES = {
    "passport",
    "kitas",
    "visa",
    "imk",
    "itk",
    "akta",
    "nib",
    "npwp",
    "spt",
    "lkpm",
    "profile_perseroan",
    "profile_document",
    "immigration_document",
    "misc_document",
    "company_document",
    "tax_document",
    "family_document",
    "spt_company",
    "spt_personal",
    "other",
}


def _safe_value(value, allowed):
    return value if value in allowed else "unknown"


async def inspect_files(pool, drive, file_ids):
    if not valid_ids(file_ids):
        raise RepairError("invalid_arguments")
    async with pool.acquire() as conn:
        await check_schema(
            conn,
            {
                "documents": {"client_id", "file_id", "document_type", "document_category"},
                "company_documents": {"company_id", "google_drive_file_id", "document_type"},
            },
        )
        results = []
        for file_id in sorted(file_ids):
            individual = await conn.fetch(
                "SELECT d.client_id AS entity_id, d.document_type, d.document_category, c.google_drive_folder_id AS root_id, c.deleted_at IS NULL AND COALESCE(c.client_type, 'individual') = 'individual' AS eligible FROM documents d LEFT JOIN clients c ON c.id = d.client_id WHERE d.file_id = $1 ORDER BY d.client_id",
                file_id,
            )
            company = await conn.fetch(
                "SELECT d.company_id AS entity_id, d.document_type, c.google_drive_folder_id AS root_id FROM company_documents d LEFT JOIN companies c ON c.id = d.company_id WHERE d.google_drive_file_id = $1 ORDER BY d.company_id",
                file_id,
            )
            owners = []
            for row in individual:
                owners.append(
                    {
                        "kind": "individual",
                        "entity_id": row["entity_id"],
                        "type": _safe_value(row["document_type"], _TYPES),
                        "category": _safe_value(row["document_category"], _CATEGORIES),
                        "root_ref": digest(row["root_id"]) if row["root_id"] else None,
                        "eligible": bool(row["eligible"] and row["root_id"]),
                    }
                )
            for row in company:
                owners.append(
                    {
                        "kind": "company",
                        "entity_id": row["entity_id"],
                        "type": _safe_value(row["document_type"], _TYPES),
                        "category": "unknown",
                        "root_ref": digest(row["root_id"]) if row["root_id"] else None,
                        "eligible": bool(row["root_id"]),
                    }
                )
            meta = _metadata(
                drive.files()
                .get(fileId=file_id, fields=META_FIELDS, supportsAllDrives=True)
                .execute()
            )
            if meta["id"] != file_id:
                raise RepairError("incomplete_inventory")
            reason = (
                "unmatched"
                if not owners
                else "ambiguous"
                if len(owners) > 1
                else "matched"
                if owners[0]["eligible"]
                else "unbound"
            )
            results.append(
                {
                    "file_ref": digest(file_id),
                    "parent_refs": sorted(digest(p) for p in meta["parents"]),
                    "trashed": meta["trashed"],
                    "reason": reason,
                    "matches": owners,
                }
            )
        return {"files": results}
