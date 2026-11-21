"""
Unit tests for whatsapp_chat router.
Coverage target: GET /webhook/whatsapp (verification), POST /webhook/whatsapp (messages),
GET /webhook/whatsapp/status,
X-Hub-Signature-256 HMAC verification.
"""

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.config import settings as real_settings
from backend.app.routers.whatsapp_chat import router

# ============================================================
# FIXTURES
# ============================================================


@pytest.fixture
def app():
    test_app = FastAPI()
    test_app.include_router(router)
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
# Fakes shared by the webhook diagnostics tests below
# ============================================================


class _FakeConversationAcquire:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, *_args: Any) -> None:
        return None


# ============================================================
# notify_human_telegram — unit test
# ============================================================


@pytest.mark.asyncio
async def test_notify_human_telegram_no_chat_id():
    """Should return early when no admin_telegram_chat_id."""
    # `notify_human_telegram` lives in `human_escalation_notifier` so the WA
    # inbox bot can reach it. It reads `settings`/`telegram_bot` from ITS OWN
    # module, so those are the patch targets.
    from backend.services.integrations.human_escalation_notifier import notify_human_telegram

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
    from backend.services.integrations.human_escalation_notifier import notify_human_telegram

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
async def test_r4_database_lookup_failure_keeps_fallback(monkeypatch, caplog):
    import logging

    import backend.app.dependencies as dependencies
    import backend.app.routers.whatsapp_chat as wa

    caplog.set_level(logging.DEBUG)
    getter = MagicMock(side_effect=RuntimeError(_R4_PRIVATE))
    monkeypatch.setattr(dependencies, "get_database", getter)
    assert wa._get_db_pool(MagicMock()) is None
    getter.assert_called_once()
    expected = "Error getting database"
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
        expected = "could not be classified"
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
        # A persisted row for a number with no handler is closed with a reason;
        # nothing persisted (persist failed) or no message change means no task.
        assert len(tasks.tasks) == (0 if branch in {"persist", "field"} else 1)
        if tasks.tasks:
            assert tasks.tasks[0].func is wa._close_retired_legacy_rows
            assert tasks.tasks[0].args[1] == [_R4_PRIVATE]
        expected = {
            "persist": "persist failed",
            "duplicate": "closing with reason",
            "object": "Webhook received",
            "field": "Ignoring non-message change",
            "type": "closing with reason",
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
