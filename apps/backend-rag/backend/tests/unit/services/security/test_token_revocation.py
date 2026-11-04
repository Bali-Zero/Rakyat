"""Tests for Redis-backed token revocation — S03 Sprint 2."""

from unittest.mock import AsyncMock, patch

import pytest


class TestTokenRevocationService:
    """Test token revocation via Redis."""

    @pytest.mark.asyncio
    async def test_revoke_token_sets_redis_key(self):
        from backend.services.security.token_revocation import TokenRevocationService

        mock_redis = AsyncMock()
        svc = TokenRevocationService(redis_client=mock_redis)
        await svc.revoke_token("jti-123", ttl_seconds=3600, reason="logout")
        mock_redis.setex.assert_called_once_with("revoked:jti-123", 3600, "logout")

    @pytest.mark.asyncio
    async def test_is_revoked_returns_true_for_revoked_token(self):
        from backend.services.security.token_revocation import TokenRevocationService

        mock_redis = AsyncMock()
        mock_redis.exists.return_value = 1
        svc = TokenRevocationService(redis_client=mock_redis)
        result = await svc.is_revoked("jti-123")
        assert result is True

    @pytest.mark.asyncio
    async def test_is_revoked_returns_false_for_valid_token(self):
        from backend.services.security.token_revocation import TokenRevocationService

        mock_redis = AsyncMock()
        mock_redis.exists.return_value = 0
        svc = TokenRevocationService(redis_client=mock_redis)
        result = await svc.is_revoked("jti-123")
        assert result is False

    @pytest.mark.asyncio
    async def test_revoke_all_user_tokens_sets_user_key(self):
        from backend.services.security.token_revocation import TokenRevocationService

        mock_redis = AsyncMock()
        svc = TokenRevocationService(redis_client=mock_redis)
        with patch(
            "backend.services.security.token_revocation.time.time",
            return_value=1_700_000_000.125,
        ):
            await svc.revoke_all_user_tokens(
                "User@BaliZero.com", reason="password_change"
            )
        mock_redis.setex.assert_called_once_with(
            "revoked_user:user@balizero.com", 86400, "1700000000.125"
        )

    @pytest.mark.asyncio
    async def test_is_user_revoked_rejects_token_issued_before_marker(self):
        from backend.services.security.token_revocation import TokenRevocationService

        mock_redis = AsyncMock()
        mock_redis.get.return_value = b"1700000000.125"
        svc = TokenRevocationService(redis_client=mock_redis)
        result = await svc.is_user_revoked("User@BaliZero.com", 1_699_999_999.9)
        assert result is True
        mock_redis.get.assert_awaited_once_with("revoked_user:user@balizero.com")

    @pytest.mark.asyncio
    async def test_is_user_revoked_accepts_new_login_after_marker(self):
        from backend.services.security.token_revocation import TokenRevocationService

        mock_redis = AsyncMock()
        mock_redis.get.return_value = "1700000000.125"
        svc = TokenRevocationService(redis_client=mock_redis)
        result = await svc.is_user_revoked("User@BaliZero.com", 1_700_000_000.25)
        assert result is False

    @pytest.mark.asyncio
    async def test_is_user_revoked_accepts_when_marker_is_absent(self):
        from backend.services.security.token_revocation import TokenRevocationService

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        svc = TokenRevocationService(redis_client=mock_redis)
        result = await svc.is_user_revoked("User@BaliZero.com", 1_700_000_000.25)
        assert result is False

    @pytest.mark.parametrize(
        ("token_issued_at", "revoked_at"),
        [
            (None, "1700000000.125"),
            ("not-a-timestamp", "1700000000.125"),
            (1_700_000_000.25, "corrupt-marker"),
            (float("nan"), "1700000000.125"),
        ],
    )
    @pytest.mark.asyncio
    async def test_is_user_revoked_fails_closed_on_invalid_timestamp_state(
        self,
        token_issued_at: object,
        revoked_at: object,
    ) -> None:
        from backend.services.security.token_revocation import TokenRevocationService

        mock_redis = AsyncMock()
        mock_redis.get.return_value = revoked_at
        svc = TokenRevocationService(redis_client=mock_redis)
        assert await svc.is_user_revoked("User@BaliZero.com", token_issued_at) is True

    @pytest.mark.asyncio
    async def test_fails_closed_on_redis_unavailable(self):
        from backend.services.security.token_revocation import (
            RevocationStoreUnavailable,
            TokenRevocationService,
        )

        svc = TokenRevocationService(redis_client=None)
        with pytest.raises(RevocationStoreUnavailable):
            await svc.is_revoked("jti-123")

    @pytest.mark.asyncio
    async def test_fails_closed_on_redis_error(self):
        from backend.services.security.token_revocation import (
            RevocationStoreUnavailable,
            TokenRevocationService,
        )

        mock_redis = AsyncMock()
        mock_redis.exists.side_effect = ConnectionError("Redis down")
        svc = TokenRevocationService(redis_client=mock_redis)
        with pytest.raises(RevocationStoreUnavailable):
            await svc.is_revoked("jti-123")

    @pytest.mark.asyncio
    async def test_is_user_deactivated_caches_active_user_for_60s_and_rejects_after_expiry(self):
        from backend.services.security.token_revocation import (
            TokenRevocationService,
            clear_user_active_cache,
            set_revocation_db_pool,
        )

        clear_user_active_cache()
        set_revocation_db_pool(None)

        conn = AsyncMock()
        conn.fetchval = AsyncMock(side_effect=[True, False])

        class _Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self_inner):
                        return conn

                    async def __aexit__(self_inner, *_exc):
                        return False

                return _Ctx()

        svc = TokenRevocationService(redis_client=AsyncMock(), db_pool=_Pool())
        with patch(
            "backend.services.security.token_revocation.time.monotonic",
            side_effect=[100.0, 130.0, 160.1, 160.1],
        ):
            assert await svc.is_user_deactivated("User@BaliZero.com") is False
            # Within 60s window (t=130.0): served from cache without hitting DB
            assert await svc.is_user_deactivated("user@balizero.com") is False
            assert conn.fetchval.await_count == 1
            # At 60.1s (t=160.1): cache expired -> DB re-queried -> active=False -> rejected
            assert await svc.is_user_deactivated("user@balizero.com") is True
            assert conn.fetchval.await_count == 2

        clear_user_active_cache()

    @pytest.mark.asyncio
    async def test_revoke_all_user_tokens_evicts_active_user_cache_immediately(self):
        from backend.services.security.token_revocation import (
            TokenRevocationService,
            clear_user_active_cache,
        )

        clear_user_active_cache()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(side_effect=[True, False])

        class _Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self_inner):
                        return conn

                    async def __aexit__(self_inner, *_exc):
                        return False

                return _Ctx()

        mock_redis = AsyncMock()
        svc = TokenRevocationService(redis_client=mock_redis, db_pool=_Pool())
        assert await svc.is_user_deactivated("user@balizero.com") is False
        await svc.revoke_all_user_tokens("user@balizero.com", reason="deactivated")
        assert await svc.is_user_deactivated("user@balizero.com") is True
        assert conn.fetchval.await_count == 2
        clear_user_active_cache()

    @pytest.mark.asyncio
    async def test_is_user_deactivated_fails_closed_on_db_error(self):
        from backend.services.security.token_revocation import (
            RevocationStoreUnavailable,
            TokenRevocationService,
            clear_user_active_cache,
        )

        clear_user_active_cache()

        class _BrokenPool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self_inner):
                        raise RuntimeError("DB connection lost")

                    async def __aexit__(self_inner, *_exc):
                        return False

                return _Ctx()

        svc = TokenRevocationService(redis_client=AsyncMock(), db_pool=_BrokenPool())
        with pytest.raises(RevocationStoreUnavailable):
            await svc.is_user_deactivated("user@balizero.com")

    @pytest.mark.asyncio
    async def test_is_session_revoked_rejects_deactivated_user_even_when_redis_clean(self):
        from backend.services.security.token_revocation import (
            clear_user_active_cache,
            is_session_revoked,
            set_revocation_db_pool,
        )

        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=False)

        class _Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self_inner):
                        return conn

                    async def __aexit__(self_inner, *_exc):
                        return False

                return _Ctx()

        mock_redis = AsyncMock()
        mock_redis.exists.return_value = 0
        mock_redis.get.return_value = None
        mock_manager = AsyncMock()
        mock_manager.get_async_client = lambda: mock_redis

        set_revocation_db_pool(_Pool())
        try:
            with patch(
                "backend.core.redis_manager.RedisManager.get_instance",
                return_value=mock_manager,
            ):
                revoked = await is_session_revoked(
                    {
                        "jti": "jti-live",
                        "email": "deactivated@balizero.com",
                        "iat": 1_700_000_000,
                    }
                )
            assert revoked is True
        finally:
            set_revocation_db_pool(None)
            clear_user_active_cache()

    def test_service_initializer_wires_revocation_pool_before_publishing_readiness(self):
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[4]
            / "app"
            / "setup"
            / "service_initializer.py"
        ).read_text()
        tree = ast.parse(src)
        fns = {
            n.name: n
            for n in tree.body
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        }
        for fn_name in ("initialize_database_services", "initialize_services_light"):
            fn = fns[fn_name]
            nodes = list(ast.walk(fn))
            set_pool_lines = [
                n.lineno
                for n in nodes
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "set_revocation_db_pool"
            ]
            publish_lines = [
                n.lineno
                for n in nodes
                if isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute)
                    and t.attr == "db_pool"
                    and isinstance(t.value, ast.Attribute)
                    and t.value.attr == "state"
                    for t in n.targets
                )
                and not (isinstance(n.value, ast.Constant) and n.value.value is None)
            ]
            assert set_pool_lines and publish_lines, fn_name
            assert set_pool_lines[0] < publish_lines[0], (
                f"{fn_name} must call set_revocation_db_pool before publishing app.state.db_pool"
            )
