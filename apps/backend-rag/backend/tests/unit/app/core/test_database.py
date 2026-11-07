"""
Tests for database pool configuration.
Validates hardening parameters without needing a real database.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.app.core.database import (
    _ACQUIRE_TIMEOUT,
    _COMMAND_TIMEOUT,
    _MAX_INACTIVE_CONN_LIFETIME,
    _POOL_MAX_SIZE,
    _POOL_MIN_SIZE,
    DatabasePoolPolicy,
    create_database_pool,
    get_db_pool,
)


class TestPoolConstants:
    """Verify pool configuration constants are sensible."""

    def test_pool_min_size(self):
        assert _POOL_MIN_SIZE >= 1
        assert _POOL_MIN_SIZE <= _POOL_MAX_SIZE

    def test_pool_max_size(self):
        assert _POOL_MAX_SIZE >= 2
        assert _POOL_MAX_SIZE <= 20  # Fly.io resource constraint

    def test_command_timeout(self):
        assert _COMMAND_TIMEOUT >= 10  # At least 10s for slow queries
        assert _COMMAND_TIMEOUT <= 120  # Never wait longer than 2 min

    def test_idle_connection_lifetime(self):
        assert _MAX_INACTIVE_CONN_LIFETIME >= 60  # At least 1 min
        assert _MAX_INACTIVE_CONN_LIFETIME <= 600  # At most 10 min

    def test_acquire_timeout(self):
        assert _ACQUIRE_TIMEOUT >= 5
        assert _ACQUIRE_TIMEOUT <= 30


class TestGetDbPool:
    """Test pool creation passes correct parameters."""

    @pytest.mark.parametrize("sslmode", ["require", "verify-full"])
    @pytest.mark.asyncio
    async def test_preserves_standalone_sslmode_for_asyncpg(self, sslmode):
        """Standalone pool DSNs retain their asyncpg TLS policy byte-for-byte."""
        dsn = (
            f"postgresql://test:test@localhost/testdb?sslmode={sslmode}&application_name=standalone"
        )
        with (
            patch(
                "backend.app.core.database.asyncpg.create_pool",
                new_callable=AsyncMock,
            ) as mock_pool,
            patch("backend.app.core.database.settings") as mock_settings,
        ):
            mock_settings.database_url = dsn
            mock_pool.return_value = AsyncMock()

            await get_db_pool()

        call_kwargs = mock_pool.await_args.kwargs
        assert call_kwargs["dsn"] == dsn
        assert "ssl" not in call_kwargs

    @pytest.mark.asyncio
    async def test_create_pool_params(self):
        """get_db_pool should pass hardening params to asyncpg.create_pool."""
        with (
            patch(
                "backend.app.core.database.asyncpg.create_pool", new_callable=AsyncMock
            ) as mock_pool,
            patch("backend.app.core.database.settings") as mock_settings,
        ):
            mock_settings.database_url = "postgresql://test:test@localhost/testdb"
            mock_pool.return_value = AsyncMock()

            await get_db_pool()

            mock_pool.assert_awaited_once()
            call_kwargs = mock_pool.call_args.kwargs

            assert call_kwargs["min_size"] == _POOL_MIN_SIZE
            assert call_kwargs["max_size"] == _POOL_MAX_SIZE
            assert call_kwargs["command_timeout"] == _COMMAND_TIMEOUT
            assert call_kwargs["max_inactive_connection_lifetime"] == _MAX_INACTIVE_CONN_LIFETIME
            assert call_kwargs["statement_cache_size"] == 0

    @pytest.mark.asyncio
    async def test_create_pool_has_init_callback(self):
        """get_db_pool should set init callback for JSON codec setup."""
        with (
            patch(
                "backend.app.core.database.asyncpg.create_pool", new_callable=AsyncMock
            ) as mock_pool,
            patch("backend.app.core.database.settings") as mock_settings,
        ):
            mock_settings.database_url = "postgresql://test:test@localhost/testdb"
            mock_pool.return_value = AsyncMock()

            await get_db_pool()

            call_kwargs = mock_pool.call_args.kwargs
            assert call_kwargs["init"] is not None
            assert callable(call_kwargs["init"])


class TestCreateDatabasePool:
    """The shared factory applies explicit full/light process policy."""

    @pytest.mark.asyncio
    async def test_full_policy_normalizes_dsn_and_runs_both_validations(self):
        connection = MagicMock()
        connection.execute = AsyncMock()
        connection.fetchval = AsyncMock(return_value=1)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = acquire

        policy = DatabasePoolPolicy(
            min_size=3,
            max_size=9,
            command_timeout=30,
            max_inactive_connection_lifetime=30.0,
            statement_timeout_seconds=30,
            validate_on_connect=True,
            validate_after_create=True,
        )
        with (
            patch(
                "backend.app.core.database.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ) as create_pool,
            patch(
                "backend.app.core.database.init_asyncpg_connection",
                new_callable=AsyncMock,
            ) as initialize_codec,
        ):
            result = await create_database_pool(
                dsn="postgresql://db/app?sslmode=disable&application_name=rag",
                policy=policy,
            )
            pool_options = create_pool.await_args.kwargs
            await pool_options["init"](connection)

        assert result is pool
        assert pool_options["dsn"] == "postgresql://db/app?application_name=rag"
        assert pool_options["ssl"] is False
        assert pool_options["min_size"] == 3
        assert pool_options["max_size"] == 9
        assert pool_options["command_timeout"] == 30
        assert pool_options["max_inactive_connection_lifetime"] == 30.0
        assert pool_options["statement_cache_size"] == 0
        assert "setup" not in pool_options
        initialize_codec.assert_awaited_once_with(connection)
        assert connection.execute.await_args_list == [
            call("SET statement_timeout = '30s'"),
            call("SELECT 1"),
        ]
        connection.fetchval.assert_awaited_once_with("SELECT 1")

    @pytest.mark.asyncio
    async def test_light_policy_skips_connection_and_post_create_validation(self):
        connection = MagicMock()
        connection.execute = AsyncMock()
        pool = MagicMock()
        policy = DatabasePoolPolicy(
            min_size=2,
            max_size=10,
            command_timeout=60,
            max_inactive_connection_lifetime=30.0,
            statement_timeout_seconds=30,
        )
        with (
            patch(
                "backend.app.core.database.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ) as create_pool,
            patch(
                "backend.app.core.database.init_asyncpg_connection",
                new_callable=AsyncMock,
            ) as initialize_codec,
        ):
            result = await create_database_pool(dsn="postgresql://db/app", policy=policy)
            pool_options = create_pool.await_args.kwargs
            await pool_options["init"](connection)

        assert result is pool
        assert "ssl" not in pool_options
        assert "setup" not in pool_options
        pool.acquire.assert_not_called()
        initialize_codec.assert_awaited_once_with(connection)
        connection.execute.assert_awaited_once_with("SET statement_timeout = '30s'")

    @pytest.mark.asyncio
    async def test_post_create_validation_rejects_unexpected_result(self):
        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=0)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = acquire
        policy = DatabasePoolPolicy(
            min_size=2,
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=30.0,
            validate_after_create=True,
        )

        with (
            patch(
                "backend.app.core.database.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            pytest.raises(ValueError, match="Pool validation failed"),
        ):
            await create_database_pool(dsn="postgresql://db/app", policy=policy)
        pool.terminate.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["acquire", "query", "release", "cancel", "cleanup"])
    async def test_failed_validation_terminates_pool_and_preserves_primary_error(
        self, failure, caplog
    ):
        primary = (
            asyncio.CancelledError() if failure == "cancel" else RuntimeError("validation sentinel")
        )
        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=1)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=connection)
        acquire.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = acquire
        if failure == "acquire":
            acquire.__aenter__.side_effect = primary
        elif failure == "release":
            acquire.__aexit__.side_effect = primary
        else:
            connection.fetchval.side_effect = primary
        if failure == "cleanup":
            pool.terminate.side_effect = RuntimeError("cleanup sentinel")
        policy = DatabasePoolPolicy(
            min_size=2,
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=30.0,
            validate_after_create=True,
        )

        with (
            patch(
                "backend.app.core.database.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            pytest.raises(type(primary)) as caught,
        ):
            await create_database_pool(dsn="postgresql://db/app", policy=policy)

        assert caught.value is primary
        pool.terminate.assert_called_once_with()
        if failure == "cleanup":
            assert "Rejected database pool cleanup failed (RuntimeError)" in caplog.text
            assert "cleanup sentinel" not in caplog.text
