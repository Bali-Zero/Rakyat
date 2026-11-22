"""
Rate Limiting Middleware for ZANTARA
Prevents API abuse and ensures fair usage

Features:
- One counter per caller and concrete path: the signed-in user, the API key, the
  shared service credential, or the address the edge saw
- Configurable limits per endpoint
- Redis-backed sliding window (one atomic script), in-memory fallback
"""

import hashlib
import hmac
import ipaddress
import logging
import math
import secrets
import time
import uuid
from functools import cache
from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.app.utils.logging_utils import sanitize_log_path
from backend.middleware.visa_oracle_privacy import is_private_visa_evaluation
from backend.services.pii.violation_store import hash_subject

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Who is counted
# --------------------------------------------------------------------------- #
_JWT_AUTH_METHODS = frozenset({"jwt_header", "jwt_cookie", "jwt_stateless"})
#: Each of these is ONE credential shared by its callers: one shared principal, on purpose.
_SERVICE_AUTH_METHODS = frozenset({"internal_key", "admin_key"})
_EDGE_ADDRESS_HEADER = "fly-client-ip"


@cache
def _subject_digest_key() -> bytes:
    """Key for credential digests, derived from the JWT secret a deployment shares."""
    from backend.app.core.config import settings

    secret = settings.jwt_secret_key
    if not secret:
        return secrets.token_bytes(32)
    return hmac.new(secret.encode("utf-8"), b"rate-limit-subject-key", hashlib.sha256).digest()


