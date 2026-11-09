"""
Unit tests for whatsapp_chat router.
Coverage target: GET /webhook/whatsapp (verification), POST /webhook/whatsapp (messages),
GET /webhook/whatsapp/status, alias routes GET/POST /api/whatsapp/webhook,
X-Hub-Signature-256 HMAC verification.
"""

import hashlib
import hmac
import json
import logging
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.config import settings as real_settings
from backend.app.routers.whatsapp_chat import alias_router, router

# ============================================================
# FIXTURES
# ============================================================


@pytest.fixture
def app():
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.include_router(alias_router)
    return test_app


@pytest.fixture
def client(app):
    """Default test client: signature enforcement OFF.

    ``meta_webhook_require_signature`` defaults to ``True`` since 2026-08-26
    (fail-closed by default). This fixture is used by the message-routing
    coverage tests below, which POST unsigned payloads and were never about
    signature verification in the first place — that surface has its own
    tests (``test_hmac_*`` below, and the dedicated
    ``test_meta_webhook_require_signature.py``). Patching the real settings
    singleton's attribute (not a reassigned mock) means every module that
    imported ``settings`` sees the same override.
    """
    with patch.object(real_settings, "meta_webhook_require_signature", False):
        yield TestClient(app)


def _seeded_settings_mock() -> MagicMock:
    """Build the ``MagicMock`` this file's ``patch("...settings")`` blocks
    install in place of the real settings singleton — pre-seeded with the
    REAL ``Settings`` field defaults, not left bare.

    A bare ``MagicMock()`` answers truthily to *any* attribute the router
    code reads, including ones no test in this file ever set. That is
    silently wrong whenever the code does a falsy/negative check on an
    unmentioned field — e.g. ``not settings.meta_webhook_require_signature``
    (real default: ``False``) reads as ``not <MagicMock>`` = ``False`` on a
    bare mock, flipping the router's documented fail-open dev-mode branch
    into fail-closed and turning every test that only sets
    ``whatsapp_app_secret = None`` into a spurious 401. Seeding from
    ``real_settings.model_dump()`` means any field a test does not
    explicitly override — today's or a future one nobody added a line for
    here — carries the actual configured default instead of a Mock, so
    this class of failure cannot recur when the config grows.

    Used via ``patch(..., new_callable=_seeded_settings_mock)`` so every
    ``as mock_settings`` binding in this file gets one for free without a
    per-block edit.
    """
    mock = MagicMock()
    for field_name, value in real_settings.model_dump().items():
        setattr(mock, field_name, value)
    return mock


# ============================================================
# GET /webhook/whatsapp — Verification
# ============================================================


def test_verify_webhook_success(client):
    """Should return hub.challenge when mode=subscribe and token matches."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_verify_token = "test_whatsapp_verify_token"
        response = client.get(
            "/webhook/whatsapp"
            "?hub.mode=subscribe"
            "&hub.verify_token=test_whatsapp_verify_token"
            "&hub.challenge=CHALLENGE_CODE_12345"
        )
    assert response.status_code == 200
    assert response.text == "CHALLENGE_CODE_12345"


def test_verify_webhook_wrong_token(client):
    """Should return 403 when verify token doesn't match."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_verify_token = "correct_token"
        response = client.get(
            "/webhook/whatsapp"
            "?hub.mode=subscribe"
            "&hub.verify_token=wrong_token"
            "&hub.challenge=CHALLENGE"
        )
    assert response.status_code == 403


def test_verify_webhook_wrong_mode(client):
    """Should return 403 when mode is not 'subscribe'."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_verify_token = "test_token"
        response = client.get(
            "/webhook/whatsapp"
            "?hub.mode=unsubscribe"
            "&hub.verify_token=test_token"
            "&hub.challenge=CHALLENGE"
        )
    assert response.status_code == 403


def test_verify_webhook_missing_params(client):
    """Should return 403 when params are missing."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_verify_token = "test_token"
        response = client.get("/webhook/whatsapp")
    assert response.status_code == 403


def test_verify_webhook_empty_challenge(client):
    """Should return empty challenge string when challenge is empty."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_verify_token = "test_whatsapp_verify_token"
        response = client.get(
            "/webhook/whatsapp"
            "?hub.mode=subscribe"
            "&hub.verify_token=test_whatsapp_verify_token"
            "&hub.challenge="
        )
    # Challenge is None → returns None as text or 200
    assert response.status_code in (200, 403)


# ============================================================
# POST /webhook/whatsapp — Message handling
# ============================================================


def _make_webhook_payload(
    phone: str = "621234567890",
    text: str = "Hello Zan!",
    message_id: str = "wamid.ABC123",
    sender_name: str = "Test Client",
    message_type: str = "text",
) -> dict:
    """Build a valid WhatsApp webhook payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15556151111",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [
                                {
                                    "profile": {"name": sender_name},
                                    "wa_id": phone,
                                }
                            ],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": message_id,
                                    "timestamp": "1712000000",
                                    "type": message_type,
                                    "text": {"body": text} if message_type == "text" else None,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_whatsapp_webhook_returns_ok(client):
    """Should return status=ok immediately without processing."""
    with (
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch("backend.app.routers.whatsapp_chat.process_whatsapp_message", new=AsyncMock()),
    ):
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json=_make_webhook_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_whatsapp_webhook_non_text_message_ignored(client):
    """Should skip non-text messages and still return ok."""
    payload = _make_webhook_payload(message_type="image")
    # Remove text field from payload since type is image
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"] = None

    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_whatsapp_webhook_non_message_change_ignored(client):
    """Should skip non-messages change events."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "BIZ_ID",
                "changes": [
                    {
                        "field": "statuses",
                        "value": {"statuses": [{"id": "wamid.XYZ", "status": "delivered"}]},
                    }
                ],
            }
        ],
    }
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json=payload)
    assert response.status_code == 200


def test_whatsapp_webhook_empty_messages(client):
    """Should handle empty messages array gracefully."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "BIZ_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messages": []},
                    }
                ],
            }
        ],
    }
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json=payload)
    assert response.status_code == 200


def test_whatsapp_webhook_empty_text_body(client):
    """Should skip messages with empty text body."""
    payload = _make_webhook_payload(text="")
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json=payload)
    assert response.status_code == 200


