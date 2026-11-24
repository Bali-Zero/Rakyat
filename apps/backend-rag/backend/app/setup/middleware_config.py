"""
Middleware Configuration Module

Handles registration of all middleware components for FastAPI application.
"""

import logging

from fastapi import FastAPI

from backend.app.setup.cors_config import register_cors_middleware
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import HybridAuthMiddleware
from backend.middleware.rate_limiter import RateLimitMiddleware
from backend.middleware.request_tracing import RequestTracingMiddleware

logger = logging.getLogger("zantara.backend")


def _register_brotli_middleware(app: FastAPI) -> bool:
    """
    Register Brotli compression middleware if available.
    Falls back to GZip if brotli-asgi is not installed.
    Returns True if Brotli was registered, False otherwise.
    """
    try:
        from brotli_asgi import BrotliMiddleware

        app.add_middleware(
            BrotliMiddleware,
            minimum_size=500,  # Only compress responses > 500 bytes
            gzip_fallback=True,  # Fall back to gzip if client doesn't support br
        )
        return True
    except ImportError:
        try:
            from starlette.middleware.gzip import GZipMiddleware

            app.add_middleware(GZipMiddleware, minimum_size=500)
            logger.warning("⚠️ brotli-asgi not installed, using GZip fallback")
            return False
        except Exception:
            logger.warning("⚠️ No compression middleware available")
            return False


def register_middleware(app: FastAPI, *, rate_limit: bool = True) -> None:
    """
    Register all middleware components on FastAPI application.

    Starlette runs the middleware added LAST first, so the order of the calls below is
    the reverse of the order on a request, which is:

    1. Activity Logging (audit trail)
    2. Error Monitoring (monitors 4xx/5xx errors, sends alerts)
    3. Request Tracing (correlation IDs, end-to-end tracing)
    4. Hybrid Authentication (establishes who is calling)
    5. Rate Limiting (counts the caller authentication established)
    6. Brotli/GZip Compression
    7. CORS (innermost: it wraps the routes, not the middleware above)

    Rate limiting runs after authentication so that it can count the verified caller;
    what authentication refuses itself (401, the role boundary's 403, 503) is not counted.

    Args:
        app: FastAPI application instance
        rate_limit: False for an app that is reached only through another app of the
            same deployment which already enforces the limits (the RAG worker behind
            the API's proxy route): one request is charged once, by the app that
            faces the edge.
    """
    # CORS is added first, which makes it the innermost middleware.
    register_cors_middleware(app)

    # Add Brotli compression (or GZip fallback) - compresses all API responses
    has_brotli = _register_brotli_middleware(app)
    compression = "Brotli" if has_brotli else "GZip"

    # Add Rate Limiting middleware (prevents API abuse, DoS protection). Added before
    # authentication, so it runs after it.
    if rate_limit:
        app.add_middleware(RateLimitMiddleware)

    # Add Hybrid Authentication middleware
    app.add_middleware(HybridAuthMiddleware)

    # Add Request Tracing middleware (correlation IDs, end-to-end tracing)
    app.add_middleware(RequestTracingMiddleware)

    # Add Error Monitoring middleware (monitors 4xx/5xx errors, sends alerts)
    app.add_middleware(ErrorMonitoringMiddleware)

    # Add Activity Logging middleware (logs all API calls for audit trail)
    app.add_middleware(ActivityLoggingMiddleware)

    logger.info(
        "✅ Middleware registered: CORS + %s + %sAuth + Tracing + ErrorMonitoring + ActivityLogging",
        compression,
        "RateLimiting + " if rate_limit else "",
    )
