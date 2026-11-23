"""Tests for portal profile update."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.portal.portal_service import PortalService

_PROFILE_ROW = {
    "id": 1,
    "full_name": "John",
    "email": "john@test.com",
    "phone": "+6281234567890",
    "whatsapp": "+6281234567890",
    "address": "Jl Raya Seminyak 123",
    "nationality": "US",
    "passport_number": "AB123",
    "passport_expiry": None,
    "date_of_birth": None,
    "gender": "M",
    "member_since": "2025-01-01",
    "assigned_to_email": None,
    "assigned_to_name": None,
    "assigned_to_avatar": None,
}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _make_service(fetchrow_return: dict | None = None) -> tuple["PortalService", AsyncMock]:
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = None
    mock_conn.fetchrow.return_value = fetchrow_return or _PROFILE_ROW
    # conn.transaction() must be an async CM, not a coroutine (phone-lock TX).
    mock_conn.transaction = MagicMock(return_value=_Tx())

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    return PortalService(mock_pool), mock_conn


@pytest.mark.asyncio
async def test_update_profile_updates_allowed_fields():
    """update_profile runs UPDATE + notification_alerts INSERT + portal_messages INSERT."""
    service, mock_conn = _make_service()

    result = await service.update_profile(
        client_id=1,
        fields={
            "phone": "+6281234567890",
            "whatsapp": "+6281234567890",
            "address": "Jl Raya Seminyak 123",
        },
        current_user={"client_id": 1, "email": "c1@example.com"},
    )

    assert result is not None

    # The submitted phone/whatsapp equal the stored ones (no-op, no request
    # row), so three execute calls: UPDATE (address only), notification_alerts,
    # portal_messages. A client never writes phone/whatsapp.
    assert mock_conn.execute.call_count == 3

    update_sql = mock_conn.execute.call_args_list[0][0][0]
    assert "phone" not in update_sql
    assert "whatsapp" not in update_sql
    assert "address" in update_sql

    alert_sql = mock_conn.execute.call_args_list[1][0][0]
    assert "notification_alerts" in alert_sql
    assert "portal_profile_update" in alert_sql
    assert "ON CONFLICT (client_id, alert_type, created_date)" in alert_sql
    assert "ON CONSTRAINT" not in alert_sql

    msg_sql = mock_conn.execute.call_args_list[2][0][0]
    assert "portal_messages" in msg_sql
    assert "is_system_generated" in msg_sql and ", TRUE)" in msg_sql
    assert "client_to_team" in msg_sql


@pytest.mark.asyncio
async def test_update_profile_rejects_sensitive_fields():
    """update_profile ignores non-whitelisted fields; a new phone becomes a request, not a write."""
    service, mock_conn = _make_service(
        fetchrow_return={**_PROFILE_ROW, "phone": "+62999", "whatsapp": None, "address": None},
    )

    result = await service.update_profile(
        client_id=1,
        fields={"full_name": "HACKED", "passport_number": "STOLEN", "phone": "+62 800 0000 999"},
        current_user={"client_id": 1, "email": "c1@example.com"},
    )

    assert result["contact_change_requested"] == ["phone"]

    # 2 calls: the change-request row, then the notification_alerts alert.
    assert mock_conn.execute.call_count == 2
    sqls = [c[0][0] for c in mock_conn.execute.call_args_list]
    assert not any("UPDATE clients" in q for q in sqls)
    assert "client_to_team" in sqls[0] and "portal_messages" in sqls[0]
    assert "HACKED" not in str(mock_conn.execute.call_args_list)
    assert "STOLEN" not in str(mock_conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_update_profile_empty_fields_noop():
    """update_profile with no valid fields does NOT call execute at all."""
    service, mock_conn = _make_service()

    result = await service.update_profile(
        client_id=1,
        fields={"full_name": "HACKED", "nationality": "RU"},
        current_user={"client_id": 1, "email": "c1@example.com"},
    )

    assert result is not None
    mock_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_update_profile_notification_failure_does_not_raise():
    """If notification_alerts INSERT fails, update_profile still returns profile (graceful degradation)."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = _PROFILE_ROW
    mock_conn.transaction = MagicMock(return_value=_Tx())

    call_count = 0

    async def execute_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Call order: #1 UPDATE (address), #2 notification_alerts INSERT.
        if call_count == 2:  # notification_alerts INSERT
            raise Exception("DB constraint error")
        return None

    mock_conn.execute.side_effect = execute_side_effect

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    service = PortalService(mock_pool)
    result = await service.update_profile(
        client_id=1,
        fields={"address": "New street 1"},
        current_user={"client_id": 1, "email": "c1@example.com"},
    )

    assert result is not None  # Profile returned despite notification failure


