import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import asyncpg

from backend.app.core.config import settings
from backend.core.pg_json_codec import (
    JSONB_ENCODER as JSONB_ENCODER,
)
from backend.core.pg_json_codec import (
    init_asyncpg_connection as init_asyncpg_connection,
)

logger = logging.getLogger(__name__)

# Pool configuration constants
_POOL_MIN_SIZE = 2
_POOL_MAX_SIZE = 5
_COMMAND_TIMEOUT = 60  # seconds — query execution timeout
_MAX_INACTIVE_CONN_LIFETIME = 300  # seconds — idle connections recycled after 5 min
_ACQUIRE_TIMEOUT = 10  # seconds — max wait for a free connection


@dataclass(frozen=True, slots=True)
class DatabasePoolPolicy:
    """Explicit asyncpg policy for one pool-owning process."""

    min_size: int
    max_size: int
    command_timeout: float
    max_inactive_connection_lifetime: float
    statement_timeout_seconds: int | None = None
    validate_on_connect: bool = False
    validate_after_create: bool = False


def normalize_database_dsn(dsn: str) -> tuple[str, bool | None]:
    """Normalize a service-pool DSN and return its explicit asyncpg SSL override."""

    parsed = urlparse(dsn)
    params = parse_qs(parsed.query)
    sslmode = params.pop("sslmode", [None])[0]
    ssl_context: bool | None = False if sslmode == "disable" else None
    clean_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=clean_query)), ssl_context


def _connection_initializer(policy: DatabasePoolPolicy):
    async def initialize(conn: asyncpg.Connection) -> None:
        await init_asyncpg_connection(conn)
        if policy.statement_timeout_seconds is not None:
            # Preserve the pre-consolidation application-pool contract: the
            # timeout is assigned once when asyncpg creates a connection.
            # asyncpg resets it after release; hardening every acquire is a
            # separate policy decision, not part of this factory refactor.
            await conn.execute(
                f"SET statement_timeout = '{policy.statement_timeout_seconds}s'",
            )
        if policy.validate_on_connect:
            await conn.execute("SELECT 1")

    return initialize


async def create_database_pool(
    *,
    dsn: str,
    policy: DatabasePoolPolicy,
    normalize_dsn: bool = True,
) -> asyncpg.Pool:
    """Create a pool from the shared codec and PgBouncer contract."""

    clean_dsn, ssl_context = normalize_database_dsn(dsn) if normalize_dsn else (dsn, None)
    pool_kwargs = {
        "dsn": clean_dsn,
        "min_size": policy.min_size,
        "max_size": policy.max_size,
        "command_timeout": policy.command_timeout,
        "max_inactive_connection_lifetime": policy.max_inactive_connection_lifetime,
        "init": _connection_initializer(policy),
        "statement_cache_size": 0,
    }
    if ssl_context is not None:
        pool_kwargs["ssl"] = ssl_context

    pool = await asyncpg.create_pool(**pool_kwargs)
    try:
        if policy.validate_after_create:
            async with pool.acquire() as conn:
                if await conn.fetchval("SELECT 1") != 1:
                    raise ValueError("Pool validation failed")
    except BaseException:
        # The rejected pool has never been published to a consumer. Termination
        # is synchronous, so cleanup has no cancellation point.
        try:
            pool.terminate()
        except BaseException as cleanup_error:
            logger.error(
                "Rejected database pool cleanup failed (%s)",
                type(cleanup_error).__name__,
            )
        raise
    return pool


async def get_db_pool() -> asyncpg.Pool:
    """
    Get a standalone database pool for scripts/testing.

    Pool hardening:
    - command_timeout: kills queries running longer than 60s
    - max_inactive_connection_lifetime: recycles idle connections after 5 min
      (prevents stale connections after Fly.io network blips)
    - statement_cache_size=0: required for PgBouncer transaction mode
    """

    pool = await create_database_pool(
        dsn=settings.database_url,
        # Standalone consumers historically passed the DSN straight to asyncpg;
        # preserve its sslmode query semantics here.
        normalize_dsn=False,
        policy=DatabasePoolPolicy(
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            command_timeout=_COMMAND_TIMEOUT,
            max_inactive_connection_lifetime=_MAX_INACTIVE_CONN_LIFETIME,
        ),
    )
    logger.info(
        "DB pool created: min=%s, max=%s, cmd_timeout=%ss, idle_recycle=%ss",
        _POOL_MIN_SIZE,
        _POOL_MAX_SIZE,
        _COMMAND_TIMEOUT,
        _MAX_INACTIVE_CONN_LIFETIME,
    )
    return pool
