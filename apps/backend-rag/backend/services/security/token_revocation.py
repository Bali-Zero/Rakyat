"""
Redis-backed token revocation service (S03 Sprint 2).

Uses Redis SETEX for O(1) token revocation checks.
Authentication fails closed when the revocation store is unavailable.
"""

import logging
import math
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

USER_REVOCATION_TTL_SECONDS = 86400
USER_ACTIVE_CACHE_TTL_SECONDS = 60.0
USER_ACTIVE_CACHE_MAX_ENTRIES = 2048

_user_active_cache: OrderedDict[str, tuple[bool, float]] = OrderedDict()
_revocation_db_pool: Any | None = None


class RevocationStoreUnavailable(RuntimeError):
    """Raised when session revocation cannot be checked or persisted safely."""


def set_revocation_db_pool(pool: Any | None) -> None:
    """Register or clear the shared asyncpg pool used for active-user checks."""
    global _revocation_db_pool
    _revocation_db_pool = pool
    _user_active_cache.clear()


def clear_user_active_cache(user_email: str | None = None) -> None:
    """Evict one user or the entire active-user TTL cache."""
    if user_email is None:
        _user_active_cache.clear()
        return
    normalized_email = user_email.strip().lower()
    if normalized_email:
        _user_active_cache.pop(normalized_email, None)


def _get_cached_user_active(normalized_email: str) -> bool | None:
    entry = _user_active_cache.get(normalized_email)
    if entry is None:
        return None
    is_active, cached_at = entry
    if time.monotonic() - cached_at >= USER_ACTIVE_CACHE_TTL_SECONDS:
        _user_active_cache.pop(normalized_email, None)
        return None
    _user_active_cache.move_to_end(normalized_email)
    return is_active


def _set_cached_user_active(normalized_email: str, is_active: bool) -> None:
    _user_active_cache[normalized_email] = (is_active, time.monotonic())
    _user_active_cache.move_to_end(normalized_email)
    while len(_user_active_cache) > USER_ACTIVE_CACHE_MAX_ENTRIES:
        _user_active_cache.popitem(last=False)


def is_token_revocation_enabled() -> bool:
    """Return the runtime revocation policy without caching environment state."""
    from backend.app.core.config import settings

    return bool(settings.enable_token_revocation)


class TokenRevocationService:
    """
    Token revocation via Redis + bounded 60s active-user verification.

    Per-token: SETEX revoked:{jti} <ttl> "reason"
    Per-user: SETEX revoked_user:{email} 86400 "<revoked_at_epoch>"
    Active-user: 60s TTL cache backed by team_members.active when db_pool is set

    Fail-closed: Redis or DB error = authentication or revocation is denied.
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        db_pool: Any | None = None,
    ) -> None:
        self._redis = redis_client
        self._db_pool = _revocation_db_pool if db_pool is None else db_pool

    async def revoke_token(
        self,
        jti: str,
        ttl_seconds: int,
        reason: str = "manual",
    ) -> bool:
        if not self._redis:
            raise RevocationStoreUnavailable("Token revocation store is unavailable")
        try:
            await self._redis.setex(f"revoked:{jti}", ttl_seconds, reason)
            logger.info("S03: Token revoked reason=%s ttl=%ss", reason, ttl_seconds)
            return True
        except (RedisError, OSError) as e:
            logger.error("S03: Token revocation store error: %s", type(e).__name__)
            raise RevocationStoreUnavailable("Token revocation store error") from e
        except Exception as e:
            logger.exception("S03: Unexpected token revocation store error")
            raise RevocationStoreUnavailable("Token revocation store error") from e

    async def is_revoked(self, jti: str) -> bool:
        if not self._redis:
            raise RevocationStoreUnavailable("Token revocation store is unavailable")
        try:
            result = await self._redis.exists(f"revoked:{jti}")
            return bool(result)
        except (RedisError, OSError) as e:
            logger.error("S03: Revocation check store error: %s", type(e).__name__)
            raise RevocationStoreUnavailable("Token revocation store error") from e
        except Exception as e:
            logger.exception("S03: Unexpected revocation check store error")
            raise RevocationStoreUnavailable("Token revocation store error") from e

    async def revoke_all_user_tokens(
        self,
        user_email: str,
        reason: str = "bulk_revoke",
    ) -> bool:
        if not self._redis:
            raise RevocationStoreUnavailable("Token revocation store is unavailable")
        try:
            normalized_email = user_email.strip().lower()
            revoked_at = time.time()
            await self._redis.setex(
                f"revoked_user:{normalized_email}",
                USER_REVOCATION_TTL_SECONDS,
                str(revoked_at),
            )
            clear_user_active_cache(normalized_email)
            logger.info("S03: All user tokens revoked reason=%s", reason)
            return True
        except (RedisError, OSError) as e:
            logger.error("S03: User revocation store error: %s", type(e).__name__)
            raise RevocationStoreUnavailable("Token revocation store error") from e
        except Exception as e:
            logger.exception("S03: Unexpected user revocation store error")
            raise RevocationStoreUnavailable("Token revocation store error") from e

    async def is_user_revoked(self, user_email: str, token_issued_at: Any) -> bool:
        if not self._redis:
            raise RevocationStoreUnavailable("Token revocation store is unavailable")
        try:
            revoked_at = await self._redis.get(f"revoked_user:{user_email.lower()}")
            return _is_issued_at_revoked(token_issued_at, revoked_at)
        except (RedisError, OSError) as e:
            logger.error("S03: User revocation check store error: %s", type(e).__name__)
            raise RevocationStoreUnavailable("Token revocation store error") from e
        except Exception as e:
            logger.exception("S03: Unexpected user revocation check store error")
            raise RevocationStoreUnavailable("Token revocation store error") from e

    async def is_user_deactivated(self, user_email: str) -> bool:
        """Return True when the user's team_members row has active=false (cached 60s)."""
        normalized_email = user_email.strip().lower()
        if not normalized_email:
            return False

        cached_active = _get_cached_user_active(normalized_email)
        if cached_active is not None:
            return not cached_active

        if self._db_pool is None:
            return False

        try:
            async with self._db_pool.acquire() as conn:
                active = await conn.fetchval(
                    "SELECT active FROM team_members WHERE LOWER(email) = $1 LIMIT 1",
                    normalized_email,
                )
        except Exception as e:
            logger.error("S03: Active-user check DB error: %s", type(e).__name__)
            raise RevocationStoreUnavailable("Active-user verification store error") from e

        is_active = bool(active) if active is not None else True
        _set_cached_user_active(normalized_email, is_active)
        return not is_active


