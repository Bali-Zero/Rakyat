"""
Intel Analytics & Metrics Endpoints

Split from intel.py for maintainability.
Provides: system metrics, semantic search, store, critical items,
trends, analytics summary, collection stats.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.app.core.constants import IntelConstants
from backend.app.dependencies import get_current_user
from backend.app.routers.intel import (
    INTEL_COLLECTIONS,
    IntelSearchRequest,
    IntelStoreRequest,
    analytics_service,
    get_embedder,
    staging_service,
)
from backend.app.utils.crm_utils import is_crm_admin
from backend.app.utils.logging_utils import get_logger
from backend.core.qdrant_db import QdrantClient, QdrantScrollBoundExceeded

logger = get_logger(__name__)

router = APIRouter(tags=["intel-analytics"])

_CRITICAL_MAX_POINTS = 5000
_DEFAULT_SEARCH_TIERS = frozenset({"T1", "T2", "T3"})
_SEARCH_MAX_RESULTS = 50
_SEARCH_FILTERED_OVERFETCH = 5
_SEARCH_FETCH_CAP = 200

# The collections whose points are classified by impact level. Empty: nothing writes
# `metadata.impact_level` today, so no collection can answer "critical". A change that
# adds such a producer adds its collection here (and the keyword index it needs).
_CLASSIFIED_COLLECTIONS: frozenset[str] = frozenset()


def _require_store_admin(user: dict[str, Any]) -> None:
    """Gate direct Qdrant upserts to admins.

    store_intel writes straight into the shared intel Qdrant collections
    with no validation/staging step, so an authenticated team member is not
    a sufficient principal (Case OS R3).
    """
    if not is_crm_admin(user):
        raise HTTPException(status_code=403, detail="Store requires admin")


# ─── System metrics ───────────────────────────────────────────────────────────


@router.get("/api/intel/metrics")
async def get_system_metrics() -> Any:
    """Get real-time system metrics for System Pulse dashboard"""
    logger.info("System metrics requested", extra={"endpoint": "/api/intel/metrics"})

    try:
        # Check agent health from autonomous scheduler
        agent_status = "unknown"
        last_run = None
        try:
            try:
                from backend.services.misc.autonomous_scheduler import get_autonomous_scheduler

                autonomous_scheduler = get_autonomous_scheduler()
            except Exception:
                autonomous_scheduler = None

            if autonomous_scheduler and autonomous_scheduler.tasks:
                recent_runs = [
                    task
                    for task in autonomous_scheduler.tasks.values()
                    if task.last_run
                    and (datetime.now(tz=timezone.utc) - task.last_run)
                    < timedelta(hours=IntelConstants.RECENT_TASK_THRESHOLD_HOURS)
                ]
                if recent_runs:
                    agent_status = "active"
                    last_run = max(recent_runs, key=lambda t: t.last_run or datetime.min).last_run
                    if last_run:
                        last_run = last_run.isoformat()
                elif any(task.enabled for task in autonomous_scheduler.tasks.values()):
                    agent_status = "idle"
                else:
                    agent_status = "disabled"
            else:
                agent_status = "not_configured"
        except Exception as e:
            logger.warning("Could not check agent health: %s", e)
            agent_status = "unknown"

        # Calculate metrics
        # NOTE: this endpoint previously also returned uptime_percentage (a fixed
        # 99.8 literal), avg_response_time_ms (synthesized from archived-file
        # content length, not a real latency measurement), next_scheduled_run
        # (last_approved + a hardcoded interval, not read from any scheduler
        # config) and qdrant_health (constructing a QdrantClient never contacts
        # the server, so this was always "healthy" unless the constructor itself
        # raised). None of the four had a live consumer — the only page that
        # rendered them (/intelligence/system-pulse) is already deleted from
        # kita, and `intelligenceApi.getMetrics()` has no other caller — so they
        # were removed rather than shipped as fake data (kita pruning wave 2d).
        metrics = {
            "agent_status": agent_status,
            "last_run": last_run,
            "items_processed_today": 0,
        }

        # Count pending items using staging service
        visa_dir = staging_service.get_staging_dir("visa")
        news_dir = staging_service.get_staging_dir("news")
        visa_count = len(list(visa_dir.glob("*.json"))) if visa_dir.exists() else 0
        news_count = len(list(news_dir.glob("*.json"))) if news_dir.exists() else 0
        metrics["items_processed_today"] = visa_count + news_count

        # Check last processed item (most recent archive)
        last_approved = None
        for archive_type in ["visa", "news"]:
            archive_dir = staging_service.get_staging_dir(archive_type) / "archived" / "approved"
            if archive_dir.exists():
                for file_path in sorted(
                    archive_dir.glob("*.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    try:
                        with open(file_path) as f:
                            data = json.load(f)
                            last_run_time = data.get("ingested_at")
                            if last_run_time:
                                last_approved = last_run_time
                                break
                    except Exception:
                        continue
            if last_approved:
                break

        if last_approved:
            metrics["last_run"] = last_approved

        logger.info(
            "System metrics calculated",
            extra={
                "agent_status": metrics["agent_status"],
                "items_processed": metrics["items_processed_today"],
            },
        )

        return metrics

    except Exception as e:
        logger.error("Failed to calculate system metrics: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Metrics calculation failed: {e!s}") from e


def _target_collections(category: str | None) -> list[str]:
    """The distinct collections a request reads.

    A category selects a collection through the map and nothing else: the stored
    `metadata.category` is never filtered on. Several categories map to one
    collection, which is then read once.
    """
    if category:
        names = [INTEL_COLLECTIONS.get(category)]
    else:
        names = list(INTEL_COLLECTIONS.values())
    return [name for name in dict.fromkeys(names) if name]


def _first_present(metadata: dict[str, Any], *keys: str) -> Any:
    """The value of the first key that is present, else None."""
    for key in keys:
        value = metadata.get(key)
        if value is not None:
            return value
    return None


def _publication(metadata: dict[str, Any]) -> tuple[Any, datetime | None]:
    """The raw publication value read and its date.

    `published_at`, else the legacy `published_date`; no other key is a publication
    date. A naive value is read as UTC; a value that does not parse has no date.
    """
    raw = _first_present(metadata, "published_at", "published_date")
    return raw, _parse_stored_date(raw)


def _search_cutoff(date_range: str) -> datetime | None:
    if date_range == "all":
        return None
    days_map = {
        "today": 1,
        "last_7_days": 7,
        "last_30_days": 30,
        "last_90_days": 90,
    }
    days = days_map.get(date_range, IntelConstants.DUPLICATE_CHECK_DAYS)
    return datetime.now(tz=timezone.utc) - timedelta(days=days)


@router.post("/api/intel/search")
async def search_intel(request: IntelSearchRequest) -> dict[str, Any]:
    """Search intel news with semantic search.

    The store is asked with no filter, because it refuses conditions on keys it has
    not indexed. A narrowed tier, an impact level and the date range are applied
    here over the fetched points; a point without the field does not match. A
    filtered search fetches a bounded window per collection and answers
    `window_full: true` when a window came back full and the filter dropped points
    from it. A collection that fails makes the answer `partial: true`; when every
    collection fails the answer is 500.
    """
    try:
        wanted = min(max(request.limit, 1), _SEARCH_MAX_RESULTS)
        cutoff = _search_cutoff(request.date_range)
        tiers = None if set(request.tier) == _DEFAULT_SEARCH_TIERS else set(request.tier)
        impact = request.impact_level or None
        filtered = cutoff is not None or tiers is not None or impact is not None
        fetch = min(wanted * _SEARCH_FILTERED_OVERFETCH, _SEARCH_FETCH_CAP) if filtered else wanted

        query_embedding = await get_embedder().generate_single_embedding(request.query)

        all_results: list[dict[str, Any]] = []
        searched = 0
        failed = 0
        window_full = False

        for collection_name in _target_collections(request.category):
            searched += 1
            try:
                client = QdrantClient(collection_name=collection_name)
                results = await client.search(
                    query_embedding=query_embedding,
                    limit=fetch,
                    raise_on_error=True,
                )
            except Exception as e:
                failed += 1
                logger.warning("Intel search failed for %s: %s", collection_name, e)
                continue

            ids = results.get("ids", [])
            dropped = 0
            collection_results: list[dict[str, Any]] = []
            for point_id, doc, metadata, distance in zip(
                ids,
                results.get("documents", []),
                results.get("metadatas", []),
                results.get("distances", []),
                strict=True,
            ):
                published, published_at = _publication(metadata)
                if (
                    (cutoff is not None and (published_at is None or published_at < cutoff))
                    or (tiers is not None and metadata.get("tier") not in tiers)
                    or (impact is not None and metadata.get("impact_level") != impact)
                ):
                    dropped += 1
                    continue
                collection_results.append(
                    {
                        "id": point_id,
                        "title": metadata.get("title"),
                        "summary_english": doc[: IntelConstants.SUMMARY_PREVIEW_LENGTH],
                        "summary_italian": metadata.get("summary_italian", ""),
                        "source": _first_present(metadata, "source_name", "source"),
                        "tier": metadata.get("tier"),
                        "published_date": published,
                        "category": collection_name.replace("bali_intel_", ""),
                        "impact_level": metadata.get("impact_level"),
                        "url": _first_present(metadata, "source_url", "url"),
                        "key_changes": metadata.get("key_changes"),
                        "action_required": metadata.get("action_required") == "True",
                        "deadline_date": metadata.get("deadline_date"),
                        "similarity_score": 1 / (1 + distance),
                    },
                )
            if dropped and len(ids) >= fetch:
                window_full = True
            all_results.extend(collection_results[:wanted])

        if searched and failed == searched:
            raise HTTPException(status_code=500, detail="Intel search unavailable")

        all_results.sort(key=lambda x: x["similarity_score"], reverse=True)
        all_results = all_results[:wanted]

        response: dict[str, Any] = {"results": all_results, "total": len(all_results)}
        if failed:
            response["partial"] = True
        if window_full:
            response["window_full"] = True
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Intel search error: %s", e)
        raise HTTPException(status_code=500, detail="Intel search unavailable") from e


@router.post("/api/intel/store")
async def store_intel(
    request: IntelStoreRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Store intel news item in Qdrant"""
    _require_store_admin(current_user)
    try:
        collection_name = INTEL_COLLECTIONS.get(request.collection)
        if not collection_name:
            raise HTTPException(status_code=400, detail=f"Invalid collection: {request.collection}")

        client = QdrantClient(collection_name=collection_name)

        await client.upsert_documents(
            chunks=[request.document],
            embeddings=[request.embedding],
            metadatas=[request.metadata],
            ids=[request.id],
        )

        return {"success": True, "collection": collection_name, "id": request.id}

    except Exception as e:
        logger.error("Store intel error: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


def _stored_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Intel points keep their fields under a `metadata` object (flat payloads tolerated)."""
    nested = payload.get("metadata")
    if isinstance(nested, dict):
        return nested
    return {key: value for key, value in payload.items() if key not in {"text", "metadata"}}


def _parse_stored_date(value: Any) -> datetime | None:
    """Dates are stored as ISO-8601 text; naive values are read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.get("/api/intel/critical")
async def get_critical_items(
    category: str | None = None,
    days: int = IntelConstants.DUPLICATE_CHECK_DAYS,
) -> dict[str, Any]:
    """Get critical impact items.

    Only a collection named in `_CLASSIFIED_COLLECTIONS` can answer: its points are
    matched on `metadata.impact_level` by a bounded strict scroll, and the date range
    is applied here. Any other target collection is not read at all; it is listed
    under `unclassified` with `classified: false`, so an empty list is not mistaken
    for "no critical item". An overflow of the bound, or a classified collection
    that fails, makes the answer `partial: true`; when every classified collection
    fails the answer is 500.
    """
    try:
        critical_items = []
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        unclassified: list[str] = []
        classified = 0
        failed = 0
        partial = False

        for collection_name in _target_collections(category):
            if collection_name not in _CLASSIFIED_COLLECTIONS:
                unclassified.append(collection_name)
                continue

            classified += 1
            try:
                client = QdrantClient(collection_name=collection_name)
                points = await client.scroll_strict(
                    metadata_filter={"impact_level": "critical"},
                    max_points=_CRITICAL_MAX_POINTS,
                )
            except QdrantScrollBoundExceeded as bound_error:
                logger.warning(
                    "Critical items read incomplete for %s: %s", collection_name, bound_error
                )
                partial = True
                continue
            except Exception as read_error:
                logger.warning("Critical items read failed for %s: %s", collection_name, read_error)
                failed += 1
                partial = True
                continue

            collection_items = []
            for point in points:
                metadata = _stored_metadata(point.get("payload", {}))
                if metadata.get("impact_level") != "critical":
                    continue
                published, published_at = _publication(metadata)
                if published_at is None or published_at < cutoff:
                    continue
                collection_items.append(
                    {
                        "id": point.get("id"),
                        "title": metadata.get("title"),
                        "source": _first_present(metadata, "source_name", "source"),
                        "tier": metadata.get("tier"),
                        "published_date": published,
                        "category": collection_name.replace("bali_intel_", ""),
                        "url": _first_present(metadata, "source_url", "url"),
                        "action_required": metadata.get("action_required") == "True",
                        "deadline_date": metadata.get("deadline_date"),
                        "severity": "high",  # All critical items are high severity for chain compatibility
                    },
                )
            collection_items.sort(
                key=lambda x: _parse_stored_date(x["published_date"]), reverse=True
            )
            critical_items.extend(collection_items[:50])

        if classified and failed == classified:
            raise HTTPException(status_code=500, detail="Critical items unavailable")

        # Sort by date (newest first)
        critical_items.sort(key=lambda x: _parse_stored_date(x["published_date"]), reverse=True)

        # Return with both 'items' and 'alerts' keys for backward compatibility
        response: dict[str, Any] = {
            "items": critical_items,
            "alerts": critical_items,  # Alias for chain_daily_ops_autopilot compatibility
            "count": len(critical_items),
        }
        if unclassified:
            response["classified"] = False
            response["unclassified"] = unclassified
        if partial:
            response["partial"] = True
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get critical items error: %s", e)
        raise HTTPException(status_code=500, detail="Critical items unavailable") from e


@router.get("/api/intel/trends")
async def get_trends(
    category: str | None = None,
    _days: int = IntelConstants.TRENDS_ANALYSIS_DAYS,
) -> dict[str, Any]:
    """Get the item count of each collection read (each collection once).

    A collection whose statistics fail is left out; when every read fails the
    answer is 500. `_days` is accepted and not used, and `top_topics` is empty.
    """
    try:
        all_keywords = []
        read = 0
        failed = 0

        for collection_name in _target_collections(category):
            read += 1
            try:
                client = QdrantClient(collection_name=collection_name)
                stats = await client.get_stats()
                if "error" in stats:
                    raise RuntimeError(stats["error"])
            except Exception as e:
                failed += 1
                logger.warning("Trends read failed for %s: %s", collection_name, e)
                continue

            all_keywords.append(
                {
                    "collection": collection_name.replace("bali_intel_", ""),
                    "total_items": stats.get("total_documents", 0),
                },
            )

        if read and failed == read:
            raise HTTPException(status_code=500, detail="Intel trends unavailable")

        return {
            "trends": all_keywords,
            "top_topics": [],  # Would require NLP analysis
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get trends error: %s", e)
        raise HTTPException(status_code=500, detail="Intel trends unavailable") from e


@router.get("/api/intel/analytics")
async def get_intelligence_analytics(days: int = IntelConstants.TRENDS_ANALYSIS_DAYS) -> Any:
    """Get historical analytics and trends for Intelligence Center"""
    logger.info("Analytics requested", extra={"endpoint": "/api/intel/analytics", "days": days})

    try:
        return analytics_service.get_intelligence_analytics(days)

    except Exception as e:
        logger.error("Failed to calculate analytics: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Analytics calculation failed: {e!s}",
        ) from e


@router.get("/api/intel/stats/{collection}")
async def get_collection_stats(collection: str) -> dict[str, Any]:
    """Get statistics for a specific intel collection"""
    try:
        collection_name = INTEL_COLLECTIONS.get(collection)
        if not collection_name:
            raise HTTPException(status_code=404, detail=f"Collection not found: {collection}")

        client = QdrantClient(collection_name=collection_name)
        stats = await client.get_stats()
        if "error" in stats:
            raise RuntimeError(stats["error"])

        return {
            "collection_name": collection_name,
            "total_documents": stats.get("total_documents", 0),
            "last_updated": datetime.now(tz=timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get stats error: %s", e)
        raise HTTPException(status_code=500, detail="Failed to read collection statistics") from e