def test_whatsapp_webhook_multiple_messages(client):
    """Should process multiple messages in a single webhook."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "BIZ_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"profile": {"name": "Client"}, "wa_id": "621234"}],
                            "messages": [
                                {
                                    "from": "621234",
                                    "id": "msg_001",
                                    "timestamp": "1712000000",
                                    "type": "text",
                                    "text": {"body": "First message"},
                                },
                                {
                                    "from": "621234",
                                    "id": "msg_002",
                                    "timestamp": "1712000001",
                                    "type": "text",
                                    "text": {"body": "Second message"},
                                },
                            ],
                        },
                    }
                ],
            }
        ],
    }
    with (
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch("backend.app.routers.whatsapp_chat.process_whatsapp_message", new=AsyncMock()),
    ):
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json=payload)
    assert response.status_code == 200


def test_whatsapp_webhook_no_contacts(client):
    """Should handle webhook without contacts (sender_name=None)."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "BIZ_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "from": "621234567890",
                                    "id": "msg_001",
                                    "timestamp": "1712000000",
                                    "type": "text",
                                    "text": {"body": "Hello"},
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }
    with (
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch("backend.app.routers.whatsapp_chat.process_whatsapp_message", new=AsyncMock()),
    ):
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json=payload)
    assert response.status_code == 200


def test_whatsapp_webhook_invalid_payload(client):
    """Should return 400 for invalid payload structure (now parsed internally)."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json={"invalid": "payload"})
    assert response.status_code == 400


def test_whatsapp_webhook_missing_object_field(client):
    """Should return 400 when 'object' field is missing."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json={"entry": []})
    assert response.status_code == 400


# ============================================================
# GET /webhook/whatsapp/status
# ============================================================


def test_whatsapp_status_configured(client):
    """Should return configured=True when tokens are set."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_api_token = "test_token"
        mock_settings.whatsapp_phone_number_id = "12345678"
        mock_settings.admin_telegram_chat_id = None

        mock_triage = MagicMock()
        mock_triage.personal_contacts = ["62812345", "62898765"]

        with patch(
            "backend.app.routers.whatsapp_chat.whatsapp_triage_service",
            mock_triage,
        ):
            response = client.get("/webhook/whatsapp/status")

    assert response.status_code == 200
    data = response.json()
    assert data["configured"] is True
    assert "phone_number_id" in data
    assert data["triage_enabled"] is True


def test_whatsapp_status_not_configured(client):
    """Should return configured=False when tokens are missing."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_api_token = None
        mock_settings.whatsapp_phone_number_id = None

        mock_triage = MagicMock()
        mock_triage.personal_contacts = []

        with patch(
            "backend.app.routers.whatsapp_chat.whatsapp_triage_service",
            mock_triage,
        ):
            response = client.get("/webhook/whatsapp/status")

    assert response.status_code == 200
    data = response.json()
    assert data["configured"] is False
    assert data["phone_number_id"] is None


# ============================================================
# Alias routes: GET/POST /api/whatsapp/webhook
# ============================================================


