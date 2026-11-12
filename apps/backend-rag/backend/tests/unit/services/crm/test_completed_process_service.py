"""Unit tests for CompletedProcessService (completed-stage automation, owner option (a), 2026-11-12).

Spec rows (behaviour -> test):

C1  Activity: every Completed writes ONE ``process_completed`` activity row, whatever happens to
    the mail -> test_every_branch_writes_one_activity_row, test_activity_row_survives_a_mail_failure
C2  The client "Congratulations" email is gone for good (the listener mail is the only client
    email) -> test_no_client_mail_in_any_branch, test_the_dead_client_mail_and_upload_paths_are_gone
C3  Client with email AND an assignee other than the client: the listener cc'd the assignee, so
    NO team mail -> test_client_with_email_and_assignee_gets_no_team_mail
C4  Client without email: the listener skipped, the assignee (else created_by) gets the team mail
    -> test_client_without_email_team_mail_goes_to_assignee, test_missing_client_row_counts_as_no_email
C5  No assignee: the listener mail had no cc, created_by gets the team mail
    -> test_no_assignee_team_mail_goes_to_created_by
C6  Nobody to tell (no assignee, no creator): no mail, activity still written
    -> test_nobody_to_tell_sends_nothing
C7  Never mail the client their own team mail: an assignee equal to the client's address falls
    through to created_by -> test_assignee_equal_to_client_address_falls_through_to_created_by
C8  Subject and body use the service display name (practice_types.name), not the code
    -> test_team_mail_uses_the_service_display_name_not_the_code
C9  ``_fetch_client_data`` selects only columns that exist on prod ``clients``
    -> test_fetch_client_data_selects_no_dead_column (+ the real-Postgres file
       test_completed_process_service_pg.py)
C10 Practice not found: error, no activity row, no mail -> test_unknown_practice
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.crm.completed_process_service import (
    CompletedProcessService,
    team_recipient,
)

CLIENT_EMAIL = "client.placeholder@example.test"
ASSIGNEE = "consultant.placeholder@example.test"
CREATOR = "creator.placeholder@example.test"


def _make_service() -> tuple[CompletedProcessService, MagicMock]:
    pool = MagicMock()
    return CompletedProcessService(pool), pool


def _practice(**over) -> dict:
    base = {
        "id": 501,
        "client_id": 7,
        "assigned_to": ASSIGNEE,
        "created_by": CREATOR,
        "practice_type_code": "CODE_X",
        "practice_type_name": "Service Display Name",
    }
    base.update(over)
    return base


def _client(email: str | None = CLIENT_EMAIL) -> dict:
    return {"id": 7, "full_name": "Placeholder Client", "email": email}


def _service(practice: dict | None, client: dict | None):
    svc, _pool = _make_service()
    svc._fetch_practice_data = AsyncMock(return_value=practice)
    svc._fetch_client_data = AsyncMock(return_value=client)
    svc._log_activity = AsyncMock()
    svc._send_with_brevo_fallback = AsyncMock()
    return svc


def _sent(svc) -> list[tuple[str, str, str]]:
    """(to, subject, body) of every mail the service tried to send."""
    return [
        (c.args[0], c.args[1], c.args[2]) for c in svc._send_with_brevo_fallback.await_args_list
    ]


# --- C3 innocence: the team already got the listener's cc -------------------------------


@pytest.mark.asyncio
async def test_client_with_email_and_assignee_gets_no_team_mail() -> None:
    svc = _service(_practice(), _client())
    result = await svc.trigger_on_completed(501, "staff@example.test")
    assert result == {"success": True, "team_notified": False}
    assert _sent(svc) == []
    svc._log_activity.assert_awaited_once()


# --- C4 guilt: the listener skipped, the team member must be told -----------------------


@pytest.mark.asyncio
async def test_client_without_email_team_mail_goes_to_assignee() -> None:
    svc = _service(_practice(), _client(email=None))
    result = await svc.trigger_on_completed(501, "staff@example.test")
    assert result == {"success": True, "team_notified": True}
    ((to, subject, body),) = _sent(svc)
    assert to == ASSIGNEE
    assert subject.startswith("[TEAM]")
    assert "no email on file" in body
    svc._log_activity.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_string_email_counts_as_no_email() -> None:
    svc = _service(_practice(), _client(email=""))
    await svc.trigger_on_completed(501, "staff@example.test")
    assert [to for to, _, _ in _sent(svc)] == [ASSIGNEE]


@pytest.mark.asyncio
async def test_missing_client_row_counts_as_no_email() -> None:
    svc = _service(_practice(), None)
    result = await svc.trigger_on_completed(501, "staff@example.test")
    assert result["success"] is True
    ((to, subject, _body),) = _sent(svc)
    assert to == ASSIGNEE
    assert "Client #7" in subject
    svc._log_activity.assert_awaited_once()


# --- C5 guilt: no assignee, so the listener mail copied nobody --------------------------


@pytest.mark.asyncio
async def test_no_assignee_team_mail_goes_to_created_by() -> None:
    svc = _service(_practice(assigned_to=None), _client())
    result = await svc.trigger_on_completed(501, "staff@example.test")
    assert result["team_notified"] is True
    ((to, _subject, body),) = _sent(svc)
    assert to == CREATOR
    assert "No team member is assigned" in body


@pytest.mark.asyncio
async def test_blank_assignee_is_no_assignee() -> None:
    svc = _service(_practice(assigned_to=""), _client())
    await svc.trigger_on_completed(501, "staff@example.test")
    assert [to for to, _, _ in _sent(svc)] == [CREATOR]


# --- C6 / C7 ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nobody_to_tell_sends_nothing() -> None:
    for client in (_client(), _client(email=None)):
        svc = _service(_practice(assigned_to=None, created_by=None), client)
        result = await svc.trigger_on_completed(501, "staff@example.test")
        assert result == {"success": True, "team_notified": False}
        assert _sent(svc) == []
        svc._log_activity.assert_awaited_once()


@pytest.mark.asyncio
async def test_assignee_equal_to_client_address_falls_through_to_created_by() -> None:
    svc = _service(_practice(assigned_to=CLIENT_EMAIL), _client())
    await svc.trigger_on_completed(501, "staff@example.test")
    # The listener did not cc the client on their own mail, so the team is not covered: the
    # creator is told, and the client never receives a "[TEAM]" mail.
    assert [to for to, _, _ in _sent(svc)] == [CREATOR]


def test_team_recipient_truth_table() -> None:
    p = _practice()
    assert team_recipient(p, CLIENT_EMAIL) == (None, None)
    assert team_recipient(p, None) == (ASSIGNEE, "no_client_email")
    assert team_recipient(_practice(assigned_to=None), CLIENT_EMAIL) == (CREATOR, "no_assignee")
    assert team_recipient(_practice(assigned_to=None, created_by=None), None) == (
        None,
        "no_client_email",
    )


# --- C1 / C2 ----------------------------------------------------------------------------


_BRANCHES = [
    (_practice(), _client()),
    (_practice(), _client(email=None)),
    (_practice(assigned_to=None), _client()),
    (_practice(assigned_to=None, created_by=None), _client()),
    (_practice(), None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("practice", "client"), _BRANCHES)
async def test_every_branch_writes_one_activity_row(practice, client) -> None:
    svc = _service(practice, client)
    await svc.trigger_on_completed(501, "staff@example.test")
    svc._log_activity.assert_awaited_once()
    assert svc._log_activity.await_args.kwargs["practice_id"] == 501
    assert svc._log_activity.await_args.kwargs["description"] == "Process completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("practice", "client"), _BRANCHES)
async def test_no_client_mail_in_any_branch(practice, client) -> None:
    svc = _service(practice, client)
    await svc.trigger_on_completed(501, "staff@example.test")
    for to, subject, body in _sent(svc):
        assert to != CLIENT_EMAIL
        assert not subject.startswith("[CLIENT]")
        assert "Congratulations" not in subject + body


def test_the_dead_client_mail_and_upload_paths_are_gone() -> None:
    assert not hasattr(CompletedProcessService, "_send_completion_email")
    assert not hasattr(CompletedProcessService, "_upload_final_documents")
    assert not hasattr(CompletedProcessService, "_save_final_document_record")
    assert (
        "final_documents"
        not in inspect.signature(CompletedProcessService.trigger_on_completed).parameters
    )
    svc, _pool = _make_service()
    assert not hasattr(svc, "drive_service")


@pytest.mark.asyncio
async def test_activity_row_survives_a_mail_failure() -> None:
    svc = _service(_practice(), _client(email=None))
    svc._send_with_brevo_fallback.side_effect = RuntimeError("Brevo down")
    result = await svc.trigger_on_completed(501, "staff@example.test")
    assert result == {"success": True, "team_notified": False}
    svc._log_activity.assert_awaited_once()


# --- C8 ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_mail_uses_the_service_display_name_not_the_code() -> None:
    svc = _service(_practice(), _client(email=None))
    await svc.trigger_on_completed(501, "staff@example.test")
    ((_to, subject, body),) = _sent(svc)
    assert "Service Display Name" in subject
    assert "Service Display Name" in body
    assert "CODE_X" not in subject + body


@pytest.mark.asyncio
async def test_team_mail_without_a_service_name_says_service_not_the_code() -> None:
    svc = _service(_practice(practice_type_name=None), _client(email=None))
    await svc.trigger_on_completed(501, "staff@example.test")
    ((_to, subject, body),) = _sent(svc)
    assert subject.endswith("(Service)")
    assert "CODE_X" not in subject + body


# --- C9 / C10 ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_client_data_selects_no_dead_column() -> None:
    svc, pool = _make_service()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    await svc._fetch_client_data(7)
    sql = conn.fetchrow.await_args.args[0]
    assert "drive_documents_folder_id" not in sql
    assert "drive_final_folder_id" not in sql


@pytest.mark.asyncio
async def test_unknown_practice() -> None:
    svc = _service(None, None)
    result = await svc.trigger_on_completed(501, "staff@example.test")
    assert result == {"success": False, "error": "Practice not found"}
    svc._log_activity.assert_not_awaited()
    assert _sent(svc) == []


# ---------------------------------------------------------------------------
# _send_with_brevo_fallback — Zoho fallback removal (2026-09-27)
# ---------------------------------------------------------------------------
#
# GUILT (the bug this fixes): the old "Zoho fallback" branch called
# ``ZohoEmailService.send_email(to_email=..., subject=..., body=...)`` —
# kwargs that never matched the real signature
# ``send_email(user_id, to, subject, content, ...)`` since the call site
# was first written (2026-02-22, before Brevo-primary even existed). Every
# double-failure was therefore recorded with ``provider="zoho"`` even
# though Zoho was never actually reachable through that call. The
# regression signal below is exactly that: a Brevo failure must now
# record ``provider="brevo"``, never ``"zoho"`` — a lone `create_autospec`
# on the (now-deleted) attribute can't express this anymore since the
# attribute itself is gone, which is the point: `test_no_zoho_attribute`
# guards against it coming back.


def test_no_zoho_attribute() -> None:
    """GUILT: ZohoEmailService must not be re-attached to this service —
    it was never a usable fallback (staff-personal OAuth mailbox, not a
    system identity) and every call site for it was broken since 2026-02-22."""
    svc, _pool = _make_service()
    assert not hasattr(svc, "zoho_email_service")


@pytest.mark.asyncio
async def test_brevo_failure_records_failed_brevo_and_alerts() -> None:
    """GUILT: before the fix this row would land as
    ``status='failed', provider='zoho'`` (a wasted, always-broken Zoho
    attempt) instead of ``provider='brevo'`` with an immediate alert."""
    svc, _pool = _make_service()

    mock_client = AsyncMock()
    mock_client.post.side_effect = RuntimeError("Brevo down")

    with (
        patch(
            "backend.services.crm.completed_process_service.get_email_client",
            AsyncMock(return_value=mock_client),
        ),
        patch(
            "backend.services.crm.completed_process_service.log_email_attempt",
            AsyncMock(return_value=42),
        ),
        patch(
            "backend.services.crm.completed_process_service.record_email_result",
            AsyncMock(),
        ) as mock_record,
        patch(
            "backend.services.crm.completed_process_service.notify_email_failure_critical",
            MagicMock(),
        ) as mock_notify,
        pytest.raises(RuntimeError, match="Brevo down"),
    ):
        await svc._send_with_brevo_fallback(
            "uxxxx7@d.com",
            "Subject",
            "Body",
            email_type="completion_client",
            practice_id=1,
            client_id=10,
        )

    mock_record.assert_awaited_once()
    _args, kwargs = mock_record.call_args
    assert kwargs["status"] == "failed"
    assert kwargs["provider"] == "brevo"
    mock_notify.assert_called_once()
    assert mock_notify.call_args.kwargs["email_type"] == "completion_client"


@pytest.mark.asyncio
async def test_brevo_success_records_sent_brevo_no_alert() -> None:
    svc, _pool = _make_service()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with (
        patch(
            "backend.services.crm.completed_process_service.get_email_client",
            AsyncMock(return_value=mock_client),
        ),
        patch(
            "backend.services.crm.completed_process_service.log_email_attempt",
            AsyncMock(return_value=42),
        ),
        patch(
            "backend.services.crm.completed_process_service.record_email_result",
            AsyncMock(),
        ) as mock_record,
        patch(
            "backend.services.crm.completed_process_service.notify_email_failure_critical",
            MagicMock(),
        ) as mock_notify,
    ):
        await svc._send_with_brevo_fallback(
            "uxxxx7@d.com",
            "Subject",
            "Body",
            email_type="completion_client",
            practice_id=1,
            client_id=10,
        )

    mock_record.assert_awaited_once()
    _args, kwargs = mock_record.call_args
    assert kwargs["status"] == "sent"
    assert kwargs["provider"] == "brevo"
    mock_notify.assert_not_called()
