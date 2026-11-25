"""Health and capability-boundary coverage for the retention worker."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from backend.scripts.visa_engine import retention_worker
from backend.services.visa_engine import retention


def _approved_active_policy() -> retention_worker.ActiveRetentionPolicy:
    return retention_worker.ActiveRetentionPolicy(
        policy_version="visa-oracle-privacy-v1",
        retention_interval=timedelta(days=30),
        idempotency_retention_interval=timedelta(hours=24),
        legal_hold_review_interval=timedelta(days=30),
        retention_anchor="EVALUATED_AT",
        approved_by="zero",
    )


def _healthy_cycle(*, healthy: bool) -> retention_worker.RetentionCycleResult:
    return retention_worker.RetentionCycleResult(
        decision_deleted=0,
        idempotency_deleted=0,
        decision_expired_remaining=0,
        decision_expired_held=0,
        decision_max_lag_seconds=0,
        idempotency_expired_remaining=0,
        idempotency_max_lag_seconds=0,
        healthy=healthy,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "idempotency", "expected_healthy"),
    [
        (None, None, True),
        ("expired_rows", None, False),
        ("max_lag_seconds", None, False),
        (None, "expired_rows", False),
        (None, "max_lag_seconds", False),
    ],
    ids=(
        "all_healthy",
        "decision_expired_rows",
        "decision_max_lag_seconds",
        "idempotency_expired_rows",
        "idempotency_max_lag_seconds",
    ),
)
async def test_retention_cycle_health_requires_each_independent_condition(
    monkeypatch: pytest.MonkeyPatch,
    decision: str | None,
    idempotency: str | None,
    expected_healthy: bool,
) -> None:
    """Guilt cases isolate each conjunct; innocence is the all-healthy case."""
    now = datetime.now(timezone.utc)
    decision_evidence = retention.DecisionRetentionEvidence(
        expired_rows=0,
        expired_held_rows=0,
        max_lag_seconds=0,
        observed_at=now,
    )
    idempotency_evidence = retention.IdempotencyRetentionEvidence(
        expired_rows=0,
        max_lag_seconds=0,
        observed_at=now,
    )
    if decision == "expired_rows":
        decision_evidence = replace(decision_evidence, expired_rows=1)
    if decision == "max_lag_seconds":
        decision_evidence = replace(decision_evidence, max_lag_seconds=3_601)
    if idempotency == "expired_rows":
        idempotency_evidence = replace(idempotency_evidence, expired_rows=1)
    if idempotency == "max_lag_seconds":
        idempotency_evidence = replace(idempotency_evidence, max_lag_seconds=3_601)

    monkeypatch.setattr(
        retention_worker, "_active_policy", AsyncMock(return_value=_approved_active_policy())
    )
    monkeypatch.setattr(
        retention, "decision_retention_evidence", AsyncMock(return_value=decision_evidence)
    )
    monkeypatch.setattr(
        retention, "idempotency_retention_evidence", AsyncMock(return_value=idempotency_evidence)
    )

    result = await retention_worker.run_retention_cycle(
        object(),  # type: ignore[arg-type]
        apply=False,
        limit=1_000,
        max_batches=1,
        requested_by="synthetic-health-test",
        max_lag_seconds=3_600,
    )

    assert result.healthy is expected_healthy


class _RunPool:
    def __init__(self) -> None:
        self.close = AsyncMock()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "cycle", "scope_results", "expected_status", "raises"),
    [
        ("VISA_DECISION", _healthy_cycle(healthy=True), None, 0, None),
        ("VISA_DECISION", _healthy_cycle(healthy=False), None, 2, None),
        (
            "GARUDA_CHECK",
            None,
            [
                retention_worker.ScopePurgeResult(
                    scope="GARUDA_CHECK",
                    label="synthetic_check",
                    deleted=1_000,
                    exhausted_batch_cap=True,
                )
            ],
            2,
            None,
        ),
        ("VISA_DECISION", RuntimeError("synthetic cycle failure"), None, None, RuntimeError),
    ],
    ids=("healthy", "unhealthy", "garuda_batch_cap", "cycle_raises"),
)
async def test_run_returns_health_status_and_always_closes_pool(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    cycle: retention_worker.RetentionCycleResult | RuntimeError | None,
    scope_results: list[retention_worker.ScopePurgeResult] | None,
    expected_status: int | None,
    raises: type[Exception] | None,
) -> None:
    """Guilt covers unhealthy exits and exceptions; healthy execution is innocence."""
    pool = _RunPool()
    monkeypatch.setenv("SYNTHETIC_RETENTION_DSN", "postgresql://synthetic.invalid/db")
    monkeypatch.setattr(retention_worker.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(retention_worker, "_assert_operator_boundary", AsyncMock())
    if isinstance(cycle, Exception):
        monkeypatch.setattr(retention_worker, "run_retention_cycle", AsyncMock(side_effect=cycle))
    elif cycle is not None:
        monkeypatch.setattr(retention_worker, "run_retention_cycle", AsyncMock(return_value=cycle))
    if scope_results is not None:
        monkeypatch.setattr(
            retention_worker, "run_scope_purge_cycle", AsyncMock(return_value=scope_results)
        )
    args = retention_worker._parse_args(
        ["--scope", scope, "--database-url-env", "SYNTHETIC_RETENTION_DSN"]
    )

    if raises is not None:
        with pytest.raises(raises, match="synthetic cycle failure"):
            await retention_worker.run(args)
    else:
        assert await retention_worker.run(args) == expected_status
    pool.close.assert_awaited_once()


class _BoundaryConnection:
    def __init__(
        self,
        *,
        current_user: str = "visa_retention_login",
        is_superuser: bool = False,
        denied_signature: str | None = None,
    ) -> None:
        self.current_user = current_user
        self.is_superuser = is_superuser
        self.denied_signature = denied_signature

    async def fetchrow(self, query: str) -> dict[str, object]:
        assert "current_user::text" in query
        return {"current_user": self.current_user, "is_superuser": self.is_superuser}

    async def fetchval(self, query: str, *args: object) -> bool:
        if "has_table_privilege" in query:
            return True
        if "has_function_privilege" in query:
            return args[0] != self.denied_signature
        raise AssertionError(f"unexpected boundary query: {query}")


class _BoundaryAcquire:
    def __init__(self, connection: _BoundaryConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _BoundaryConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class _BoundaryPool:
    def __init__(self, connection: _BoundaryConnection) -> None:
        self.connection = connection

    def acquire(self) -> _BoundaryAcquire:
        return _BoundaryAcquire(self.connection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection", "apply", "scopes", "error"),
    [
        (
            _BoundaryConnection(current_user=retention_worker.FORBIDDEN_RUNTIME_ROLE),
            False,
            ("VISA_DECISION",),
            "serving runtime role",
        ),
        (
            _BoundaryConnection(is_superuser=True),
            False,
            ("VISA_DECISION",),
            "superuser retention",
        ),
        (
            _BoundaryConnection(
                denied_signature="public.purge_visa_decisions(integer,text)"
            ),
            True,
            ("VISA_DECISION",),
            "purge_visa_decisions",
        ),
        (
            _BoundaryConnection(
                denied_signature="public.purge_garuda_voa_checks(integer,text)"
            ),
            True,
            ("GARUDA_CHECK",),
            "purge_garuda_voa_checks",
        ),
        (_BoundaryConnection(), True, ("VISA_DECISION", "GARUDA_CHECK"), None),
    ],
    ids=("forbidden_runtime_role", "superuser", "visa_purge_missing", "garuda_purge_missing", "allowed"),
)
async def test_operator_boundary_rejects_forbidden_identity_and_missing_apply_capabilities(
    connection: _BoundaryConnection,
    apply: bool,
    scopes: tuple[str, ...],
    error: str | None,
) -> None:
    """Guilt exercises every rejected boundary; the final case is innocence."""
    pool = _BoundaryPool(connection)

    if error is None:
        await retention_worker._assert_operator_boundary(pool, apply=apply, scopes=scopes)  # type: ignore[arg-type]
    else:
        with pytest.raises(RuntimeError, match=error):
            await retention_worker._assert_operator_boundary(pool, apply=apply, scopes=scopes)  # type: ignore[arg-type]
