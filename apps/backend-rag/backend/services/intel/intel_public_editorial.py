"""Explicit, content-bound enrollment of public editorial items kept in visa staging."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from backend.services.intel.intel_staging_service import assert_valid_item_id

if TYPE_CHECKING:
    from backend.services.intel.intel_staging_service import IntelStagingService

_MARKER = "workspace_editorial"
# Publication bookkeeping changes do not authorize a different editorial package.
_OPERATIONAL = frozenset(
    {
        _MARKER,
        "status",
        "published_at",
        "published_url",
        "publication_requested_at",
        "publication_operation_id",
        "publication_lease_until",
        "publish_position",
        "github_commit_sha",
        "pull_request_number",
        "auto_merge_enabled",
        "mdx_path",
        "published_cover_path",
        "last_publication_failed_at",
        "live_verified_at",
    }
)
_EDITABLE = frozenset(
    {
        "title",
        "content",
        "category",
        "seo_title",
        "seo_description",
        "slug",
        "cover_image_alt",
    }
)


def editorial_fingerprint(item: dict[str, Any]) -> str:
    """Hash the complete non-operational record, including unknown future fields."""
    payload = {key: value for key, value in item.items() if key not in _OPERATIONAL}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def is_public_editorial(item: dict[str, Any] | None, item_id: str) -> bool:
    if not isinstance(item, dict):
        return False
    marker = item.get(_MARKER)
    return (
        item.get("item_id") == item_id
        and item.get("intel_type") == "visa"
        and isinstance(marker, dict)
        and marker.get("version") == 1
        and marker.get("fingerprint") == editorial_fingerprint(item)
    )


@contextmanager
def _locked(service: IntelStagingService, item_id: str) -> Iterator[None]:
    assert_valid_item_id(item_id)
    locks = service.get_staging_dir("visa") / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / f"{item_id}.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def enroll_public_editorial(
    service: IntelStagingService,
    item_id: str,
    source_url: str,
    fingerprint: str,
) -> bool:
    """Called only after admin authorization and explicit public-content confirmation.

    No source URL or taxonomy alone grants workspace access. The admin attests the
    exact reviewed record. A source writer's later change invalidates that consent.
    """
    with _locked(service, item_id):
        item = service.load_staging_item("visa", item_id)
        if item is None:
            raise LookupError("Item not found")
        source = urlsplit(source_url)
        if (
            item.get("item_id") != item_id
            or item.get("intel_type") != "visa"
            or item.get("status") != "pending"
            or item.get("published_url")
            or item.get("detection_type") != "scraper_auto"
            or item.get("extraction_method") != "editorial_manual"
            or item.get("source_url") != source_url
            or source.scheme != "https"
            or not source.hostname
            or source.username
            or source.password
            or editorial_fingerprint(item) != fingerprint
        ):
            raise ValueError("Item is not the reviewed pending public editorial record")
        if is_public_editorial(item, item_id):
            return False
        item[_MARKER] = {
            "version": 1,
            "fingerprint": fingerprint,
            "enrolled_at": datetime.now(timezone.utc).isoformat(),
            "authority": "admin-public-editorial-confirmation",
        }
        service.save_staging_item("visa", item_id, item)
        return True


def update_public_editorial(
    service: IntelStagingService,
    item_id: str,
    fingerprint: str,
    updates: dict[str, Any],
    *,
    cover: tuple[bytes, str] | None = None,
) -> None:
    """Apply an authorized workspace edit under the canonical item lock."""
    if updates.keys() - _EDITABLE:
        raise ValueError("Unsupported public editorial update")
    with _locked(service, item_id):
        item = service.load_staging_item("visa", item_id)
        if (
            not is_public_editorial(item, item_id)
            or editorial_fingerprint(item) != fingerprint
            or item.get("status") != "pending"
            or item.get("published_url")
        ):
            raise ValueError("Public editorial item changed or is not editable")
        if cover is not None:
            image, extension = cover
            if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                raise ValueError("Unsupported editorial cover format")
            # Content-addressed names prevent an old generic cover writer from
            # changing bytes at the path that this authorization refers to.
            name = f"{item_id}-{hashlib.sha256(image).hexdigest()}{extension}"
            covers = service.get_staging_dir("visa") / "covers"
            covers.mkdir(parents=True, exist_ok=True)
            (covers / name).write_bytes(image)
            item["cover_image"] = f"covers/{name}"
            item.pop("image_drive_file_id", None)
            item.pop("image_drive_url", None)
        item.update(updates)
        item[_MARKER]["fingerprint"] = editorial_fingerprint(item)
        service.save_staging_item("visa", item_id, item)


def enrolled_items(service: IntelStagingService) -> list[dict[str, Any]]:
    """Project only individually enrolled items; never return the private visa queue."""
    result = []
    for path in service.get_staging_dir("visa").glob("*.json"):
        item = service.load_staging_item("visa", path.stem)
        if is_public_editorial(item, path.stem):
            result.append(item)
    return result
