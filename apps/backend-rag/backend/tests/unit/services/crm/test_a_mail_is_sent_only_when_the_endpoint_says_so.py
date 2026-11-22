from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.crm import automation, completed_process_service, waiting_documents_service
from backend.services.crm import birthday_notifier_service as birthday_module
from backend.services.notifications import email_audit


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeAsyncClient:
    def __init__(self, response, *args, **kwargs):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return self.response


@pytest.mark.parametrize(
    ("body", "raises", "uncertain"),
    [
        ({"success": False, "message": "All providers failed"}, True, False),
        ({"success": False, "delivery_uncertain": True}, True, True),
        ({"success": True}, False, False),
        (ValueError("not json"), False, False),
        (["not", "an", "object"], False, False),
    ],
)
def test_raise_if_not_delivered_only_rejects_explicit_endpoint_failure(body, raises, uncertain):
    helper = getattr(email_audit, "raise_if_not_delivered", None)
    assert callable(helper)
    if raises:
        with pytest.raises(email_audit.EmailNotDelivered) as exc_info:
            helper(FakeResponse(body))
        assert exc_info.value.delivery_uncertain is uncertain
    else:
        helper(FakeResponse(body))


def test_raise_if_not_delivered_bounds_the_endpoint_message():
    helper = getattr(email_audit, "raise_if_not_delivered", None)
    assert callable(helper)
    with pytest.raises(RuntimeError) as exc_info:
        helper(FakeResponse({"success": False, "message": "x" * 201}))

    assert str(exc_info.value) == "x" * 200


async def _call_automation():
    await automation._send_with_brevo_fallback(
        MagicMock(),
        "person.one@example.test",
        "Subject",
        "Body",
        email_type="waiting_docs_client",
        practice_id=900001,
        client_id=900001,
    )


async def _call_waiting(service):
    await service._send_with_brevo_fallback(
        "person.one@example.test",
        "Subject",
        "Body",
        email_type="waiting_docs_client",
        practice_id=900001,
        client_id=900001,
    )


async def _call_completed(service):
    await service._send_with_brevo_fallback(
        "person.one@example.test",
        "Subject",
        "Body",
        email_type="completion_client",
        practice_id=900001,
        client_id=900001,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", ["automation", "waiting", "completed"])
async def test_practice_senders_record_failed_when_endpoint_reports_failure(
    monkeypatch, module_name
):
    response = FakeResponse({"success": False, "message": "All providers failed"})
    if module_name == "automation":
        monkeypatch.setattr(
            automation.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(response)
        )
        monkeypatch.setattr(automation, "log_email_attempt", AsyncMock(return_value=1))
        recorded = AsyncMock()
        notified = MagicMock()
        monkeypatch.setattr(automation, "record_email_result", recorded)
        monkeypatch.setattr(automation, "notify_email_failure_critical", notified)
        call = _call_automation()
    else:
        module = (
            waiting_documents_service if module_name == "waiting" else completed_process_service
        )
        client = FakeAsyncClient(response)
        monkeypatch.setattr(module, "get_email_client", AsyncMock(return_value=client))
        monkeypatch.setattr(module, "log_email_attempt", AsyncMock(return_value=1))
        recorded = AsyncMock()
        notified = MagicMock()
        monkeypatch.setattr(module, "record_email_result", recorded)
        monkeypatch.setattr(module, "notify_email_failure_critical", notified)
        service_class = (
            module.WaitingDocumentsService
            if module_name == "waiting"
            else module.CompletedProcessService
        )
        call = (
            _call_waiting(service_class(MagicMock()))
            if module_name == "waiting"
            else _call_completed(service_class(MagicMock()))
        )

    stopped = None
    try:
        await call
    except Exception as cause:  # the failed branch hands its cause back to the caller
        stopped = cause

    statuses = [item.kwargs["status"] for item in recorded.await_args_list]
    assert statuses == ["failed"], statuses
    notified.assert_called_once()
    assert type(stopped).__name__ == "EmailNotDelivered"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [{"success": True}, ValueError("not json"), ["not", "an", "object"]]
)
@pytest.mark.parametrize("module_name", ["automation", "waiting", "completed"])
async def test_practice_senders_keep_existing_success_for_nonfailure_bodies(
    monkeypatch, module_name, body
):
    response = FakeResponse(body)
    if module_name == "automation":
        monkeypatch.setattr(
            automation.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(response)
        )
        monkeypatch.setattr(automation, "log_email_attempt", AsyncMock(return_value=1))
        recorded = AsyncMock()
        monkeypatch.setattr(automation, "record_email_result", recorded)
        call = _call_automation()
    else:
        module = (
            waiting_documents_service if module_name == "waiting" else completed_process_service
        )
        monkeypatch.setattr(
            module, "get_email_client", AsyncMock(return_value=FakeAsyncClient(response))
        )
        monkeypatch.setattr(module, "log_email_attempt", AsyncMock(return_value=1))
        recorded = AsyncMock()
        monkeypatch.setattr(module, "record_email_result", recorded)
        service_class = (
            module.WaitingDocumentsService
            if module_name == "waiting"
            else module.CompletedProcessService
        )
        call = (
            _call_waiting(service_class(MagicMock()))
            if module_name == "waiting"
            else _call_completed(service_class(MagicMock()))
        )

    await call
    recorded.assert_awaited_once()
    assert recorded.await_args.kwargs["status"] == "sent"


class FakeBirthdayEmailService:
    def __init__(self):
        self.send_email = AsyncMock()


def _birthday_service():
    service = birthday_module.BirthdayNotifierService.__new__(
        birthday_module.BirthdayNotifierService
    )
    service.db_pool = None
    service.email_service = FakeBirthdayEmailService()
    return service


@pytest.mark.asyncio
async def test_birthday_falls_back_when_endpoint_reports_not_delivered(monkeypatch):
    service = _birthday_service()
    monkeypatch.setattr(
        birthday_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(FakeResponse({"success": False})),
    )

    assert await service.send_birthday_email(
        {"email": "person.one@example.test", "first_name": "Person"}
    )
    service.email_service.send_email.assert_awaited_once()


@pytest.mark.asyncio
async def test_birthday_does_not_fallback_when_delivery_is_uncertain(monkeypatch):
    service = _birthday_service()
    monkeypatch.setattr(
        birthday_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(
            FakeResponse({"success": False, "delivery_uncertain": True})
        ),
    )

    assert not await service.send_birthday_email(
        {"email": "person.one@example.test", "first_name": "Person"}
    )
    service.email_service.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_birthday_counts_explicit_success_without_fallback(monkeypatch):
    service = _birthday_service()
    monkeypatch.setattr(
        birthday_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(FakeResponse({"success": True})),
    )

    assert await service.send_birthday_email(
        {"email": "person.one@example.test", "first_name": "Person"}
    )
    service.email_service.send_email.assert_not_awaited()
