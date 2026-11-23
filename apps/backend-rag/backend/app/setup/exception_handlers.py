"""
Exception Handlers for FastAPI Application

Provides global exception handlers to ensure proper error serialization
and prevent TypeError when serializing HTTPException with non-serializable objects.
"""

import logging
import math
from typing import Any

import asyncpg
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.utils.logging_utils import sanitize_log_path

logger = logging.getLogger("zantara.backend")


_MAX_DETAIL_DEPTH = 32
_TRUNCATED_DETAIL = "[truncated]"


def _encodable_text(text: str) -> str:
    """Escape lone surrogates, which JSONResponse cannot encode as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text


def _sanitize_key(key: Any) -> Any:
    """Keep a key JSON accepts; anything else (a tuple, an object) becomes text."""
    if key is None or isinstance(key, (str, int, float)):
        return _sanitize_value(key, frozenset())
    return _encodable_text(str(key))


def _sanitize_value(value: Any, parents: frozenset[int]) -> Any:
    """Keep the structure of a detail value; only what JSON cannot carry becomes text.

    ``parents`` holds the containers above this value, so a cycle or an absurdly
    deep value ends in a fixed marker instead of a failed error response.
    """
    if isinstance(value, str):
        return _encodable_text(value)
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        # JSONResponse refuses NaN and Infinity (allow_nan=False).
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (dict, list, tuple)):
        if id(value) in parents or len(parents) >= _MAX_DETAIL_DEPTH:
            return _TRUNCATED_DETAIL
        inside = parents | {id(value)}
        if isinstance(value, dict):
            return {_sanitize_key(k): _sanitize_value(v, inside) for k, v in value.items()}
        return [_sanitize_value(item, inside) for item in value]
    return _encodable_text(str(value))


def sanitize_detail(detail: Any) -> str | dict[str, Any]:
    """
    Sanitize exception detail to ensure JSON serialization.

    Dicts and lists nested inside a dict detail keep their shape, so a client
    can read structured fields such as ``candidates[0].company_name``.

    Args:
        detail: Exception detail (can be str, dict, or any object)

    Returns:
        Sanitized detail (str or dict with only serializable values)
    """
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        return _sanitize_value(detail, frozenset())
    # Convert non-serializable objects to string
    return str(detail)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Global handler for HTTPException.

    Ensures proper serialization of HTTPException.detail even if it contains
    non-serializable objects (e.g., asyncpg.Pool).

    Args:
        request: FastAPI request
        exc: HTTPException instance

    Returns:
        JSONResponse with sanitized error detail
    """
    correlation_id = (
        getattr(request.state, "correlation_id", None)
        or getattr(request.state, "request_id", None)
        or "unknown"
    )

    # Sanitize detail to prevent JSON serialization errors
    sanitized_detail = sanitize_detail(exc.detail)
    log_path = sanitize_log_path(request.url.path)

    logger.warning(
        f"[{correlation_id}] HTTPException: {exc.status_code} - {request.method} {log_path}. "
        f"Detail: {sanitized_detail if isinstance(sanitized_detail, str) else 'See detail object'}",
    )

    # Build response headers
    headers = dict(exc.headers) if exc.headers else {}
    headers["X-Correlation-ID"] = correlation_id

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": sanitized_detail, "correlation_id": correlation_id},
        headers=headers,
    )


async def starlette_http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """
    Global handler for Starlette HTTPException (used by FastAPI internally).

    Args:
        request: FastAPI request
        exc: StarletteHTTPException instance

    Returns:
        JSONResponse with sanitized error detail
    """
    correlation_id = (
        getattr(request.state, "correlation_id", None)
        or getattr(request.state, "request_id", None)
        or "unknown"
    )

    # Sanitize detail
    sanitized_detail = sanitize_detail(exc.detail)
    log_path = sanitize_log_path(request.url.path)

    logger.warning(
        f"[{correlation_id}] StarletteHTTPException: {exc.status_code} - {request.method} {log_path}",
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": sanitized_detail, "correlation_id": correlation_id},
        headers={"X-Correlation-ID": correlation_id},
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Handler for RequestValidationError (Pydantic validation errors).

    Args:
        request: FastAPI request
        exc: RequestValidationError instance

    Returns:
        JSONResponse with validation error details
    """

    correlation_id = (
        getattr(request.state, "correlation_id", None)
        or getattr(request.state, "request_id", None)
        or "unknown"
    )

    # Get validation errors
    errors = exc.errors() if hasattr(exc, "errors") else []
    log_path = sanitize_log_path(request.url.path)

    logger.warning(
        f"[{correlation_id}] ValidationError: {len(errors)} validation errors - "
        f"{request.method} {log_path}",
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": errors,
            "correlation_id": correlation_id,
        },
        headers={"X-Correlation-ID": correlation_id},
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Global handler for unhandled exceptions.

    Prevents TypeError when serializing exceptions with non-serializable objects.

    Args:
        request: FastAPI request
        exc: Exception instance

    Returns:
        JSONResponse with sanitized error detail
    """
    correlation_id = (
        getattr(request.state, "correlation_id", None)
        or getattr(request.state, "request_id", None)
        or "unknown"
    )

    error_type = type(exc).__name__
    error_module = getattr(type(exc), "__module__", "unknown")
    log_path = sanitize_log_path(request.url.path)

    # asyncpg.InterfaceError = stale connection after Fly.io cold start.
    # Expire pool connections so the next request gets a fresh one, then return 503.
    if isinstance(exc, asyncpg.InterfaceError):
        logger.warning(
            f"[{correlation_id}] Stale DB connection on {request.method} {log_path}: {exc}. "
            "Expiring pool connections.",
        )
        try:
            db_pool = getattr(request.app.state, "db_pool", None)
            if db_pool is not None:
                await db_pool.expire_connections()
        except Exception as pool_err:
            logger.warning("[%s] Pool expire failed: %s", correlation_id, pool_err)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": "Database connection temporarily unavailable. Please retry.",
                "correlation_id": correlation_id,
            },
            headers={"X-Correlation-ID": correlation_id, "Retry-After": "3"},
        )

    logger.critical(
        f"[{correlation_id}] Unhandled exception: Type={error_type}, Module={error_module}, "
        f"Request={request.method} {log_path}",
    )

    # Sanitize error message
    try:
        error_msg = str(exc)
        # Remove any Pool references
        if "Pool" in error_msg or "asyncpg" in error_msg.lower():
            error_msg = "Database connection error"
        elif len(error_msg) > 200:
            error_msg = f"{error_type}: {error_msg[:200]}..."
    except Exception:
        error_msg = f"{error_type} error"

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": f"Internal server error: {error_msg}",
            "correlation_id": correlation_id,
            "error_type": error_type,
        },
        headers={"X-Correlation-ID": correlation_id},
    )


def register_exception_handlers(app: Any) -> None:
    """The three global handlers every app instance carries (one place, so a test can mount
    the very same registration instead of a copy)."""
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, starlette_http_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)