def _credential_digest(credential: str) -> str:
    digest = hmac.new(_subject_digest_key(), credential.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def _edge_address(request: Request) -> str:
    """The caller's address as the edge saw it, else the socket peer.

    The edge overwrites ``Fly-Client-IP`` on every request it forwards. It is believed
    only when the request carries exactly one such header and its value is an address;
    ``X-Forwarded-For`` and ``X-Real-IP`` are never read, a client can write them.
    """
    values = request.headers.getlist(_EDGE_ADDRESS_HEADER)
    if len(values) == 1 and isinstance(values[0], str):
        try:
            return str(ipaddress.ip_address(values[0].strip()))
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


def rate_limit_subject(request: Request) -> str:
    """Name who a request is counted against. Namespaced, never a constant.

    - ``user:<e-mail>`` for a session authentication verified (header, cookie or
      stateless JWT);
    - ``key:<digest>`` for an API key: a keyed digest of the whole key authentication
      has just verified, so two keys of one role are two callers;
    - ``svc:<method>`` for the internal and the admin key, shared principals;
    - ``addr:<address>`` for everything else: public routes with or without
      credentials, an unknown method, an identity with no usable field.
    """
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        method = user.get("auth_method")
        if method in _JWT_AUTH_METHODS:
            email = user.get("email")
            if isinstance(email, str) and email.strip():
                return f"user:{email.strip().lower()}"
        elif method == "api_key":
            api_key = request.headers.get("X-API-Key")
            if isinstance(api_key, str) and api_key:
                return f"key:{_credential_digest(api_key)}"
        elif method in _SERVICE_AUTH_METHODS:
            return f"svc:{method}"
    return f"addr:{_edge_address(request)}"


# --------------------------------------------------------------------------- #
# How it is counted (Redis)
# --------------------------------------------------------------------------- #
# One atomic step: drop what left the window, count, admit or refuse. Only an admitted
# request is stored, so a refusal changes neither the members nor the expiry. Every
# number that matters arrives as a string the app formatted (no Lua float conversion);
# the oldest score goes back as the string Redis holds.
# KEYS[1] = counter. ARGV = now, now - window, limit, window in ms, member.
_SLIDING_WINDOW_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
local count = redis.call('ZCARD', KEYS[1])
local admitted = 0
if count < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[1], ARGV[5])
  redis.call('PEXPIRE', KEYS[1], ARGV[4])
  count = count + 1
  admitted = 1
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {admitted, count, oldest[2]}
"""

# In-memory rate limit storage (fallback) with eviction
_rate_limit_storage: dict[str, list[float]] = {}
_MAX_RATE_LIMIT_KEYS = 10_000
_EVICTION_STALE_SECONDS = 120


def _evict_stale_keys() -> None:
    """Evict stale keys to prevent unbounded memory growth."""
    global _rate_limit_storage
    if len(_rate_limit_storage) <= _MAX_RATE_LIMIT_KEYS:
        return
    cutoff = time.time() - _EVICTION_STALE_SECONDS
    _rate_limit_storage = {k: v for k, v in _rate_limit_storage.items() if v and v[-1] > cutoff}


class RateLimiter:
    """
    Sliding-window rate limiter.

    - Primary backend: Redis via `RedisManager` (distributed across replicas),
      one atomic script per request; every admitted request is one member.
    - Fallback: in-memory dict with stricter limits (half the configured
      per-route limit) when Redis is unreachable or returns errors.
    - Self-healing: after a Redis error flips `redis_available` to False,
      subsequent requests try to reconnect on a cooldown (see
      `_RECOVERY_COOLDOWN`). This way a transient Redis hiccup doesn't
      leave the limiter permanently degraded.

    Observability:
    - `self.metrics` exposes running counters that the admin
      `/api/admin/rate-limit/stats` endpoint reads.
    """

    _RECOVERY_COOLDOWN = 10.0  # seconds between reconnect attempts

    def __init__(self) -> None:
        self.redis_available = False
        self.redis_client = None
        self._last_recovery_attempt: float = 0.0
        self._last_error: str | None = None
        self.metrics: dict[str, int] = {
            "redis_requests": 0,
            "redis_errors": 0,
            "memory_fallback_requests": 0,
            "recovery_attempts": 0,
            "recovery_successes": 0,
            "refused": 0,
        }
        #: Refusals by the table entry that set the threshold (bounded by the table).
        self.refused_by_bucket: dict[str, int] = {}
        self._script: Any = None
        self._script_client: Any = None

        self._connect_from_manager()

    def _connect_from_manager(self) -> None:
        """(Re)acquire a sync Redis client from the central manager."""
        # Get sync Redis client from centralized RedisManager
        from backend.core.redis_manager import RedisManager

        manager = RedisManager.get_instance()
        client = manager.get_sync_client()
        # Every bind starts without a script: it is registered again, for this
        # client, on the first request that needs it (see `_window_script`).
        self._script = None
        self._script_client = None
        if client is not None:
            self.redis_client = client
            self.redis_available = True
            manager.register_component("rate_limiter", "active")
            logger.info("Rate limiter using Redis via RedisManager")
        else:
            self.redis_client = None
            self.redis_available = False
            manager.register_component("rate_limiter", "fallback_memory")
            logger.warning(
                "Rate limiter using in-memory storage — no Redis available, rate limits not shared across workers"
            )

    def refresh_from_manager(self) -> bool:
        """Deterministically refresh the backend from RedisManager.

        Service initialization can create RedisManager after this module-level
        singleton was imported. This method lets startup code rebind the sync
        Redis client immediately instead of waiting for the request-time
        recovery cooldown.
        """
        was_available = self.redis_available
        self._connect_from_manager()
        if not was_available and self.redis_available:
            self.metrics["recovery_successes"] += 1
            self._last_error = None
        return self.redis_available

    def _try_recover(self) -> None:
        """Attempt to reconnect to Redis if we've been in fallback mode."""
        now = time.time()
        if now - self._last_recovery_attempt < self._RECOVERY_COOLDOWN:
            return
        self._last_recovery_attempt = now
        self.metrics["recovery_attempts"] += 1
        was_unavailable = not self.redis_available
        self._connect_from_manager()
        if was_unavailable and self.redis_available:
            self.metrics["recovery_successes"] += 1
            self._last_error = None
            logger.info("Rate limiter Redis connection recovered")

    def _window_script(self) -> Any:
        """The counting script, bound to the client in use.

        Registered again after every bind (start, refresh, recovery) and whenever
        the client object changes: a script object is never kept across clients.
        Registering sends nothing; a call sends EVALSHA, and redis-py loads the
        script and repeats the call when the server answers NOSCRIPT.
        """
        client = self.redis_client
        if self._script is None or self._script_client is not client:
            self._script = client.register_script(_SLIDING_WINDOW_SCRIPT)
            self._script_client = client
        return self._script

    def record_refusal(self, bucket: str) -> None:
        """Count a refused request, in total and for the table entry that matched."""
        self.metrics["refused"] += 1
        self.refused_by_bucket[bucket] = self.refused_by_bucket.get(bucket, 0) + 1

    def is_allowed(
        self, key: str, limit: int, window: int, *, now: float | None = None
    ) -> tuple[bool, dict]:
        """
        Check if request is allowed under rate limit

        Args:
            key: Unique identifier (caller and path)
            limit: Max requests allowed
            window: Time window in seconds
            now: The time of the request (defaults to the clock)

        Returns:
            (allowed, info_dict). On the Redis backend ``reset`` is the second the
            oldest admitted request leaves the window and a refusal carries
            ``retry_after``; the in-memory paths keep ``reset = now + window``.
        """
        if now is None:
            now = time.time()
        current_time = int(now)
        window_start = current_time - window

        # If we previously degraded to in-memory, try to reconnect on a
        # cooldown so a transient Redis outage doesn't leave the limiter
        # permanently unshared across workers.
        if not self.redis_available:
            self._try_recover()

        try:
            if self.redis_available and self.redis_client:
                # Redis-backed sliding window
                self.metrics["redis_requests"] += 1
                reply = self._window_script()(
                    keys=[key],
                    args=[
                        f"{now:.6f}",
                        f"{now - window:.6f}",
                        int(limit),
                        int(window) * 1000,
                        uuid.uuid4().hex,
                    ],
                )
                allowed = int(reply[0]) == 1
                count = int(reply[1])
                oldest = reply[2] if len(reply) > 2 else None
                # The window frees a slot when its oldest admitted request leaves it.
                leaves_at = float(oldest) + window if oldest is not None else now + window
                info = {
                    "limit": limit,
                    "remaining": max(0, limit - count),
                    "reset": math.ceil(leaves_at),
                    "backend": "redis",
                }
                if not allowed:
                    info["retry_after"] = min(window, max(1, math.ceil(leaves_at - now)))
                return allowed, info
            else:
                # In-memory fallback (with eviction guard)
                self.metrics["memory_fallback_requests"] += 1
                _evict_stale_keys()
                if key not in _rate_limit_storage:
                    _rate_limit_storage[key] = []

                # Remove old entries
                _rate_limit_storage[key] = [t for t in _rate_limit_storage[key] if t > window_start]

                count = len(_rate_limit_storage[key])
                allowed = count < limit

                if allowed:
                    _rate_limit_storage[key].append(current_time)

                remaining = max(0, limit - count - 1)

                return allowed, {
                    "limit": limit,
                    "remaining": remaining,
                    "reset": current_time + window,
                    "backend": "memory",
                }

        except Exception as exc:
            # Redis call raised mid-flight. Record the error and use the
            # half-limit in-memory fallback for THIS request. The server may have
            # run the script before the failure: that request is then held by both
            # counters, an over-count of at most one per failed call. We intentionally
            # do NOT flip self.redis_available to False here — the existing
            # behavior is to retry Redis on every call (per-request
            # fail-safe). The `_try_recover` cooldown path above handles the
            # other failure mode: Redis was down at boot and comes back.
            self.metrics["redis_errors"] += 1
            error_type = type(exc).__name__
            self._last_error = error_type
            logger.warning(
                "Rate limit Redis error, falling back to in-memory: %s",
                error_type,
            )
            # Fail-safe: in-memory with half limit so a Redis outage doesn't
            # multiply effective capacity by the number of replicas.
            safe_limit = max(1, limit // 2)
            if key not in _rate_limit_storage:
                _rate_limit_storage[key] = []
            _rate_limit_storage[key] = [t for t in _rate_limit_storage[key] if t > window_start]
            count = len(_rate_limit_storage[key])
            allowed = count < safe_limit
            if allowed:
                _rate_limit_storage[key].append(current_time)
            remaining = max(0, safe_limit - count - 1)
            return allowed, {
                "limit": safe_limit,
                "remaining": remaining,
                "reset": current_time + window,
                "backend": "memory_degraded",
            }


# Global rate limiter instance
rate_limiter = RateLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce rate limits on API endpoints
    """

    # Rate limit configuration per endpoint pattern
    #: GARUDA VOA public operations promise the frozen contract's flat
    #: RATE_LIMITED envelope and privacy headers on a 429
    #: (`products/garuda-voa/contracts/errors.yaml`); the generic body below
    #: is what every other path keeps.
    GARUDA_CONTRACT_PREFIX = "/api/visa/voa/"
    GARUDA_RATE_LIMITED_BODY = {
        "code": "RATE_LIMITED",
        "retryable": True,
        "message_key": "garuda_voa.error.rate_limited",
    }
    GARUDA_PRIVACY_HEADERS = {
        "Cache-Control": "no-store, private",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    }

    #: Thresholds that apply to ONE path and never to a longer one; read before the
    #: table below. The phone upsert is the write path of the intake and the WhatsApp
    #: mirror, whose measured peak is about 200 a minute from one address.
    EXACT_RATE_LIMITS = {
        "/api/crm/clients/upsert-by-phone": (600, 60),  # 600 per minute, per caller address
    }

    #: What a browser may read on a 429 from another origin.
    CORS_EXPOSED_HEADERS = (
        "Retry-After, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset"
    )

    RATE_LIMITS = {
        # Strict limits for expensive operations
        "/api/agents/journey/create": (10, 3600),  # 10 per hour
        "/api/agents/compliance/track": (20, 3600),  # 20 per hour
        "/api/agents/ingestion/run": (5, 3600),  # 5 per hour
        # CRM endpoints - moderate limits
        "/api/crm/clients": (100, 60),  # 100 per minute
        "/api/crm/practices": (100, 60),  # 100 per minute
        "/api/crm/interactions": (150, 60),  # 150 per minute (more frequent)
        "/api/crm/shared-memory": (60, 60),  # 60 per minute (expensive queries)
        # CRM stats endpoints - stricter limits (cached but still protect)
        "/api/crm/clients/stats": (30, 60),  # 30 per minute
        "/api/crm/practices/stats": (30, 60),  # 30 per minute
        "/api/crm/interactions/stats": (30, 60),  # 30 per minute
        "/api/crm/shared-memory/team-overview": (20, 60),  # 20 per minute
        "/api/crm/shared-memory/upcoming-renewals": (20, 60),  # 20 per minute
        # Moderate limits for read operations
        "/api/agents/journey/": (60, 60),  # 60 per minute
        "/api/agents/compliance/": (60, 60),  # 60 per minute
        "/api/agents/": (100, 60),  # 100 per minute
        # Generous limits for general endpoints
        "/bali-zero/chat": (30, 60),  # 30 per minute (includes reranker usage)
        "/search": (60, 60),  # 60 per minute
        "/api/": (120, 60),  # 120 per minute
        # Reranker-specific endpoints (if any in future)
        "/rerank": (100, 60),  # 100 per minute (anti-abuse)
        # Public endpoint rate limits (Security Audit 2026-01-13)
        "/api/intel/scraper/submit": (10, 60),  # 10 per minute - prevent spam
        "/api/intel/staging/approve/": (20, 60),  # 20 per minute - prevent abuse
        "/api/audio/": (30, 60),  # 30 per minute - prevent cost abuse (TTS/STT)
        "/api/whatsapp/send": (30, 60),  # 30 per minute - prevent send/message abuse
        "/api/knowledge/visa": (100, 60),  # 100 per minute - public knowledge base
        # Visa Oracle v2 evaluate read-path (W1) — dedicated bucket, tighter
        # than the generic /api/ 120/min: exact path beats the /api/ prefix.
        "/api/visa-oracle/evaluate": (30, 60),  # 30 per minute - public engine evaluation
        "/api/public/tax-calendar/obligations": (30, 60),  # 30 per minute - public tax calendar
        "/api/legal/parent-documents": (20, 60),  # 20 per minute - internal ingestion
        # Default for all other endpoints
        "*": (200, 60),  # 200 per minute
    }

    def __init__(self, app, dispatch=None) -> None:
        super().__init__(app, dispatch)
        # CORS wraps the routes, inside this middleware: a 429 written here never
        # passes through it, so the allowlist is read once and applied by hand.
        from backend.app.setup.cors_config import get_allowed_origins

        self._cors_origins = frozenset(get_allowed_origins())

    def _cors_headers(self, request: Request) -> dict[str, str]:
        """CORS headers for a 429, for an origin of the application's own allowlist."""
        origin = request.headers.get("origin")
        if not isinstance(origin, str) or origin not in self._cors_origins:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Expose-Headers": self.CORS_EXPOSED_HEADERS,
            "Vary": "Origin",
        }

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks. A preflight is not counted either: CORS
        # answers it, and a refused preflight is an opaque error in the browser.
        if request.method == "OPTIONS" or request.url.path in [
            "/health",
            "/docs",
            "/openapi.json",
        ]:
            return await call_next(request)

        # Who is counted: the identity authentication verified (it runs before this
        # middleware), else the address the edge saw.
        subject = rate_limit_subject(request)

        # Find matching rate limit
        bucket, limit, window = self._match_rate_limit(request.url.path)
        log_path = sanitize_log_path(request.url.path)

        # Fail-open if the limiter malfunctions beyond its internal Redis fallback —
        # a hot-path middleware bug must not take the whole API offline.
        subject_reference = hash_subject(subject) or "unknown"
        rate_limit_key = f"ratelimit:{subject_reference}:{log_path}"
        try:
            allowed, info = rate_limiter.is_allowed(rate_limit_key, limit, window)
        except Exception as exc:
            logger.error(
                "Rate limiter failure, failing open for %s (%s)",
                log_path,
                type(exc).__name__,
            )
            return await call_next(request)

        if not allowed:
            # A refusal is counted here, by threshold; it is not an alert.
            rate_limiter.record_refusal(bucket)
            if is_private_visa_evaluation(request.method, request.url.path):
                logger.warning("Visa Oracle evaluation rate limit exceeded")
            else:
                logger.warning("Rate limit exceeded on %s", log_path)
            headers = {
                "X-RateLimit-Limit": str(info["limit"]),
                "X-RateLimit-Remaining": str(info["remaining"]),
                "X-RateLimit-Reset": str(info["reset"]),
                # The Redis backend knows when a slot frees; the fallbacks say the window.
                "Retry-After": str(info.get("retry_after", window)),
                **self._cors_headers(request),
            }
            if request.url.path.startswith(self.GARUDA_CONTRACT_PREFIX):
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content=dict(self.GARUDA_RATE_LIMITED_BODY),
                    headers={**headers, **self.GARUDA_PRIVACY_HEADERS},
                )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": "Rate limit exceeded",
                    "message": f"Too many requests. Limit: {limit} per {window}s",
                    "limit": info["limit"],
                    "remaining": info["remaining"],
                    "reset": info["reset"],
                },
                headers=headers,
            )

        # Add rate limit headers to response
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(info["limit"])
        response.headers["X-RateLimit-Remaining"] = str(info["remaining"])
        response.headers["X-RateLimit-Reset"] = str(info["reset"])

        return response

    def _match_rate_limit(self, path: str) -> tuple[str, int, int]:
        """The table entry that sets the threshold of a path, with its limit and window.

        An entry selects a THRESHOLD; the counter itself is per caller and concrete path.
        """
        # A path with a threshold of its own
        if path in self.EXACT_RATE_LIMITS:
            return (path, *self.EXACT_RATE_LIMITS[path])

        # Try exact match first
        if path in self.RATE_LIMITS:
            return (path, *self.RATE_LIMITS[path])

        # Try prefix match
        for pattern, limit_config in self.RATE_LIMITS.items():
            if pattern != "*" and path.startswith(pattern):
                return (pattern, *limit_config)

        # Default rate limit
        return ("*", *self.RATE_LIMITS["*"])

    def _get_rate_limit(self, path: str) -> tuple[int, int]:
        """Find matching rate limit for path"""
        _, limit, window = self._match_rate_limit(path)
        return limit, window


def get_rate_limit_stats() -> dict:
    """Get rate limiting statistics"""
    return {
        "backend": "redis" if rate_limiter.redis_available else "memory",
        "connected": rate_limiter.redis_available,
        "rate_limits_configured": len(
            set(RateLimitMiddleware.RATE_LIMITS) | set(RateLimitMiddleware.EXACT_RATE_LIMITS)
        ),
        "metrics": dict(rate_limiter.metrics),
        "refused_by_bucket": dict(rate_limiter.refused_by_bucket),
        "last_error": rate_limiter._last_error,
        "in_memory_keys": len(_rate_limit_storage),
        "recovery_cooldown_seconds": RateLimiter._RECOVERY_COOLDOWN,
    }
