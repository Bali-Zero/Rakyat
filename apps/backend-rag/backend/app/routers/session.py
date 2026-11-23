"""
Session Management API Router

Only the health check is left. The conversation-session routes (create, read,
overwrite, extend, info, export, delete, analytics, cleanup) had no caller in
the repository, and every route that named a session acted on whatever id the
caller supplied: any staff or API-key caller could read or rewrite another
person's conversation, and the analytics overview published session ids. They
are retired rather than given an owner check nobody would exercise.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends

from backend.app.core.config import settings
from backend.app.deps.auth import reject_portal_roles
from backend.services.misc.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/sessions",
    tags=["sessions"],
    dependencies=[Depends(reject_portal_roles)],
)

# Global service instance
_session_service: SessionService | None = None


def get_session_service() -> SessionService:
    """Get or create SessionService instance"""
    global _session_service
    if _session_service is None:
        redis_url = getattr(settings, "redis_url", "redis://localhost:6379")
        _session_service = SessionService(redis_url)
    return _session_service


@router.get("/health")
async def health_check(service: SessionService = Depends(get_session_service)) -> dict[str, Any]:
    """Health check for session service"""
    try:
        healthy = await service.health_check()
        return {"success": healthy, "service": "session"}
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return {"success": False, "service": "session", "error": str(e)}
