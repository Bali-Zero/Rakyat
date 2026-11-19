"""Contract tests for the Vertical 1 (Visa) API-parity fixes.

Pins, with no database, no network, and zero cleartext PII/emails (Builder Contract 4):

* VISA-3A-01 — ``POST /api/visa/clock/email`` exists, is public (exact match),
  enforces ``EmailStr`` + hash/payload bounds, recomputes inclusive Day-1
  timeline expiry, and never logs the subscriber token.
* VISA-3C-01 — a cached/bookmarked client that still submits the retired
  ``"B211A"`` code is normalised to ``C1`` instead of getting a 422; a genuinely
  unknown code is still rejected.
* VISA-3C-03 — GARUDA staff ``assignment`` / ``transitions`` reserve the
  Idempotency-Key INSIDE the business transaction, so a rolled-back domain
  error leaves no reservation behind.
* KBLI-3A-04 cross-vertical — ``/api/prime/search-kbli`` is a public exact path.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response
from pydantic import EmailStr, ValidationError

from backend.app.auth.public_endpoints import find_entry, is_public_path
from backend.app.routers import garuda_staff_router
from backend.app.routers import visa_check as router_mod
from backend.services.visa_check.catalogue import VisaType

_SUBSCRIBER_PLACEHOLDER = "client_placeholder_sub_01"
_STAFF_PLACEHOLDER = "staff_placeholder_op_01"
_HASH = "abc12345XYZ"
_IDEM_HEADER = "idem-token-0000000001"


# --------------------------------------------------------------------------
# VISA-3C-01 — retired B211A code
# --------------------------------------------------------------------------


class TestClockRequestLegacyVisaType:
    def test_retired_b211a_is_normalised_to_c1(self) -> None:
        req = router_mod.ClockRequest(visa_type="B211A", entry_date=date.today())
        assert req.visa_type is VisaType.C1

    def test_retired_code_is_case_and_whitespace_tolerant(self) -> None:
        req = router_mod.ClockRequest(visa_type=" b211a ", entry_date=date.today())
        assert req.visa_type is VisaType.C1

    def test_current_codes_are_untouched(self) -> None:
        for code in ("B1", "C1", "C2", "E33G", "E28A"):
            req = router_mod.ClockRequest(visa_type=code, entry_date=date.today())
            assert req.visa_type.value == code

    def test_unknown_code_is_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            router_mod.ClockRequest(visa_type="ZZ99", entry_date=date.today())

    def test_entry_date_window_is_unchanged(self) -> None:
        with pytest.raises(ValidationError):
            router_mod.ClockRequest(visa_type="C1", entry_date=date(2001, 1, 1))


# --------------------------------------------------------------------------
# VISA-3A-01 — POST /api/visa/clock/email
# --------------------------------------------------------------------------


class TestClockEmailRouteExistsAndIsPublic:
    def test_route_is_mounted_with_post(self) -> None:
        routes = {
            (route.path, method)
            for route in router_mod.router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/api/visa/clock/email", "POST") in routes

    def test_path_is_public_exact_match(self) -> None:
        assert is_public_path("/api/visa/clock/email")
        entry = find_entry("/api/visa/clock/email")
        assert entry is not None and entry.match == "exact"
        assert not is_public_path("/api/visa/clock/email/extra")

    def test_prime_search_kbli_is_public_exact(self) -> None:
        assert is_public_path("/api/prime/search-kbli")
        entry = find_entry("/api/prime/search-kbli")
        assert entry is not None and entry.match == "exact"
        assert not is_public_path("/api/prime/search-kbli/extra")


class TestClockEmailRequestValidation:
    def test_schema_declares_email_str_and_app_defaults(self) -> None:
        fields = router_mod.ClockEmailOptInRequest.model_fields
        assert fields["email"].annotation is EmailStr
        assert fields["app"].default == "visa_clock"

    def test_rejects_a_malformed_email(self) -> None:
        with pytest.raises(ValidationError):
            router_mod.ClockEmailOptInRequest(email="not-an-email", result_hash=_HASH)

    def test_payload_cap_validator_enforces_4kb_bound(self) -> None:
        small = {"visa_type": "C1", "entry_date": "2026-11-01", "expiry_date": "2026-12-30"}
        assert router_mod.ClockEmailOptInRequest._cap_payload_size(small) == small
        with pytest.raises(ValueError, match="payload too large"):
            router_mod.ClockEmailOptInRequest._cap_payload_size({"blob": "x" * 5000})

    def test_rejects_short_or_unsafe_hash_even_before_email(self) -> None:
        with pytest.raises(ValidationError) as short_err:
            router_mod.ClockEmailOptInRequest(email="invalid", result_hash="short")
        locs = {e["loc"] for e in short_err.value.errors()}
        assert ("result_hash",) in locs
        assert ("email",) in locs

    def test_rejects_unknown_app_literal(self) -> None:
        with pytest.raises(ValidationError) as app_err:
            router_mod.ClockEmailOptInRequest(email="invalid", result_hash=_HASH, app="visa_match")
        locs = {e["loc"] for e in app_err.value.errors()}
        assert ("app",) in locs


def _saved_clock() -> Any:
    return SimpleNamespace(
        hash=_HASH,
        visa_type=VisaType.C1,
        entry_date=date(2026, 11, 1),
        expiry_date=date(2026, 12, 31),
    )


def _opt_in_model() -> router_mod.ClockEmailOptInRequest:
    return router_mod.ClockEmailOptInRequest.model_construct(
        email=_SUBSCRIBER_PLACEHOLDER,
        app="visa_clock",
        result_hash=_HASH,
        payload={"visa_type": "C1"},
    )


@pytest.mark.asyncio
class TestClockEmailHandler:
    async def test_schedules_reminders_for_an_existing_result(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        async def _load_clock(self, hash_: str):
            return _saved_clock()

        async def _subscribe(**kwargs: Any):
            captured.update(kwargs)
            return [object(), object(), object()]

        monkeypatch.setattr(router_mod.VisaCheckRepository, "load_clock", _load_clock)
        monkeypatch.setattr(router_mod, "subscribe_visa_clock", _subscribe)

        resp = await router_mod.subscribe_clock_email(_opt_in_model(), db_pool=None)

        assert resp.ok is True
        assert resp.scheduled_count == 3
        assert captured["visa_type"] == "C1"
        assert captured["result_hash"] == _HASH
        # Inclusive Day-1 clock: 60-day C1 entered 2026-11-01 expires 2026-12-30
        assert captured["expiry_date"] == date(2026, 12, 30)
        assert captured["whatsapp_url"].startswith("https://wa.me/")

    async def test_unknown_result_is_404(self, monkeypatch) -> None:
        async def _load_clock(self, hash_: str):
            return None

        monkeypatch.setattr(router_mod.VisaCheckRepository, "load_clock", _load_clock)
        with pytest.raises(HTTPException) as exc:
            await router_mod.subscribe_clock_email(_opt_in_model(), db_pool=None)
        assert exc.value.status_code == 404

    async def test_db_failure_is_500_and_never_logs_the_subscriber(
        self, monkeypatch, caplog
    ) -> None:
        import asyncpg

        async def _load_clock(self, hash_: str):
            return _saved_clock()

        async def _subscribe(**kwargs: Any):
            raise asyncpg.InterfaceError("pool closed")

        monkeypatch.setattr(router_mod.VisaCheckRepository, "load_clock", _load_clock)
        monkeypatch.setattr(router_mod, "subscribe_visa_clock", _subscribe)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(HTTPException) as exc:
                await router_mod.subscribe_clock_email(_opt_in_model(), db_pool=None)
        assert exc.value.status_code == 500
        assert _SUBSCRIBER_PLACEHOLDER not in caplog.text


# --------------------------------------------------------------------------
# VISA-3C-03 — idempotency reservation shares the business transaction
# --------------------------------------------------------------------------


class _Tx:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def __aenter__(self) -> None:
        self._log.append("tx_enter")

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._log.append(f"tx_exit:{exc_type.__name__ if exc_type else None}")
        return False


class _Conn:
    def __init__(self, log: list[str], row: Any) -> None:
        self._log = log
        self._row = row

    def transaction(self) -> _Tx:
        return _Tx(self._log)

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        self._log.append("update")
        return self._row


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _practice_row() -> dict[str, Any]:
    return {
        "practice_id": "prc_0000000000000001",
        "order_id": "ord_0000000000000001",
        "state": "Received",
        "assigned_to": _STAFF_PLACEHOLDER,
        "updated_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
        "customer_reason_key": None,
        "required_action_key": None,
        "artifact_available": False,
    }


def _install_staff_fakes(monkeypatch, log: list[str]) -> None:
    async def _actor(request: Any) -> dict[str, Any]:
        return {"email": _STAFF_PLACEHOLDER, "is_admin": True}

    async def _valid_target(conn: Any, email: str) -> bool:
        return True

    async def _reserve(conn: Any, *, key_sha256: bytes, payload_sha256: bytes):
        log.append("reserve")
        return SimpleNamespace(replayed=False, response_body=None)

    async def _complete(conn: Any, **kwargs: Any) -> None:
        log.append("complete")

    monkeypatch.setattr(garuda_staff_router, "_require_actor", _actor)
    monkeypatch.setattr(garuda_staff_router, "is_valid_garuda_assignment_target", _valid_target)
    monkeypatch.setattr(garuda_staff_router.idempotency, "reserve", _reserve)
    monkeypatch.setattr(garuda_staff_router.idempotency, "complete", _complete)


@pytest.mark.asyncio
class TestStaffIdempotencySharesTheBusinessTransaction:
    async def test_assign_reserves_inside_the_transaction(self, monkeypatch) -> None:
        log: list[str] = []
        _install_staff_fakes(monkeypatch, log)
        pool = _Pool(_Conn(log, _practice_row()))

        body = await garuda_staff_router.assign_practice(
            practice_id="prc_0000000000000001",
            request=SimpleNamespace(),
            response=Response(),
            body={"assigned_to": _STAFF_PLACEHOLDER},
            idempotency_key=_IDEM_HEADER,
            pool=pool,
        )

        assert body["assigned_to"] == _STAFF_PLACEHOLDER
        assert log == ["tx_enter", "reserve", "update", "complete", "tx_exit:None"]

    async def test_assign_domain_error_rolls_the_reservation_back_with_it(
        self, monkeypatch
    ) -> None:
        log: list[str] = []
        _install_staff_fakes(monkeypatch, log)
        pool = _Pool(_Conn(log, None))

        with pytest.raises(HTTPException) as exc:
            await garuda_staff_router.assign_practice(
                practice_id="prc_missing",
                request=SimpleNamespace(),
                response=Response(),
                body={"assigned_to": _STAFF_PLACEHOLDER},
                idempotency_key=_IDEM_HEADER,
                pool=pool,
            )

        assert exc.value.status_code == 404
        assert log == ["tx_enter", "reserve", "update", "tx_exit:HTTPException"]

    async def test_transition_reserves_inside_the_transaction_and_rolls_back(
        self, monkeypatch
    ) -> None:
        log: list[str] = []
        _install_staff_fakes(monkeypatch, log)

        async def _apply_transition(conn: Any, **kwargs: Any):
            log.append("apply")
            raise HTTPException(
                status_code=409, detail={"code": "INVALID_STATE_TRANSITION", "retryable": False}
            )

        monkeypatch.setattr(garuda_staff_router, "apply_transition", _apply_transition)
        pool = _Pool(_Conn(log, None))

        with pytest.raises(HTTPException) as exc:
            await garuda_staff_router.transition_practice(
                practice_id="prc_0000000000000001",
                request=SimpleNamespace(),
                response=Response(),
                body={"transition_id": "PR-02"},
                idempotency_key=_IDEM_HEADER,
                pool=pool,
            )

        assert exc.value.status_code == 409
        assert log == ["tx_enter", "reserve", "apply", "tx_exit:HTTPException"]

    async def test_transition_success_completes_inside_the_same_transaction(
        self, monkeypatch
    ) -> None:
        log: list[str] = []
        _install_staff_fakes(monkeypatch, log)

        async def _apply_transition(conn: Any, **kwargs: Any):
            log.append("apply")
            return _practice_row()

        monkeypatch.setattr(garuda_staff_router, "apply_transition", _apply_transition)
        pool = _Pool(_Conn(log, None))

        body = await garuda_staff_router.transition_practice(
            practice_id="prc_0000000000000001",
            request=SimpleNamespace(),
            response=Response(),
            body={"transition_id": "PR-02"},
            idempotency_key=_IDEM_HEADER,
            pool=pool,
        )

        assert body["practice_id"] == "prc_0000000000000001"
        assert log == ["tx_enter", "reserve", "apply", "complete", "tx_exit:None"]

    async def test_replay_returns_the_cached_body_without_touching_the_business_row(
        self, monkeypatch
    ) -> None:
        log: list[str] = []
        _install_staff_fakes(monkeypatch, log)
        cached = {"practice_id": "prc_0000000000000001", "state": "In review"}

        async def _reserve(conn: Any, *, key_sha256: bytes, payload_sha256: bytes):
            log.append("reserve")
            return SimpleNamespace(replayed=True, response_body=cached)

        async def _apply_transition(conn: Any, **kwargs: Any):  # pragma: no cover
            raise AssertionError("a replayed command must not run the business write")

        monkeypatch.setattr(garuda_staff_router.idempotency, "reserve", _reserve)
        monkeypatch.setattr(garuda_staff_router, "apply_transition", _apply_transition)
        pool = _Pool(_Conn(log, None))
        response = Response()

        body = await garuda_staff_router.transition_practice(
            practice_id="prc_0000000000000001",
            request=SimpleNamespace(),
            response=response,
            body={"transition_id": "PR-02"},
            idempotency_key=_IDEM_HEADER,
            pool=pool,
        )

        assert body == cached
        assert response.headers["Idempotency-Replayed"] == "true"
        assert log == ["tx_enter", "reserve", "tx_exit:None"]
