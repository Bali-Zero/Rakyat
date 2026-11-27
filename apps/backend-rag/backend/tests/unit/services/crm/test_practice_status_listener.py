"""Tests for backend.services.crm.practice_status_listener"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.crm.delivery_progress import DeliveryProgress
from backend.services.crm.practice_status_listener import (
    PracticeStatusListener,
    _milestone_content,
)


@pytest.fixture
def mock_pool():
    return AsyncMock()


@pytest.fixture
def listener(mock_pool):
    with patch("backend.services.crm.practice_status_listener.ProcessAutomationService") as MockPAS:
        MockPAS.return_value = AsyncMock()
        inst = PracticeStatusListener(db_dsn="postgres://fake:5432/test", db_pool=mock_pool)
        return inst


# ── _milestone_content ───────────────────────────────────────────────────────


class TestMilestoneContent:
    def test_submitted(self):
        subject, body = _milestone_content("John", "KITAS", 42, "submitted")
        assert "Submitted" in subject
        assert "John" in body
        assert "KITAS" in body

    def test_approved(self):
        subject, body = _milestone_content("Jane", "PT PMA", 99, "approved")
        assert "APPROVED" in subject
        assert "Jane" in body

    def test_completed(self):
        subject, body = _milestone_content("Bob", "Tax Filing", 10, "completed")
        assert "Completed" in subject
        assert "Bob" in body

    def test_unknown_status(self):
        subject, body = _milestone_content("Alice", "Visa", 7, "some_other_status")
        assert "Update" in subject
        assert "some_other_status" in body
        assert "Alice" in body

    def test_portal_url_included(self):
        _, body = _milestone_content("X", "Y", 1, "approved")
        assert "my.balizero.com" in body


# ── PracticeStatusListener lifecycle ────────────────────────────────────────


class TestPracticeStatusListenerLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_task(self, listener):
        await listener.start()
        assert listener._running is True
        assert listener._task is not None
        # Clean up
        await listener.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, listener):
        await listener.start()
        task1 = listener._task
        await listener.start()
        assert listener._task is task1
        await listener.stop()

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, listener):
        await listener.start()
        await listener.stop()
        assert listener._running is False

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self, listener):
        await listener.stop()
        assert listener._running is False


# ── _handle_notification ────────────────────────────────────────────────────


class TestHandleNotification:
    @pytest.mark.asyncio
    async def test_invalid_json(self, listener):
        with patch(
            "backend.services.crm.practice_status_listener.logger"
        ) as mock_logger:
            await listener._handle_notification("not valid json{{{")

        mock_logger.warning.assert_called_once_with(
            "practice_changed: invalid JSON payload: 'not valid json{{{'"
        )
        mock_logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_payment_transition_to_paid(self, listener):
        listener._on_payment_received = AsyncMock()
        payload = json.dumps(
            {
                "practice_id": 42,
                "old_status": "inquiry",
                "new_status": "inquiry",
                "old_payment": "unpaid",
                "new_payment": "paid",
            }
        )
        await listener._handle_notification(payload)
        listener._on_payment_received.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_payment_transition_if_already_paid(self, listener):
        listener._on_payment_received = AsyncMock()
        payload = json.dumps(
            {
                "practice_id": 42,
                "old_payment": "paid",
                "new_payment": "paid",
            }
        )
        await listener._handle_notification(payload)
        listener._on_payment_received.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_status_change_to_on_process(self, listener):
        listener._on_status_changed = AsyncMock()
        payload = json.dumps(
            {
                "practice_id": 42,
                "old_status": "inquiry",
                "new_status": "on_process",
            }
        )
        await listener._handle_notification(payload)
        listener._on_status_changed.assert_awaited_once_with(
            42,
            "on_process",
            pytest.approx(json.loads(payload), abs=0),
        )

    @pytest.mark.asyncio
    async def test_status_change_to_submitted(self, listener):
        listener._on_status_changed = AsyncMock()
        payload = json.dumps(
            {
                "practice_id": 42,
                "old_status": "on_process",
                "new_status": "submitted",
            }
        )
        await listener._handle_notification(payload)
        listener._on_status_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_status_unchanged_no_dispatch(self, listener):
        listener._on_status_changed = AsyncMock()
        payload = json.dumps(
            {
                "practice_id": 42,
                "old_status": "inquiry",
                "new_status": "inquiry",
            }
        )
        await listener._handle_notification(payload)
        listener._on_status_changed.assert_not_awaited()


# ── _on_payment_received ────────────────────────────────────────────────────


class TestOnPaymentReceived:
    @pytest.mark.asyncio
    async def test_dispatches_to_send_payment_emails(self, listener):
        listener._send_payment_emails = AsyncMock()
        data = {"assigned_to": "team@balizero.com"}
        await listener._on_payment_received(42, data)
        listener._send_payment_emails.assert_awaited_once_with(
            42,
            triggered_by="team@balizero.com",
        )

    @pytest.mark.asyncio
    async def test_default_assigned_to(self, listener):
        listener._send_payment_emails = AsyncMock()
        data = {}
        await listener._on_payment_received(42, data)
        listener._send_payment_emails.assert_awaited_once_with(
            42,
            triggered_by="asya@balizero.com",
        )

    @pytest.mark.asyncio
    async def test_exception_caught(self, listener):
        listener._send_payment_emails = AsyncMock(side_effect=Exception("fail"))
        with patch(
            "backend.services.crm.practice_status_listener.logger"
        ) as mock_logger:
            await listener._on_payment_received(42, {})

        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        assert call_args.args[0] == "M4 payment email unexpected error for practice %s: %s"
        assert call_args.args[1] == 42
        assert str(call_args.args[2]) == "fail"
        assert call_args.kwargs == {"exc_info": True}


# ── _on_status_changed ──────────────────────────────────────────────────────


class TestOnStatusChanged:
    @pytest.mark.asyncio
    async def test_on_process_delegates_to_process_svc(self, listener):
        listener._process_svc.trigger_on_process_start = AsyncMock()
        await listener._on_status_changed(42, "on_process", {"assigned_to": "user"})
        listener._process_svc.trigger_on_process_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_other_status_sends_milestone_email(self, listener):
        listener._send_milestone_email = AsyncMock()
        await listener._on_status_changed(42, "approved", {"assigned_to": "user"})
        listener._send_milestone_email.assert_awaited_once_with(42, "approved", "user")

    @pytest.mark.asyncio
    async def test_exception_caught(self, listener):
        listener._process_svc.trigger_on_process_start = AsyncMock(
            side_effect=Exception("fail"),
        )
        with patch(
            "backend.services.crm.practice_status_listener.logger"
        ) as mock_logger:
            await listener._on_status_changed(42, "on_process", {})

        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        assert (
            call_args.args[0]
            == "M5: status milestone handler unexpected error for practice %s new_status=%s: %s"
        )
        assert call_args.args[1] == 42
        assert call_args.args[2] == "on_process"
        assert str(call_args.args[3]) == "fail"


# ── _send_payment_emails ────────────────────────────────────────────────────


class TestSendPaymentEmails:
    @pytest.mark.asyncio
    async def test_practice_not_found(self, listener):
        listener._process_svc._fetch_practice_data = AsyncMock(return_value=None)
        listener._process_svc._fetch_client_data = AsyncMock()
        with patch(
            "backend.services.crm.practice_status_listener.logger"
        ) as mock_logger:
            await listener._send_payment_emails(42, triggered_by="test")

        listener._process_svc._fetch_client_data.assert_not_called()
        mock_logger.error.assert_called_once_with("M4: practice %s not found", 42)

    @pytest.mark.asyncio
    async def test_client_not_found(self, listener):
        listener._process_svc._fetch_practice_data = AsyncMock(
            return_value={"client_id": 1, "practice_type_name": "KITAS"},
        )
        listener._process_svc._fetch_client_data = AsyncMock(return_value=None)
        listener._send_via_internal_api = AsyncMock()
        with patch(
            "backend.services.crm.practice_status_listener.logger"
        ) as mock_logger:
            await listener._send_payment_emails(42, triggered_by="test")

        listener._send_via_internal_api.assert_not_called()
        mock_logger.error.assert_called_once_with("M4: client 1 not found")

    @pytest.mark.asyncio
    async def test_sends_client_and_team_emails(self, listener):
        listener._process_svc._fetch_practice_data = AsyncMock(
            return_value={
                "client_id": 1,
                "practice_type_name": "KITAS",
                "assigned_to": "team@balizero.com",
                "quoted_price": 5000000,
            },
        )
        listener._process_svc._fetch_client_data = AsyncMock(
            return_value={"full_name": "John Doe", "email": "john@example.com"},
        )
        listener._send_via_internal_api = AsyncMock()

        await listener._send_payment_emails(42, triggered_by="test")
        assert listener._send_via_internal_api.await_count == 2


# ── _send_via_internal_api ──────────────────────────────────────────────────


class TestSendViaInternalApi:
    @pytest.mark.asyncio
    async def test_sends_request(self, listener):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.services.crm.practice_status_listener.httpx.AsyncClient",
            return_value=mock_client,
        ):
            await listener._send_via_internal_api(
                "test@example.com",
                "Subject",
                "Body",
                cc="cc@example.com",
            )
            mock_client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_html_newline_replacement(self, listener):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.services.crm.practice_status_listener.httpx.AsyncClient",
            return_value=mock_client,
        ):
            await listener._send_via_internal_api("c@b.com", "S", "Line1\nLine2")
            call_kwargs = mock_client.post.call_args
            body = call_kwargs.kwargs["json"]["body"]
            assert "<br>" in body
            assert "\n" not in body


# ── _close_conn ─────────────────────────────────────────────────────────────


class TestCloseConn:
    @pytest.mark.asyncio
    async def test_close_when_no_conn(self, listener):
        listener._conn = None
        await listener._close_conn()
        assert listener._conn is None

    @pytest.mark.asyncio
    async def test_close_when_conn_exists(self, listener):
        mock_conn = MagicMock()
        mock_conn.is_closed.return_value = False
        mock_conn.remove_listener = AsyncMock()
        mock_conn.close = AsyncMock()
        listener._conn = mock_conn
        await listener._close_conn()
        assert listener._conn is None


# ── completion wording: which final documents the client can already see ────────

_TODAY_SUBJECT = "[CLIENT] ✅ Completed: Your C1 Documents Are Ready"


class TestCompletionWording:
    def test_full_and_legacy_are_today_verbatim(self):
        today = _milestone_content("CLIENT", "C1", 7, "completed")
        assert today[0] == _TODAY_SUBJECT
        assert "fully complete" in today[1] and "review" in today[1]
        for wording in ("full", "legacy"):
            assert _milestone_content("CLIENT", "C1", 7, "completed", wording) == today

    def test_partial_says_the_rest_will_follow(self):
        subject, body = _milestone_content("CLIENT", "C1", 7, "completed", "partial")
        assert subject == "[CLIENT] Your C1: first documents delivered"
        assert "The first final documents of your C1 are ready in your portal" in body
        assert "The remaining documents will follow" in body
        for banned in ("fully complete", "Documents Are Ready", "review"):
            assert banned not in subject + body

    def test_neutral_claims_no_documents(self):
        subject, body = _milestone_content("CLIENT", "C1", 7, "completed", "neutral")
        assert subject == "[CLIENT] Completed: Your C1"
        assert "Your C1 process is complete. Your advisor will be in touch shortly" in body
        for banned in ("fully complete", "Documents Are Ready", "review", "documents"):
            assert banned not in subject + body

    def test_wording_changes_nothing_but_the_completed_mail(self):
        assert _milestone_content("X", "Y", 1, "approved", "partial") == _milestone_content(
            "X", "Y", 1, "approved"
        )


def _wired(listener, progress=None, *, boom=None):
    """The listener with a real-shaped pool and the helper replaced, send captured."""
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    listener._db_pool = pool
    svc = listener._process_svc
    svc._fetch_practice_data = AsyncMock(
        return_value={"client_id": 31, "practice_type_name": "C1", "assigned_to": None}
    )
    svc._fetch_client_data = AsyncMock(
        return_value={"full_name": "CLIENT", "email": "client@example.test"}
    )
    listener._send_logged = AsyncMock()
    return patch(
        "backend.services.crm.practice_status_listener.documents_pending",
        AsyncMock(return_value=progress, side_effect=boom),
    )


class TestSendMilestoneEmailWording:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("progress", "subject"),
        [
            (DeliveryProgress(True, True, False), "[CLIENT] Your C1: first documents delivered"),
            (DeliveryProgress(False, True, False), "[CLIENT] Your C1: first documents delivered"),
            (DeliveryProgress(True, False, False), "[CLIENT] Completed: Your C1"),
            (DeliveryProgress(False, True, True), _TODAY_SUBJECT),
            (None, _TODAY_SUBJECT),
        ],
    )
    async def test_subject_follows_the_helper(self, listener, progress, subject):
        with _wired(listener, progress):
            await listener._send_milestone_email(7, "completed", "staff")
        args = listener._send_logged.await_args.args
        assert args[0] == "completion_client"
        assert args[4] == subject

    @pytest.mark.asyncio
    async def test_helper_exception_is_neutral_never_full(self, listener):
        with _wired(listener, boom=RuntimeError("db gone")):
            await listener._send_milestone_email(7, "completed", "staff")
        assert listener._send_logged.await_args.args[4] == "[CLIENT] Completed: Your C1"

    @pytest.mark.asyncio
    async def test_pool_failure_is_neutral_never_full(self, listener):
        with _wired(listener, DeliveryProgress(False, True, True)):
            listener._db_pool.acquire.side_effect = ConnectionError("pool closed")
            await listener._send_milestone_email(7, "completed", "staff")
        assert listener._send_logged.await_args.args[4] == "[CLIENT] Completed: Your C1"

    @pytest.mark.asyncio
    async def test_other_milestones_do_not_ask_the_helper(self, listener):
        with _wired(listener, DeliveryProgress(True, True, False)):
            with patch(
                "backend.services.crm.practice_status_listener.documents_pending",
                AsyncMock(side_effect=AssertionError("asked the helper")),
            ):
                await listener._send_milestone_email(7, "approved", "staff")
        assert listener._send_logged.await_args.args[0] == "status_approved_client"
        assert "APPROVED" in listener._send_logged.await_args.args[4]