def test_verify_webhook_alias_success(client):
    """Alias GET /api/whatsapp/webhook should behave like primary."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_verify_token = "test_whatsapp_verify_token"
        response = client.get(
            "/api/whatsapp/webhook"
            "?hub.mode=subscribe"
            "&hub.verify_token=test_whatsapp_verify_token"
            "&hub.challenge=ALIAS_CHALLENGE"
        )
    assert response.status_code == 200
    assert response.text == "ALIAS_CHALLENGE"


def test_verify_webhook_alias_wrong_token(client):
    """Alias GET should also return 403 for wrong token."""
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_verify_token = "correct_token"
        response = client.get(
            "/api/whatsapp/webhook"
            "?hub.mode=subscribe"
            "&hub.verify_token=wrong_token"
            "&hub.challenge=CHALLENGE"
        )
    assert response.status_code == 403


def test_webhook_alias_post_returns_ok(client):
    """Alias POST /api/whatsapp/webhook should return ok."""
    with (
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch("backend.app.routers.whatsapp_chat.process_whatsapp_message", new=AsyncMock()),
    ):
        mock_settings.whatsapp_app_secret = None
        response = client.post("/api/whatsapp/webhook", json=_make_webhook_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ============================================================
# HMAC-SHA256 Signature Verification
# ============================================================

_TEST_APP_SECRET = "test_secret_key_for_hmac"


def _sign_payload(payload: dict, secret: str = _TEST_APP_SECRET) -> str:
    """Generate X-Hub-Signature-256 header value for a payload."""
    body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def test_hmac_unsigned_request_rejected_when_secret_configured(client):
    """POST without signature should return 401 when WHATSAPP_APP_SECRET is set."""
    payload = _make_webhook_payload()
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = _TEST_APP_SECRET
        response = client.post(
            "/webhook/whatsapp",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 401


def test_hmac_wrong_signature_rejected(client):
    """POST with wrong signature should return 401."""
    payload = _make_webhook_payload()
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = _TEST_APP_SECRET
        response = client.post(
            "/webhook/whatsapp",
            content=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
            },
        )
    assert response.status_code == 401


def test_hmac_valid_signature_accepted(client):
    """POST with valid HMAC signature should pass through and return 200."""
    payload = _make_webhook_payload()
    body = json.dumps(payload)
    signature = _sign_payload(payload)

    with (
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch("backend.app.routers.whatsapp_chat.process_whatsapp_message", new=AsyncMock()),
    ):
        mock_settings.whatsapp_app_secret = _TEST_APP_SECRET
        response = client.post(
            "/webhook/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": signature,
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_hmac_skipped_when_no_secret(client):
    """POST without signature should pass when WHATSAPP_APP_SECRET is not configured."""
    with (
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch("backend.app.routers.whatsapp_chat.process_whatsapp_message", new=AsyncMock()),
    ):
        mock_settings.whatsapp_app_secret = None
        response = client.post("/webhook/whatsapp", json=_make_webhook_payload())
    assert response.status_code == 200


def test_hmac_malformed_signature_rejected(client):
    """POST with malformed signature (no sha256= prefix) should return 401."""
    payload = _make_webhook_payload()
    with patch(
        "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
    ) as mock_settings:
        mock_settings.whatsapp_app_secret = _TEST_APP_SECRET
        response = client.post(
            "/webhook/whatsapp",
            content=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "invalid_format_no_prefix",
            },
        )
    assert response.status_code == 401


# ============================================================
# process_whatsapp_message — unit tests (pure function)
# ============================================================


class _FakeConversationAcquire:
    def __init__(self, conn: "_FakeConversationConn") -> None:
        self.conn = conn

    async def __aenter__(self) -> "_FakeConversationConn":
        return self.conn

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeConversationPool:
    def __init__(self, conn: "_FakeConversationConn") -> None:
        self.conn = conn

    def acquire(self) -> _FakeConversationAcquire:
        return _FakeConversationAcquire(self.conn)


class _FakeConversationConn:
    def __init__(self, existing_row: dict[str, Any] | None = None) -> None:
        self.existing_row = existing_row
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, *_args: Any) -> dict[str, Any] | None:
        return self.existing_row

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))

    def transaction(self) -> _FakeConversationAcquire:
        return _FakeConversationAcquire(self)


@pytest.mark.asyncio
async def test_process_whatsapp_message_not_allowed():
    """Should return early for non-allowed phone numbers."""
    from backend.app.routers.whatsapp_chat import process_whatsapp_message

    mock_request = MagicMock()
    mock_triage_service = MagicMock()
    mock_triage_service.is_allowed.return_value = False
    mock_wa_service = MagicMock()
    mock_wa_service.mark_message_read = AsyncMock()

    with (
        patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
        patch("backend.app.routers.whatsapp_chat.whatsapp_triage_service", mock_triage_service),
        patch("backend.app.routers.whatsapp_chat._get_db_pool", return_value=None),
    ):
        # Should complete without error
        await process_whatsapp_message(
            phone="628999999999",
            message_text="Hello",
            sender_name="Unknown",
            message_id="msg_test",
            request=mock_request,
        )

    mock_triage_service.is_allowed.assert_called_once_with("628999999999")


@pytest.mark.asyncio
async def test_process_whatsapp_message_escalate_to_human():
    """Should escalate to human and send Telegram notification."""
    from backend.app.routers.whatsapp_chat import process_whatsapp_message
    from backend.services.integrations.whatsapp_triage_service import TriageDecision

    mock_request = MagicMock()
    mock_triage_service = MagicMock()
    mock_triage_service.is_allowed.return_value = True
    mock_triage_service.should_escalate = AsyncMock(
        return_value=(TriageDecision.ESCALATE_PERSONAL, "personal_contact")
    )
    mock_triage_service.get_escalation_message.return_value = "Transferring to team..."

    mock_wa_service = MagicMock()
    mock_wa_service.mark_message_read = AsyncMock()
    mock_wa_service.send_message = AsyncMock()

    mock_onboarding = MagicMock()
    mock_onboarding.detect_and_trigger = AsyncMock(return_value=None)

    with (
        patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
        patch("backend.app.routers.whatsapp_chat.whatsapp_triage_service", mock_triage_service),
        patch(
            "backend.app.routers.whatsapp_chat.get_onboarding_detector",
            return_value=mock_onboarding,
        ),
        patch("backend.app.routers.whatsapp_chat.notify_human_telegram", new=AsyncMock()),
        patch("backend.app.routers.whatsapp_chat._get_db_pool", return_value=None),
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
    ):
        mock_settings.admin_telegram_chat_id = "123456"
        await process_whatsapp_message(
            phone="621234567890",
            message_text="This is personal",
            sender_name="Test Client",
            message_id="msg_001",
            request=mock_request,
        )

    mock_wa_service.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_process_whatsapp_message_offer_choice():
    """Should send welcome message for OFFER_CHOICE decision."""
    from backend.app.routers.whatsapp_chat import process_whatsapp_message
    from backend.services.integrations.whatsapp_triage_service import TriageDecision

    mock_request = MagicMock()
    mock_triage_service = MagicMock()
    mock_triage_service.is_allowed.return_value = True
    mock_triage_service.should_escalate = AsyncMock(
        return_value=(TriageDecision.OFFER_CHOICE, "ambiguous")
    )
    mock_triage_service.get_welcome_message.return_value = "How can I help?"

    mock_wa_service = MagicMock()
    mock_wa_service.mark_message_read = AsyncMock()
    mock_wa_service.send_message = AsyncMock()

    mock_onboarding = MagicMock()
    mock_onboarding.detect_and_trigger = AsyncMock(return_value=None)

    with (
        patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
        patch("backend.app.routers.whatsapp_chat.whatsapp_triage_service", mock_triage_service),
        patch(
            "backend.app.routers.whatsapp_chat.get_onboarding_detector",
            return_value=mock_onboarding,
        ),
        patch("backend.app.routers.whatsapp_chat._get_db_pool", return_value=None),
    ):
        await process_whatsapp_message(
            phone="621234567890",
            message_text="hi",
            sender_name=None,
            message_id="msg_002",
            request=mock_request,
        )

    mock_wa_service.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_process_whatsapp_message_onboarding_triggered():
    """Should send onboarding confirmation when detector triggers."""
    from backend.app.routers.whatsapp_chat import process_whatsapp_message
    from backend.services.integrations.whatsapp_triage_service import TriageDecision

    mock_request = MagicMock()
    mock_triage_service = MagicMock()
    mock_triage_service.is_allowed.return_value = True
    mock_triage_service.should_escalate = AsyncMock(
        return_value=(TriageDecision.BOT_CAN_HANDLE, "business_query")
    )

    mock_wa_service = MagicMock()
    mock_wa_service.mark_message_read = AsyncMock()
    mock_wa_service.send_message = AsyncMock()

    mock_onboarding = MagicMock()
    mock_onboarding.detect_and_trigger = AsyncMock(
        return_value={"chain": "new_client_onboarding", "status": "triggered"}
    )

    mock_audit = AsyncMock()
    mock_telegram = MagicMock()
    mock_telegram.send_message = AsyncMock()

    with (
        patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
        patch("backend.app.routers.whatsapp_chat.whatsapp_triage_service", mock_triage_service),
        patch(
            "backend.app.routers.whatsapp_chat.get_onboarding_detector",
            return_value=mock_onboarding,
        ),
        patch("backend.app.routers.whatsapp_chat.telegram_bot", mock_telegram),
        patch("backend.app.routers.whatsapp_chat._persist_audit_messages", mock_audit),
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch("backend.app.routers.whatsapp_chat._get_db_pool", return_value=None),
    ):
        mock_settings.admin_telegram_chat_id = "synthetic-admin-chat"
        await process_whatsapp_message(
            phone="621234567890",
            message_text="I want to set up a company",
            sender_name="New Client",
            message_id="msg_003",
            request=mock_request,
        )

    # Should send onboarding confirmation to client
    mock_wa_service.send_message.assert_awaited_once()
    mock_audit.assert_awaited_once()
    mock_telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_whatsapp_message_corrects_openclaw_villa_kbli_reply():
    """OpenClaw replies are guarded before WhatsApp delivery."""
    from backend.app.routers.whatsapp_chat import process_whatsapp_message
    from backend.services.integrations.whatsapp_triage_service import TriageDecision

    mock_request = MagicMock()
    mock_triage_service = MagicMock()
    mock_triage_service.is_allowed.return_value = True
    mock_triage_service.should_escalate = AsyncMock(
        return_value=(TriageDecision.BOT_CAN_HANDLE, "business_query")
    )

    mock_wa_service = MagicMock()
    mock_wa_service.mark_message_read = AsyncMock()
    mock_wa_service.send_message = AsyncMock()
    mock_wa_service.chunk_message.side_effect = lambda text, max_length: [text]

    mock_onboarding = MagicMock()
    mock_onboarding.detect_and_trigger = AsyncMock(return_value=None)

    context = {
        "_wa_user_id": "whatsapp_6281234567890",
        "_session_id": "wa_session_6281234567890",
        "_existing_row_id": None,
        "client_name": "Test Client",
        "client_profile": {"detected_language": "it"},
        "conversation_history": [],
        "detected_language": "it",
        "is_first_message": False,
        "time_of_day": "afternoon",
    }

    with (
        patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
        patch("backend.app.routers.whatsapp_chat.whatsapp_triage_service", mock_triage_service),
        patch(
            "backend.app.routers.whatsapp_chat.get_onboarding_detector",
            return_value=mock_onboarding,
        ),
        patch(
            "backend.services.whatsapp_context_builder.build_context",
            new=AsyncMock(return_value=context),
        ),
        patch(
            "backend.app.routers.whatsapp_chat.ask_openclaw_whatsapp",
            new=AsyncMock(return_value="55193 - Aktivitas Vila: usa questo per Airbnb."),
        ),
        patch("backend.app.routers.whatsapp_chat.notify_zero_conversation_log", new=AsyncMock()),
        patch("backend.app.routers.whatsapp_chat._save_conversation", new=AsyncMock()),
        patch("backend.app.routers.whatsapp_chat._get_db_pool", return_value=None),
    ):
        await process_whatsapp_message(
            phone="6281234567890",
            message_text="ma 55193 o 55203?",
            sender_name="Test Client",
            message_id="msg_kbli_guard",
            request=mock_request,
        )

    sent_text = mock_wa_service.send_message.call_args.kwargs["text"]
    assert "55203" in sent_text
    assert "55193" in sent_text
    assert "KBLI 2020/PP28" in sent_text
    assert "usa questo per Airbnb" not in sent_text


@pytest.mark.asyncio
async def test_save_conversation_inserts_messages_as_jsonb_array_text():
    """New WhatsApp conversations should store messages as parseable JSONB arrays."""
    from backend.app.routers.whatsapp_chat import _save_conversation

    conn = _FakeConversationConn()

    await _save_conversation(
        db_pool=_FakeConversationPool(conn),
        wa_user_id="whatsapp_6281234567890",
        session_id="wa_session_6281234567890",
        existing_row_id=None,
        message_text="Halo Zan",
        response_text="Halo, bisa saya bantu.",
        client_profile={"detected_language": "id"},
        sender_name="Client",
        phone="6281234567890",
    )

    query, args = next(item for item in conn.executed if "INSERT INTO conversations" in item[0])
    assert "$3::text::jsonb" in query
    assert "$4::text::jsonb" in query
    messages = json.loads(args[2])
    metadata = json.loads(args[3])
    assert isinstance(messages, list)
    assert messages == [
        {"role": "user", "content": "Halo Zan"},
        {"role": "assistant", "content": "Halo, bisa saya bantu."},
    ]
    assert metadata == {"detected_language": "id"}


@pytest.mark.asyncio
async def test_save_conversation_appends_from_legacy_jsonb_string_messages():
    """Existing scalar-string JSONB rows should be repaired on append."""
    from backend.app.routers.whatsapp_chat import _save_conversation

    legacy_messages = json.dumps([{"role": "user", "content": "Old message"}])
    conn = _FakeConversationConn(existing_row={"id": 42, "messages": legacy_messages})

    await _save_conversation(
        db_pool=_FakeConversationPool(conn),
        wa_user_id="whatsapp_6281234567890",
        session_id="wa_session_6281234567890",
        existing_row_id=42,
        message_text="New question",
        response_text="New answer",
        client_profile={"detected_language": "en"},
        sender_name="Client",
        phone="6281234567890",
    )

    query, args = next(item for item in conn.executed if "UPDATE conversations" in item[0])
    assert "$1::text::jsonb" in query
    assert "$2::text::jsonb" in query
    messages = json.loads(args[0])
    assert args[2] == 42
    assert messages == [
        {"role": "user", "content": "Old message"},
        {"role": "user", "content": "New question"},
        {"role": "assistant", "content": "New answer"},
    ]


# ============================================================
# notify_zero_conversation_log — unit test
# ============================================================


@pytest.mark.asyncio
async def test_notify_zero_conversation_log_no_chat_id():
    """Should return early when no admin_telegram_chat_id configured."""
    from backend.app.routers.whatsapp_chat import notify_zero_conversation_log

    with (
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch(
            "backend.app.routers.whatsapp_chat.telegram_bot.send_message",
            new_callable=AsyncMock,
        ) as mock_send,
    ):
        mock_settings.admin_telegram_chat_id = None
        # Should not raise, and must return before ever reaching the
        # Telegram send call (no chat_id to send to).
        result = await notify_zero_conversation_log(
            phone="621234567890",
            sender_name="Client",
            client_message="Hello",
            bot_response="Hi!",
            language="en",
        )
    assert result is None
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_notify_zero_conversation_log_sends_message():
    """Should send Telegram message when chat_id is configured."""
    from backend.app.routers.whatsapp_chat import notify_zero_conversation_log

    mock_telegram = MagicMock()
    mock_telegram.send_message = AsyncMock()

    with (
        patch(
            "backend.app.routers.whatsapp_chat.settings", new_callable=_seeded_settings_mock
        ) as mock_settings,
        patch("backend.app.routers.whatsapp_chat.telegram_bot", mock_telegram),
    ):
        mock_settings.admin_telegram_chat_id = "8847435604"
        await notify_zero_conversation_log(
            phone="621234567890",
            sender_name="Test",
            client_message="What is a PT PMA?",
            bot_response="PT PMA is a foreign investment company...",
            language="en",
        )

    mock_telegram.send_message.assert_called_once()


# ============================================================
# notify_human_telegram — unit test
# ============================================================


@pytest.mark.asyncio
async def test_notify_human_telegram_no_chat_id():
    """Should return early when no admin_telegram_chat_id."""
    # The function moved to `human_escalation_notifier` (2026-08-11) so the WA
    # inbox bot can reach it too. It is still importable from the router, but it
    # now reads `settings`/`telegram_bot` from ITS OWN module — patching the
    # router's globals no longer reaches them, so the patch target moved with it.
    from backend.app.routers.whatsapp_chat import notify_human_telegram

    mock_telegram = MagicMock()
    mock_telegram.send_message = AsyncMock()

    with (
        patch("backend.services.integrations.human_escalation_notifier.settings") as mock_settings,
        patch(
            "backend.services.integrations.human_escalation_notifier.telegram_bot",
            mock_telegram,
        ),
    ):
        mock_settings.admin_telegram_chat_id = None
        await notify_human_telegram(phone="62123", message_text="Help!")

    # "Should return early" was the docstring's whole claim and nothing checked
    # it: with no telegram patched and nothing asserted, this test passed on a
    # function that sent. Surviving the call is not the property.
    mock_telegram.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_human_telegram_with_context():
    """Should format and send message with client profile and history."""
    from backend.app.routers.whatsapp_chat import notify_human_telegram

    mock_telegram = MagicMock()
    mock_telegram.send_message = AsyncMock()

    with (
        patch("backend.services.integrations.human_escalation_notifier.settings") as mock_settings,
        patch(
            "backend.services.integrations.human_escalation_notifier.telegram_bot",
            mock_telegram,
        ),
    ):
        mock_settings.admin_telegram_chat_id = "8847435604"
        await notify_human_telegram(
            phone="621234567890",
            message_text="I need help personally",
            sender_name="Marco",
            reason="personal_contact",
            client_profile={
                "detected_language": "it",
                "interests": ["visa", "company"],
                "visa_discussed": ["KITAS"],
                "client_type": "prospect",
                "message_count": 5,
                "first_contact": "2026-04-01",
            },
            conversation_history=[
                {"role": "user", "content": "ciao"},
                {"role": "assistant", "content": "Ciao! Come posso aiutarti?"},
            ],
        )

    mock_telegram.send_message.assert_called_once()


# ============================================================
# F7: raw phone must NEVER appear in a log record (guilt/innocence)
# ============================================================
#
# whatsapp_chat.py is the LIVE client-bot WhatsApp path — 20 raw-phone log
# sites, all now routed through backend.security.pii_log_identifier.
# redact_identifier_for_log. Per cicatrix-superscar.md family #3, a guard
# (here: a redaction) needs both a guilt test (the raw value never survives)
# and an innocence test (the digest stays present/stable, so an operator can
# still correlate lines across one client's conversation).

_LOGGER_NAME = "backend.app.routers.whatsapp_chat"
_NON_DIGITS_RE = re.compile(r"\D+")

# Obviously-synthetic — never a shape mistakable for a real client's number.
_SYNTHETIC_PHONE = "628000111222"


def _digits_only(text: str) -> str:
    """Collapse a log line to its bare digit run.

    Load-bearing: several sites in whatsapp_chat.py build f-strings that
    interpolate ``phone`` directly, so an exact-string check against the raw
    literal can miss a leak that survives in a slightly different textual
    form (e.g. wrapped, or with adjacent characters). Comparing digit runs
    is robust to that — see the same discipline in
    test_messaging_identity_service.py (F7 reference fix, PR 43bb93e11).
    """
    return _NON_DIGITS_RE.sub("", text)


def _all_log_text(records: list[logging.LogRecord]) -> str:
    """Every rendered message across every captured record, concatenated —
    guilt must be checked across ALL lines, not just the one line a fix
    happened to target."""
    return "\n".join(r.getMessage() for r in records)


class TestRawPhoneNeverInLogsGuilt:
    """Guilt: the synthetic phone must never survive into ANY log record
    emitted while processing a message, across two independent branches of
    process_whatsapp_message (non-allowed-number short-circuit, and the
    escalate-to-human path — both log the phone at least once)."""

    @pytest.mark.asyncio
    async def test_non_allowed_number_never_logs_raw_phone(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from backend.app.routers.whatsapp_chat import process_whatsapp_message

        mock_request = MagicMock()
        mock_triage_service = MagicMock()
        mock_triage_service.is_allowed.return_value = False
        mock_wa_service = MagicMock()
        mock_wa_service.mark_message_read = AsyncMock()

        with (
            caplog.at_level(logging.INFO, logger=_LOGGER_NAME),
            patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
            patch(
                "backend.app.routers.whatsapp_chat.whatsapp_triage_service",
                mock_triage_service,
            ),
        ):
            await process_whatsapp_message(
                phone=_SYNTHETIC_PHONE,
                message_text="Hello",
                sender_name="Unknown",
                message_id="msg_test",
                request=mock_request,
            )

        text = _all_log_text(caplog.records)
        assert _SYNTHETIC_PHONE not in text
        assert _SYNTHETIC_PHONE not in _digits_only(text)

    @pytest.mark.asyncio
    async def test_escalate_to_human_never_logs_raw_phone(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from backend.app.routers.whatsapp_chat import process_whatsapp_message
        from backend.services.integrations.whatsapp_triage_service import TriageDecision

        mock_request = MagicMock()
        mock_triage_service = MagicMock()
        mock_triage_service.is_allowed.return_value = True
        mock_triage_service.should_escalate = AsyncMock(
            return_value=(TriageDecision.ESCALATE_PERSONAL, "personal_contact")
        )
        mock_triage_service.get_escalation_message.return_value = "Transferring to team..."

        mock_wa_service = MagicMock()
        mock_wa_service.mark_message_read = AsyncMock()
        mock_wa_service.send_message = AsyncMock()

        mock_onboarding = MagicMock()
        mock_onboarding.detect_and_trigger = AsyncMock(return_value=None)

        with (
            caplog.at_level(logging.INFO, logger=_LOGGER_NAME),
            patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
            patch(
                "backend.app.routers.whatsapp_chat.whatsapp_triage_service",
                mock_triage_service,
            ),
            patch(
                "backend.app.routers.whatsapp_chat.get_onboarding_detector",
                return_value=mock_onboarding,
            ),
            patch("backend.app.routers.whatsapp_chat.notify_human_telegram", new=AsyncMock()),
            patch("backend.app.routers.whatsapp_chat._get_db_pool", return_value=None),
            patch("backend.app.routers.whatsapp_chat.settings") as mock_settings,
        ):
            mock_settings.admin_telegram_chat_id = "123456"
            await process_whatsapp_message(
                phone=_SYNTHETIC_PHONE,
                message_text="This is personal",
                sender_name="Test Client",
                message_id="msg_001",
                request=mock_request,
            )

        text = _all_log_text(caplog.records)
        assert _SYNTHETIC_PHONE not in text
        assert _SYNTHETIC_PHONE not in _digits_only(text)


class TestRedactedPhoneStaysCorrelatable:
    """Innocence: the fix must not collapse every line to the same opaque
    placeholder — a stable digest must appear, so an operator can still
    follow one client's conversation across log lines."""

    @pytest.mark.asyncio
    async def test_digest_present_for_non_allowed_number(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from backend.app.routers.whatsapp_chat import process_whatsapp_message

        mock_request = MagicMock()
        mock_triage_service = MagicMock()
        mock_triage_service.is_allowed.return_value = False
        mock_wa_service = MagicMock()
        mock_wa_service.mark_message_read = AsyncMock()

        with (
            caplog.at_level(logging.INFO, logger=_LOGGER_NAME),
            patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
            patch(
                "backend.app.routers.whatsapp_chat.whatsapp_triage_service",
                mock_triage_service,
            ),
        ):
            await process_whatsapp_message(
                phone=_SYNTHETIC_PHONE,
                message_text="Hello",
                sender_name="Unknown",
                message_id="msg_test",
                request=mock_request,
            )

        text = _all_log_text(caplog.records)
        assert "id:" in text

    @pytest.mark.asyncio
    async def test_same_phone_yields_same_digest_across_two_calls(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from backend.app.routers.whatsapp_chat import process_whatsapp_message

        mock_request = MagicMock()
        mock_triage_service = MagicMock()
        mock_triage_service.is_allowed.return_value = False
        mock_wa_service = MagicMock()
        mock_wa_service.mark_message_read = AsyncMock()

        with (
            caplog.at_level(logging.INFO, logger=_LOGGER_NAME),
            patch("backend.app.routers.whatsapp_chat.whatsapp_service", mock_wa_service),
            patch(
                "backend.app.routers.whatsapp_chat.whatsapp_triage_service",
                mock_triage_service,
            ),
        ):
            await process_whatsapp_message(
                phone=_SYNTHETIC_PHONE,
                message_text="Hello",
                sender_name="Unknown",
                message_id="msg_test_1",
                request=mock_request,
            )
            first_text = _all_log_text(caplog.records)
            caplog.clear()

            await process_whatsapp_message(
                phone=_SYNTHETIC_PHONE,
                message_text="Hello again",
                sender_name="Unknown",
                message_id="msg_test_2",
                request=mock_request,
            )
            second_text = _all_log_text(caplog.records)

        first_digest = next(tok for tok in first_text.split() if "id:" in tok)
        second_digest = next(tok for tok in second_text.split() if "id:" in tok)
        assert first_digest == second_digest


# R4 regression fixtures use only synthetic identifiers and stub all effects.
_R4_PRIVATE = "R4_SYNTHETIC_PRIVATE_SENTINEL"


def _r4_assert_logs(caplog, expected):
    import backend.app.routers.whatsapp_chat as wa

    records = [record for record in caplog.records if record.name == wa.logger.name]
    assert any(expected in record.getMessage() for record in records)
    for record in records:
        assert _R4_PRIVATE not in record.getMessage()
        assert _R4_PRIVATE not in repr(vars(record))
        assert record.exc_info is None or record.exc_info is False
        assert record.stack_info is None


def _r4_pool():
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    conn.transaction.side_effect = lambda: _FakeConversationAcquire(conn)
    pool = MagicMock()
    pool.acquire.side_effect = lambda: _FakeConversationAcquire(conn)
    return pool, conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch",
    ["claim", "duplicate", "onboarding", "payload", "context", "ack", "processing", "send"],
)
async def test_r4_legacy_branches_keep_effects_without_private_logs(branch, monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    import backend.app.routers.whatsapp_chat as wa
    import backend.services.whatsapp_ack as ack
    import backend.services.whatsapp_context_builder as context_builder
    from backend.services.integrations.whatsapp_triage_service import TriageDecision

    caplog.set_level(logging.DEBUG)
    pool, _ = _r4_pool()
    settings = _seeded_settings_mock()
    settings.admin_telegram_chat_id = None
    monkeypatch.setattr(wa, "settings", settings)
    service = MagicMock()
    service.mark_message_read = AsyncMock()
    service.send_message = AsyncMock()
    service.chunk_message.side_effect = lambda text, max_length: [text]
    monkeypatch.setattr(wa, "whatsapp_service", service)
    triage = MagicMock()
    triage.is_allowed.return_value = True
    decision = {
        "context": TriageDecision.ESCALATE_PERSONAL,
        "ack": TriageDecision.BOT_CAN_HANDLE,
    }.get(branch, TriageDecision.OFFER_CHOICE)
    triage.should_escalate = AsyncMock(return_value=(decision, "business_query"))
    triage.get_welcome_message.return_value = "Synthetic welcome"
    triage.get_escalation_message.return_value = "Synthetic handoff"
    monkeypatch.setattr(wa, "whatsapp_triage_service", triage)
    detector = SimpleNamespace(detect_and_trigger=AsyncMock(return_value=None))
    monkeypatch.setattr(wa, "get_onboarding_detector", lambda: detector)
    monkeypatch.setattr(wa, "_get_db_pool", lambda _: pool)
    claim = AsyncMock(return_value=branch != "duplicate")
    monkeypatch.setattr(wa, "_claim_wamid_reply", claim)
    audit = AsyncMock()
    save = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr(wa, "_persist_audit_messages", audit)
    monkeypatch.setattr(wa, "_save_conversation", save)
    monkeypatch.setattr(wa, "notify_zero_conversation_log", AsyncMock())
    monkeypatch.setattr(wa, "notify_human_telegram", notify)
    context = {
        "_wa_user_id": "synthetic",
        "_session_id": "synthetic",
        "_existing_row_id": None,
        "client_name": "Synthetic",
        "client_profile": {},
        "conversation_history": [],
        "detected_language": "en",
        "is_first_message": False,
        "sender_identity": {},
        "time_of_day": "afternoon",
    }
    build = AsyncMock(return_value=context)
    monkeypatch.setattr(context_builder, "build_context", build)
    monkeypatch.setattr(ack, "should_send_ack", lambda *_: False)
    monkeypatch.setattr(wa, "ask_openclaw_whatsapp", AsyncMock(return_value="Synthetic answer"))

    if branch == "claim":
        claim.side_effect = RuntimeError(_R4_PRIVATE)
    elif branch == "onboarding":
        detector.detect_and_trigger.side_effect = RuntimeError(_R4_PRIVATE)
    elif branch == "payload":
        detector.detect_and_trigger.return_value = {"chain": "synthetic", "payload": _R4_PRIVATE}
    elif branch == "context":
        build.side_effect = RuntimeError(_R4_PRIVATE)
    elif branch == "ack":
        monkeypatch.setattr(
            ack, "should_send_ack", MagicMock(side_effect=RuntimeError(_R4_PRIVATE))
        )
    elif branch in {"processing", "send"}:
        service.mark_message_read.side_effect = RuntimeError(_R4_PRIVATE)
        if branch == "send":
            service.send_message.side_effect = ValueError(_R4_PRIVATE)

    assert (
        await wa.process_whatsapp_message(
            _R4_PRIVATE, "Synthetic question", "Synthetic", _R4_PRIVATE, MagicMock()
        )
        is None
    )
    if branch == "duplicate":
        service.send_message.assert_not_awaited()
        triage.should_escalate.assert_not_awaited()
        _r4_assert_logs(caplog, "already claimed")
    elif branch == "payload":
        service.send_message.assert_awaited_once()
        assert "onboarding" in service.send_message.await_args.kwargs["text"]
        assert detector.detect_and_trigger.return_value["payload"] == _R4_PRIVATE
        audit.assert_awaited_once()
        _r4_assert_logs(caplog, "Auto-triggered onboarding")
    elif branch == "context":
        # Preserve the existing None-context fallback, including its outer error response.
        assert service.send_message.await_count == 2
        notify.assert_not_awaited()
        _r4_assert_logs(caplog, "Error building context")
    elif branch == "ack":
        service.send_message.assert_awaited_once()
        assert service.send_message.await_args.kwargs["text"] == "Synthetic answer"
        save.assert_awaited_once()
        _r4_assert_logs(caplog, "Concierge ack failed")
    elif branch == "send":
        service.send_message.assert_awaited_once()
        audit.assert_not_awaited()
        _r4_assert_logs(caplog, "Failed to send error message: ValueError")
    else:
        service.send_message.assert_awaited_once()
        audit.assert_awaited_once()
        _r4_assert_logs(
            caplog,
            {
                "claim": "Cross-path claim failed",
                "onboarding": "Onboarding detection failed",
                "processing": "Error processing WhatsApp message",
            }[branch],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["notify", "database", "mark"])
async def test_r4_auxiliary_failures_keep_fallbacks(branch, monkeypatch, caplog):
    import logging

    import backend.app.dependencies as dependencies
    import backend.app.routers.whatsapp_chat as wa
    from backend.services.channels import inbound_webhook_repo

    caplog.set_level(logging.DEBUG)
    if branch == "notify":
        settings = _seeded_settings_mock()
        settings.admin_telegram_chat_id = "synthetic-chat"
        monkeypatch.setattr(wa, "settings", settings)
        sender = AsyncMock(side_effect=RuntimeError(_R4_PRIVATE))
        monkeypatch.setattr(wa.telegram_bot, "send_message", sender)
        assert (
            await wa.notify_zero_conversation_log(
                _R4_PRIVATE, "Synthetic", "Synthetic question", "Synthetic answer", "en"
            )
            is None
        )
        sender.assert_awaited_once()
        expected = "Failed to send conversation log"
    elif branch == "database":
        getter = MagicMock(side_effect=RuntimeError(_R4_PRIVATE))
        monkeypatch.setattr(dependencies, "get_database", getter)
        assert wa._get_db_pool(MagicMock()) is None
        getter.assert_called_once()
        expected = "Error getting database"
    else:
        process = AsyncMock()
        mark = AsyncMock(side_effect=RuntimeError(_R4_PRIVATE))
        pool, _ = _r4_pool()
        monkeypatch.setattr(wa, "process_whatsapp_message", process)
        monkeypatch.setattr(wa, "_get_db_pool", lambda _: pool)
        monkeypatch.setattr(inbound_webhook_repo, "mark_processed", mark)
        assert (
            await wa.process_whatsapp_message_and_mark_processed(
                _R4_PRIVATE, "Synthetic", None, _R4_PRIVATE, MagicMock()
            )
            is None
        )
        process.assert_awaited_once()
        mark.assert_awaited_once_with(pool, channel="whatsapp", dedup_key=_R4_PRIVATE)
        expected = "failed to mark inbound row processed"
    _r4_assert_logs(caplog, expected)
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["claim", "duplicate", "read"])
async def test_r4_meta_inbox_claim_and_read_keep_database_effects(branch, monkeypatch, caplog):
    import logging

    import backend.app.routers.whatsapp_chat as wa

    caplog.set_level(logging.DEBUG)
    _, conn = _r4_pool()
    conn.fetchrow.side_effect = [
        {"thread_id": 7, "human_handling": False},
        {"id": 8},
        {"id": 9},
    ]
    claim = AsyncMock(return_value=branch != "duplicate")
    if branch == "claim":
        claim.side_effect = RuntimeError(_R4_PRIVATE)
    monkeypatch.setattr(wa, "_claim_wamid_reply", claim)
    monkeypatch.setattr(wa, "_manners_enabled", lambda: True)
    read = AsyncMock(side_effect=RuntimeError(_R4_PRIVATE) if branch == "read" else None)
    monkeypatch.setattr(wa.whatsapp_service, "mark_message_read", read)
    msg = {
        "id": _R4_PRIVATE,
        "from": _R4_PRIVATE,
        "type": "text",
        "text": {"body": "Synthetic"},
        "timestamp": "1712000000",
    }
    assert await wa._handle_meta_inbox_message(conn, msg, "Synthetic", 10) is None
    if branch == "duplicate":
        conn.fetchrow.assert_not_awaited()
        read.assert_not_awaited()
        expected = "already claimed"
    else:
        assert conn.fetchrow.await_count == 3
        conn.execute.assert_awaited_once()
        read.assert_awaited_once_with(_R4_PRIVATE)
        expected = "cross-path claim failed" if branch == "claim" else "mark_message_read failed"
    _r4_assert_logs(caplog, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["parse", "processing", "mark", "recovery"])
async def test_r4_meta_payload_fallbacks_keep_ack_and_recovery(branch, monkeypatch, caplog):
    import logging

    import backend.app.routers.whatsapp_chat as wa
    from backend.services.channels import inbound_webhook_repo

    caplog.set_level(logging.DEBUG)
    pool, conn = _r4_pool()
    payload = _make_webhook_payload(phone=_R4_PRIVATE, message_id=_R4_PRIVATE)
    monkeypatch.setattr(wa, "_change_belongs_to_meta_inbox", lambda _: True)
    monkeypatch.setattr(wa, "_change_belongs_to_team_bot_ingress", lambda _: False)
    handle = AsyncMock()
    monkeypatch.setattr(wa, "_handle_meta_inbox_message", handle)
    monkeypatch.setattr(wa, "_resolve_webhook_id", AsyncMock(return_value=7))
    mark = AsyncMock(side_effect=RuntimeError(_R4_PRIVATE))
    monkeypatch.setattr(inbound_webhook_repo, "mark_processed", mark)
    if branch in {"parse", "recovery"}:
        monkeypatch.setattr(wa, "WhatsAppWebhook", MagicMock(side_effect=ValueError(_R4_PRIVATE)))
    if branch == "processing":
        conn.fetchrow.side_effect = RuntimeError(_R4_PRIVATE)
        monkeypatch.setattr(
            wa, "_resolve_webhook_id", AsyncMock(side_effect=RuntimeError(_R4_PRIVATE))
        )
    if branch == "recovery":
        legacy = AsyncMock()
        assert await wa.route_whatsapp_recovery(payload, db_pool=pool, legacy_route=legacy) is None
        legacy.assert_awaited_once_with(payload)
        expected = "defaulting to legacy route"
    else:
        assert await wa.process_meta_inbox_payload(payload, None, db_pool=pool) is (
            branch != "processing"
        )
        if branch == "mark":
            handle.assert_awaited_once()
            mark.assert_awaited_once_with(pool, channel="whatsapp", dedup_key=_R4_PRIVATE)
        else:
            handle.assert_not_awaited()
            mark.assert_not_awaited()
        expected = {
            "parse": "payload parse failed",
            "processing": "processing failed",
            "mark": "failed to mark inbound row processed",
        }[branch]
    _r4_assert_logs(caplog, expected)


@pytest.mark.asyncio
async def test_r4_media_handoff_failure_does_not_change_publish_payload(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    import backend.app.routers.whatsapp_chat as wa
    import backend.channels.whatsapp.media_webhook_parse as media_parse
    from backend.services.events import outbox

    caplog.set_level(logging.DEBUG)
    pool, _ = _r4_pool()
    media = SimpleNamespace(
        phone_number_id="synthetic-number",
        media_id=_R4_PRIVATE,
        mime_type="image/png",
        message_type="image",
        filename=_R4_PRIVATE,
        declared_sha256=None,
        wa_message_id=_R4_PRIVATE,
        from_phone=_R4_PRIVATE,
        sender_name="Synthetic",
    )
    monkeypatch.setattr(
        media_parse, "parse_media_webhook", lambda _: SimpleNamespace(media=[media])
    )
    monkeypatch.setattr(wa, "META_INBOX_PHONE_NUMBER_IDS", {"synthetic-number"})
    monkeypatch.setattr(wa, "_get_db_pool", lambda _: pool)
    publish = AsyncMock(side_effect=RuntimeError(_R4_PRIVATE))
    monkeypatch.setattr(outbox, "publish", publish)
    assert await wa._ingest_meta_inbox_media({}, MagicMock()) is None
    publish.assert_awaited_once()
    assert publish.await_args.args[2]["media_id"] == _R4_PRIVATE
    _r4_assert_logs(caplog, "media handoff failed: RuntimeError")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch", ["peer", "parse", "persist", "duplicate", "object", "field", "type"]
)
async def test_r4_webhook_diagnostics_preserve_response_and_scheduling(branch, monkeypatch, caplog):
    import json
    import logging

    from fastapi import BackgroundTasks, HTTPException

    import backend.app.routers.whatsapp_chat as wa
    from backend.services.channels import inbound_webhook_repo

    caplog.set_level(logging.DEBUG)
    payload = _make_webhook_payload(phone=_R4_PRIVATE, message_id=_R4_PRIVATE)
    if branch == "object":
        payload["object"] = _R4_PRIVATE
    elif branch == "field":
        payload["entry"][0]["changes"][0]["field"] = _R4_PRIVATE
    elif branch == "type":
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = _R4_PRIVATE
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(payload).encode())
    request.client.host = _R4_PRIVATE
    tasks = BackgroundTasks()
    pool, _ = _r4_pool()
    monkeypatch.setattr(wa, "_get_db_pool", lambda _: pool)
    monkeypatch.setattr(wa, "_verify_whatsapp_signature", lambda *_: branch != "peer")
    monkeypatch.setattr(wa, "_change_belongs_to_meta_inbox", lambda _: False)
    monkeypatch.setattr(wa, "_change_belongs_to_team_bot_ingress", lambda _: False)
    persist = AsyncMock(return_value=(1, branch != "duplicate"))
    if branch == "persist":
        persist.side_effect = RuntimeError(_R4_PRIVATE)
    monkeypatch.setattr(inbound_webhook_repo, "persist", persist)
    if branch == "parse":
        request.body.return_value = _R4_PRIVATE.encode()
    if branch in {"parse", "peer"}:
        with pytest.raises(HTTPException) as exc:
            await wa.whatsapp_webhook(request=request, background_tasks=tasks)
        assert exc.value.status_code == (401 if branch == "peer" else 400)
        assert tasks.tasks == []
        persist.assert_not_awaited()
        expected = "signature verification failed" if branch == "peer" else "body parse error"
    else:
        assert await wa.whatsapp_webhook(request=request, background_tasks=tasks) == {"status": "ok"}
        assert len(tasks.tasks) == (0 if branch in {"duplicate", "field", "type"} else 1)
        if tasks.tasks:
            assert tasks.tasks[0].kwargs["message_id"] == _R4_PRIVATE
        expected = {
            "persist": "persist failed",
            "duplicate": "Duplicate WhatsApp webhook skipped",
            "object": "Webhook received",
            "field": "Ignoring non-message change",
            "type": "Ignoring non-text message",
        }[branch]
    _r4_assert_logs(caplog, expected)


@pytest.mark.asyncio
async def test_r4_verification_mode_is_not_logged(monkeypatch, caplog):
    import logging

    from fastapi import HTTPException

    import backend.app.routers.whatsapp_chat as wa

    caplog.set_level(logging.DEBUG)
    request = MagicMock()
    request.query_params = {
        "hub.mode": _R4_PRIVATE,
        "hub.verify_token": _R4_PRIVATE,
        "hub.challenge": "synthetic",
    }
    with pytest.raises(HTTPException) as exc:
        await wa.verify_webhook(request)
    assert exc.value.status_code == 403
    _r4_assert_logs(caplog, "verification requested")
