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
from backend.core.qdrant_db import QdrantClient

logger = get_logger(__name__)

router = APIRouter(tags=["intel-analytics"])

_CRITICAL_MAX_POINTS = 5000


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


@router.post("/api/intel/search")
async def search_intel(request: IntelSearchRequest) -> dict[str, Any]:
    """Search intel news with semantic search"""
    try:
        # Generate query embedding
        query_embedding = await get_embedder().generate_single_embedding(request.query)

        # Determine collections to search
        if request.category:
            collection_names = [INTEL_COLLECTIONS.get(request.category)]
        else:
            collection_names = list(INTEL_COLLECTIONS.values())

        all_results = []
        searched = 0
        failed = 0

        for collection_name in collection_names:
            if not collection_name:
                continue

            searched += 1
            try:
                client = QdrantClient(collection_name=collection_name)

                # Build metadata filter
                where_filter = {"tier": {"$in": request.tier}}

                # Add date range filter
                if request.date_range != "all":
                    days_map = {
                        "today": 1,
                        "last_7_days": 7,
                        "last_30_days": 30,
                        "last_90_days": 90,
                    }
                    days = days_map.get(request.date_range, IntelConstants.DUPLICATE_CHECK_DAYS)
                    cutoff_date = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
                    where_filter["published_date"] = {"$gte": cutoff_date}

                # Add impact level filter
                if request.impact_level:
                    where_filter["impact_level"] = request.impact_level

                # Search (async)
                results = await client.search(
                    query_embedding=query_embedding,
                    filter=where_filter,
                    limit=request.limit,
                )

                # Parse results
                for doc, metadata, distance in zip(
                    results.get("documents", []),
                    results.get("metadatas", []),
                    results.get("distances", []),
                    strict=True,
                ):
                    similarity_score = 1 / (1 + distance)

                    all_results.append(
                        {
                            "id": metadata.get("id"),
                            "title": metadata.get("title"),
                            "summary_english": doc[: IntelConstants.SUMMARY_PREVIEW_LENGTH],
                            "summary_italian": metadata.get("summary_italian", ""),
                            "source": metadata.get("source"),
                            "tier": metadata.get("tier"),
                            "published_date": metadata.get("published_date"),
                            "category": collection_name.replace("bali_intel_", ""),
                            "impact_level": metadata.get("impact_level"),
                            "url": metadata.get("url"),
                            "key_changes": metadata.get("key_changes"),
                            "action_required": metadata.get("action_required") == "True",
                            "deadline_date": metadata.get("deadline_date"),
                            "similarity_score": similarity_score,
                        },
                    )

            except Exception as e:
                failed += 1
                logger.warning("Error searching collection %s: %s", collection_name, e)
                continue

        if searched and failed == searched:
            raise HTTPException(status_code=500, detail="Intel search unavailable")

        # Sort by similarity score
        all_results.sort(key=lambda x: x["similarity_score"], reverse=True)

        # Limit total results
        all_results = all_results[: request.limit]

        return {"results": all_results, "total": len(all_results)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Intel search error: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


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

    Points are matched on `metadata.impact_level` by the vector database; the date
    range is applied here because the date is stored as text, which a range filter
    cannot compare. The scroll is bounded by `_CRITICAL_MAX_POINTS` per collection;
    when a collection exceeds it, or fails, the answer carries `partial: true`.
    """
    try:
        if category:
            collection_names = [INTEL_COLLECTIONS.get(category)]
        else:
            collection_names = list(INTEL_COLLECTIONS.values())

        critical_items = []
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        searched = 0
        failed = 0
        partial = False

        for collection_name in dict.fromkeys(collection_names):
            if not collection_name:
                continue

            searched += 1
            try:
                client = QdrantClient(collection_name=collection_name)
                points = await client.scroll_strict(
                    metadata_filter={"impact_level": "critical"},
                    max_points=_CRITICAL_MAX_POINTS,
                )
            except RuntimeError as bound_error:
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
                published = metadata.get("published_at") or metadata.get("published_date")
                published_at = _parse_stored_date(published)
                if published_at is None or published_at < cutoff:
                    continue
                collection_items.append(
                    {
                        "id": metadata.get("id"),
                        "title": metadata.get("title"),
                        "source": metadata.get("source") or metadata.get("source_name"),
                        "tier": metadata.get("tier"),
                        "published_date": published,
                        "category": collection_name.replace("bali_intel_", ""),
                        "url": metadata.get("url") or metadata.get("source_url"),
                        "action_required": metadata.get("action_required") == "True",
                        "deadline_date": metadata.get("deadline_date"),
                        "severity": "high",  # All critical items are high severity for chain compatibility
                    },
                )
            collection_items.sort(
                key=lambda x: _parse_stored_date(x["published_date"]), reverse=True
            )
            critical_items.extend(collection_items[:50])

        if searched and failed == searched:
            raise HTTPException(status_code=500, detail="Critical items unavailable")

        # Sort by date (newest first)
        critical_items.sort(key=lambda x: _parse_stored_date(x["published_date"]), reverse=True)

        # Return with both 'items' and 'alerts' keys for backward compatibility
        response: dict[str, Any] = {
            "items": critical_items,
            "alerts": critical_items,  # Alias for chain_daily_ops_autopilot compatibility
            "count": len(critical_items),
        }
        if partial:
            response["partial"] = True
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Get critical items error: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/intel/trends")
async def get_trends(
    category: str | None = None,
    _days: int = IntelConstants.TRENDS_ANALYSIS_DAYS,
) -> dict[str, Any]:
    """Get trending topics and keywords"""
    try:
        if category:
            collection_names = [INTEL_COLLECTIONS.get(category)]
        else:
            collection_names = list(INTEL_COLLECTIONS.values())

        all_keywords = []

        for collection_name in collection_names:
            if not collection_name:
                continue

            try:
                client = QdrantClient(collection_name=collection_name)
                stats = await client.get_stats()
                if "error" in stats:
                    raise RuntimeError(stats["error"])

                all_keywords.append(
                    {
                        "collection": collection_name.replace("bali_intel_", ""),
                        "total_items": stats.get("total_documents", 0),
                    },
                )

            except Exception:
                continue

        return {
            "trends": all_keywords,
            "top_topics": [],  # Would require NLP analysis
        }

    except Exception as e:
        logger.error("Get trends error: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


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