def _is_issued_at_revoked(token_issued_at: Any, revoked_at: Any) -> bool:
    """Return whether a token predates a user-level revocation marker.

    A missing marker means that no bulk revocation is active. Once a marker
    exists, malformed marker data or a missing/malformed ``iat`` fails closed:
    accepting an unverifiable token would defeat the revoke-all guarantee.
    """
    if revoked_at is None:
        return False

    try:
        if isinstance(revoked_at, bytes):
            revoked_at = revoked_at.decode("ascii")
        if isinstance(token_issued_at, bool) or isinstance(revoked_at, bool):
            raise ValueError("boolean timestamps are invalid")

        issued_at_value = float(token_issued_at)
        revoked_at_value = float(revoked_at)
        if (
            not math.isfinite(issued_at_value)
            or not math.isfinite(revoked_at_value)
            or issued_at_value <= 0
            or revoked_at_value <= 0
        ):
            raise ValueError("timestamps must be finite positive numbers")
    except (TypeError, ValueError, UnicodeDecodeError):
        logger.error("S03: Invalid user revocation timestamp state; denying session")
        return True

    return issued_at_value <= revoked_at_value


async def is_session_revoked(payload: Mapping[str, Any]) -> bool:
    """Check token- and user-level revocation using the shared async Redis client."""
    if not is_token_revocation_enabled():
        return False

    from backend.core.redis_manager import RedisManager

    redis_client = RedisManager.get_instance().get_async_client()
    service = TokenRevocationService(redis_client=redis_client)
    jti = str(payload.get("jti") or "")
    user_email = str(payload.get("email") or payload.get("sub") or "").strip().lower()
    token_issued_at = payload.get("iat")

    if jti and await service.is_revoked(jti):
        return True
    if user_email and await service.is_user_revoked(user_email, token_issued_at):
        return True
    return bool(user_email and await service.is_user_deactivated(user_email))


def is_session_revoked_sync(payload: Mapping[str, Any]) -> bool:
    """Check token- and user-level revocation using the shared sync Redis client.

    Also consults the in-memory 60s active-user cache populated by the async
    ``HybridAuthMiddleware`` path before route dependencies execute.
    """
    if not is_token_revocation_enabled():
        return False

    from backend.core.redis_manager import RedisManager

    redis_client = RedisManager.get_instance().get_sync_client()
    if not redis_client:
        raise RevocationStoreUnavailable("Token revocation store is unavailable")

    jti = str(payload.get("jti") or "")
    user_email = str(payload.get("email") or payload.get("sub") or "").strip().lower()
    token_issued_at = payload.get("iat")
    try:
        if jti and redis_client.exists(f"revoked:{jti}"):
            return True
        revoked_at = redis_client.get(f"revoked_user:{user_email}") if user_email else None
        if user_email and _is_issued_at_revoked(token_issued_at, revoked_at):
            return True
        return bool(user_email and _get_cached_user_active(user_email) is False)
    except (RedisError, OSError) as e:
        logger.error("S03: Sync revocation check store error: %s", type(e).__name__)
        raise RevocationStoreUnavailable("Token revocation store error") from e
    except Exception as e:
        logger.exception("S03: Unexpected sync revocation check store error")
        raise RevocationStoreUnavailable("Token revocation store error") from e