@pytest.mark.asyncio
async def test_update_profile_message_failure_does_not_raise() -> None:
    """If portal_messages insert fails, update_profile still returns the profile."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = _PROFILE_ROW
    mock_conn.transaction = MagicMock(return_value=_Tx())

    call_count = 0

    async def execute_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        # #1 UPDATE (address), #2 notification_alerts, #3 portal_messages.
        if call_count == 3:  # portal_messages INSERT
            raise Exception("message insert failed")

    mock_conn.execute.side_effect = execute_side_effect

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    service = PortalService(mock_pool)
    result = await service.update_profile(
        client_id=1,
        fields={"address": "New street 1"},
        current_user={"client_id": 1, "email": "c1@example.com"},
    )

    assert result is not None


@pytest.mark.asyncio
async def test_get_profile_data_returns_empty_when_client_missing() -> None:
    """_get_profile_data returns an empty dict for a missing/deleted client."""
    service, mock_conn = _make_service(fetchrow_return=None)
    mock_conn.fetchrow.return_value = None

    result = await service._get_profile_data(mock_conn, client_id=404)

    assert result == {}


_USER = {"client_id": 1, "email": "c1@example.com"}


def _sqls(mock_conn) -> list[str]:
    return [str(c[0][0]) for c in mock_conn.execute.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_update_profile_contact_change_is_a_request_never_a_write(field):
    """A new usable number is recorded as ONE client_to_team request row, never
    written to clients, never locked, and DUP_OWNER_SQL is never consulted."""
    from backend.db.repositories.client_repository import DUP_OWNER_SQL

    service, mock_conn = _make_service()

    result = await service.update_profile(
        client_id=1, fields={field: "+6281200000001"}, current_user=_USER
    )

    assert result["contact_change_requested"] == [field]
    sqls = _sqls(mock_conn)
    assert not any("UPDATE clients" in q for q in sqls)
    assert not any("pg_advisory_xact_lock" in q for q in sqls)
    requests = [c for c in mock_conn.execute.call_args_list if "client_to_team" in c[0][0]]
    assert len(requests) == 1
    text = requests[0][0][2]
    assert text.startswith("Requested contact change: ")
    assert "+6281200000001" in text
    seen = [
        str(c[0][0]) for c in mock_conn.fetchrow.call_args_list + mock_conn.fetch.call_args_list
    ]
    assert DUP_OWNER_SQL not in seen + sqls


@pytest.mark.asyncio
async def test_update_profile_unusable_new_number_is_refused_before_any_write():
    from backend.services.portal._mixins.billing import ProfileContactRefused

    service, mock_conn = _make_service()

    for bad in ("", "   ", "12-3"):
        with pytest.raises(ProfileContactRefused):
            await service.update_profile(
                client_id=1, fields={"phone": bad, "address": "Never saved"}, current_user=_USER
            )
    mock_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_update_profile_same_core_value_is_a_no_op():
    service, mock_conn = _make_service()

    result = await service.update_profile(
        client_id=1,
        fields={"phone": "081234567890", "whatsapp": "+62 812-3456-7890"},
        current_user=_USER,
    )

    assert result["contact_change_requested"] == []
    mock_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_update_profile_address_save_survives_unusable_stored_contacts():
    """A stored +62999 resubmitted unchanged is a no-op, so the address still saves."""
    service, mock_conn = _make_service(
        fetchrow_return={**_PROFILE_ROW, "phone": "+62999", "whatsapp": "+62999"}
    )

    result = await service.update_profile(
        client_id=1,
        fields={"phone": "+62999", "whatsapp": "+62999", "address": "New street 1"},
        current_user=_USER,
    )

    assert result["contact_change_requested"] == []
    sqls = _sqls(mock_conn)
    assert any("UPDATE clients" in q and "address" in q for q in sqls)
    assert not any("client_to_team" in q and "Contact change" in q for q in sqls)


@pytest.mark.asyncio
async def test_update_profile_two_requested_fields_make_one_request_row_and_the_alert():
    service, mock_conn = _make_service()

    result = await service.update_profile(
        client_id=1,
        fields={"phone": "+6281200000001", "whatsapp": "+6281200000002", "address": "A 1"},
        current_user=_USER,
    )

    assert result["contact_change_requested"] == ["phone", "whatsapp"]
    requests = [c for c in mock_conn.execute.call_args_list if "Contact change" in c[0][0]]
    assert len(requests) == 1
    assert "phone \u2192 +6281200000001; WhatsApp \u2192 +6281200000002" in requests[0][0][2]
    alert = [c for c in mock_conn.execute.call_args_list if "notification_alerts" in c[0][0]]
    assert len(alert) == 1 and "phone change requested" in alert[0][0][2]


# ============================================
# CRM AUDIT TRAIL (crm_audit_log)
#
# The portal write path used to update `clients` and only ever notify two
# soft channels (notification_alerts, portal_messages) — never the
# structured `crm_audit_log` table the CRM's own PATCH path writes via
# @audit_change. These tests pin the fix: update_profile now calls
# CRMAuditLogger.log_state_change (reused, not reimplemented) with an
# actor tagged unambiguously as the client, is a no-op when nothing was
# actually written, and can never fail the client's own update.
# ============================================

_AUDIT_LOG_TARGET = "backend.services.portal._mixins.billing.audit_logger.log_state_change"


@pytest.mark.asyncio
async def test_update_profile_writes_crm_audit_log_entry():
    """GUILT: a real field change writes a crm_audit_log entry (via the
    existing CRMAuditLogger.log_state_change API) naming the changed
    field, with old and new values, and an actor tagged as the client —
    not a bare staff-indistinguishable email."""
    service, _mock_conn = _make_service(
        fetchrow_return={**_PROFILE_ROW, "address": "OLD ADDRESS"},
    )

    with patch(_AUDIT_LOG_TARGET, new=AsyncMock(return_value=True)) as mock_audit:
        result = await service.update_profile(
            client_id=1,
            fields={"address": "NEW ADDRESS"},
            current_user={"client_id": 1, "email": "c1@example.com"},
        )

    assert result is not None
    mock_audit.assert_awaited_once()
    kwargs = mock_audit.call_args.kwargs
    assert kwargs["entity_type"] == "client"
    assert kwargs["entity_id"] == 1
    assert kwargs["old_state"] == {"address": "OLD ADDRESS"}
    assert kwargs["new_state"] == {"address": "NEW ADDRESS"}
    # Actor must be unambiguously the client, not a bare email — an
    # operator can't tell "client" from "staff" out of an @domain alone.
    assert kwargs["user_email"] == "portal-client:c1@example.com"
    assert kwargs["change_type"] == "update"


@pytest.mark.asyncio
async def test_update_profile_non_editable_fields_write_no_audit_row():
    """INNOCENCE: an update whose fields are all non-editable (safe_fields
    empty, no UPDATE runs) writes NO crm_audit_log row — an audit trail
    that records non-events is noise."""
    service, _mock_conn = _make_service()

    with patch(_AUDIT_LOG_TARGET, new=AsyncMock(return_value=True)) as mock_audit:
        result = await service.update_profile(
            client_id=1,
            fields={"full_name": "HACKED", "nationality": "RU"},
            current_user={"client_id": 1, "email": "c1@example.com"},
        )

    assert result is not None
    mock_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_profile_audit_log_failure_does_not_raise():
    """RESILIENCE: if the crm_audit_log write raises, the profile update
    still succeeds (the client is never blocked by an audit-plumbing bug)
    AND the failure is logged loudly — never swallowed silently."""
    service, _mock_conn = _make_service()

    with (
        patch(
            _AUDIT_LOG_TARGET, new=AsyncMock(side_effect=Exception("audit db exploded"))
        ) as mock_audit,
        patch("backend.services.portal._mixins.billing.logger") as mock_logger,
    ):
        result = await service.update_profile(
            client_id=1,
            fields={"address": "NEW ADDRESS"},
            current_user={"client_id": 1, "email": "c1@example.com"},
        )

    assert result is not None  # the client's own update was not affected
    mock_audit.assert_awaited_once()
    assert mock_logger.error.called, "audit failure must be logged, not swallowed silently"
    logged = " ".join(str(c) for c in mock_logger.error.call_args_list)
    assert "crm_audit_log" in logged
