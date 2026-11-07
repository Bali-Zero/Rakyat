"""PII log hygiene for the inbound log line of the WhatsApp, Telegram and web adapters.

Class B of the 2026-11-04 PII-log window: every inbound message used to log, at INFO, the
sender's display name (WhatsApp), first name and username (Telegram) and the first 50
characters of the message text; the web line also printed the first 20 characters of a
``user_id`` that may be an email. The line now carries the redacted identifier and the
text length only. The web session id is client-controlled (only a ``web_`` prefix is
added), so it is redacted too, on the inbound line and on the three outbound lines that
log it as ``channel_id``. GUILT: no sentinel name, username, email or text fragment
reaches a log record. INNOCENCE: the lines are still emitted with the redacted identifier
and the length, the parsed message still carries the full text and session id, and the
SSE stream still carries the real correlation id.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest

from backend.channels.base import ChannelResponse
from backend.channels.telegram.adapter import TelegramChannelAdapter
from backend.channels.web.adapter import WebChannelAdapter
from backend.channels.whatsapp.adapter import WhatsAppChannelAdapter
from backend.security.pii_log_identifier import redact_identifier_for_log

_NAME = "Sentinelname Probe"
_USERNAME = "sentinel_handle_probe"
_EMAIL = "sentinel.user@example.com"
_SESSION = "sentinel.session@example.com"
_WEB_SESSION = "web_session_1730700000"
_TEXT = "SENTINELTEXT the message body must never reach a log line"
_PHONE = "6284000220000"
_CHAT_ID = 123456789


def _messages(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


def _assert_no_pii(lines: list[str], *needles: str) -> None:
    blob = "\n".join(lines)
    for needle in needles:
        assert needle not in blob
    assert "SENTINELTEXT" not in blob
    assert "@example.com" not in blob


def _whatsapp_event() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": _PHONE,
                                    "id": "wamid.probe",
                                    "timestamp": "1711900000",
                                    "type": "text",
                                    "text": {"body": _TEXT},
                                }
                            ],
                            "contacts": [{"profile": {"name": _NAME}}],
                        }
                    }
                ]
            }
        ],
    }


def _telegram_event() -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 7,
            "from": {"id": _CHAT_ID, "first_name": _NAME, "username": _USERNAME},
            "chat": {"id": _CHAT_ID, "type": "private"},
            "date": 1234567890,
            "text": _TEXT,
        },
    }


@pytest.mark.asyncio
async def test_whatsapp_inbound_line_carries_no_name_and_no_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = WhatsAppChannelAdapter(
        {"access_token": "t", "phone_number_id": "111222333444", "business_account_id": "b"}
    )
    with caplog.at_level(logging.DEBUG, logger="backend.channels.whatsapp.adapter"):
        message = await adapter.receive_message(_whatsapp_event())

    lines = _messages(caplog, "backend.channels.whatsapp.adapter")
    _assert_no_pii(lines, _NAME, _PHONE)
    inbound = [line for line in lines if line.startswith("📨 WhatsApp message:")]
    assert inbound == [
        f"📨 WhatsApp message: phone={redact_identifier_for_log(_PHONE)}, text_chars={len(_TEXT)}"
    ]
    assert message.text == _TEXT


@pytest.mark.asyncio
async def test_telegram_inbound_line_carries_no_name_username_or_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = TelegramChannelAdapter({"bot_token": "t", "max_message_length": 4096})
    with caplog.at_level(logging.DEBUG, logger="backend.channels.telegram.adapter"):
        message = await adapter.receive_message(_telegram_event())

    lines = _messages(caplog, "backend.channels.telegram.adapter")
    _assert_no_pii(lines, _NAME, _USERNAME, "Sentinelname", str(_CHAT_ID))
    inbound = [line for line in lines if line.startswith("📨 Telegram message received:")]
    assert inbound == [
        "📨 Telegram message received: "
        f"chat_id={redact_identifier_for_log(_CHAT_ID)}, text_chars={len(_TEXT)}"
    ]
    assert message.text == _TEXT


@pytest.mark.asyncio
async def test_web_inbound_line_redacts_email_shaped_user_and_session_and_drops_the_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = WebChannelAdapter({"max_message_length": 100000})
    event = {"user_id": _EMAIL, "session_id": _SESSION, "query": _TEXT}
    with caplog.at_level(logging.DEBUG, logger="backend.channels.web.adapter"):
        message = await adapter.receive_message(event)

    lines = _messages(caplog, "backend.channels.web.adapter")
    _assert_no_pii(lines, _EMAIL, _EMAIL[:20], "sentinel.user", _SESSION, "sentinel.session")
    inbound = [line for line in lines if line.startswith("🌐 Web message received:")]
    assert inbound == [
        "🌐 Web message received: "
        f"user={redact_identifier_for_log(_EMAIL)}, "
        f"session={redact_identifier_for_log(f'web_{_SESSION}')}, "
        f"text_chars={len(_TEXT)}"
    ]
    assert message.text == _TEXT
    assert message.session_id == f"web_{_SESSION}"


@pytest.mark.asyncio
async def test_web_inbound_line_keeps_the_anonymous_placeholder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = WebChannelAdapter({"max_message_length": 100000})
    with caplog.at_level(logging.DEBUG, logger="backend.channels.web.adapter"):
        message = await adapter.receive_message({"session_id": _WEB_SESSION, "query": "hi"})

    inbound = [
        line
        for line in _messages(caplog, "backend.channels.web.adapter")
        if line.startswith("🌐 Web message received:")
    ]
    assert inbound == [
        "🌐 Web message received: user=anonymous, "
        f"session={redact_identifier_for_log(_WEB_SESSION)}, text_chars=2"
    ]
    assert _WEB_SESSION not in "\n".join(inbound)
    assert message.session_id == _WEB_SESSION


@pytest.mark.asyncio
async def test_web_outbound_lines_redact_the_channel_id_and_the_stream_keeps_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = WebChannelAdapter({"max_message_length": 100000})
    channel_id = f"web_{_SESSION}"

    async def _tokens() -> AsyncIterator[ChannelResponse]:
        yield ChannelResponse(text="tok", metadata={})

    with caplog.at_level(logging.DEBUG, logger="backend.channels.web.adapter"):
        await adapter.send_response(channel_id, ChannelResponse(text="ok", metadata={}))
        await adapter.send_status_update(channel_id, "processing")
        events = [event async for event in adapter.stream_response(channel_id, _tokens())]

    lines = _messages(caplog, "backend.channels.web.adapter")
    _assert_no_pii(lines, _SESSION, "sentinel.session")
    digest = redact_identifier_for_log(channel_id)
    assert len(lines) == 3
    assert all(digest in line for line in lines)
    assert sum(channel_id in event for event in events) == 2
