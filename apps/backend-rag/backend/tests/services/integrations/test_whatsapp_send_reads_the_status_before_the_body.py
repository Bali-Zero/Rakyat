from __future__ import annotations

import json
from typing import Any

import pytest

from backend.services.integrations.whatsapp_service import WhatsAppService


class FakeResponse:
    def __init__(
        self,
        payload: object | None = None,
        *,
        status_code: int = 200,
        body: bytes = b"",
        json_error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = body
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.is_closed = False

    async def post(self, _url: str, **_kwargs: Any) -> FakeResponse:
        return self.response


def make_service(response: FakeResponse) -> WhatsAppService:
    service = WhatsAppService.__new__(WhatsAppService)
    service._token = "tok-a"  # pragma: allowlist secret
    service._phone_number_id = "phone-id"
    service._client = FakeClient(response)

    async def fake_get_client() -> FakeClient:
        return service._client

    service._get_client = fake_get_client  # type: ignore[method-assign]
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"not-json"])
async def test_send_message_accepts_success_with_unparseable_body(body: bytes) -> None:
    service = make_service(
        FakeResponse(body=body, json_error=json.JSONDecodeError("bad", "", 0)),
    )

    result = await service.send_message("620000000001", "Hello")

    assert result == {}


@pytest.mark.asyncio
async def test_send_message_accepts_success_with_json_array_body() -> None:
    service = make_service(FakeResponse([], body=b"[]"))

    assert await service.send_message("620000000005", "Hello") == {}


@pytest.mark.asyncio
async def test_send_message_uses_status_when_error_body_is_html() -> None:
    service = make_service(
        FakeResponse(
            status_code=500,
            body=b"<html>failure</html>",
            json_error=json.JSONDecodeError("bad", "", 0),
        ),
    )

    with pytest.raises(ValueError, match=r"WhatsApp API error \[500\]: Unknown error"):
        await service.send_message("620000000002", "Hello")


@pytest.mark.asyncio
async def test_send_message_returns_success_json_object_unchanged() -> None:
    payload = {"messages": [{"id": "wamid.synthetic"}]}
    service = make_service(FakeResponse(payload, body=b'{"messages":[]}'))

    assert await service.send_message("620000000003", "Hello") == payload


@pytest.mark.asyncio
async def test_send_message_preserves_json_error_details() -> None:
    service = make_service(
        FakeResponse(
            {"error": {"message": "Synthetic bad request", "code": 131030}},
            status_code=400,
            body=b'{"error":{}}',
        ),
    )

    with pytest.raises(
        ValueError,
        match=r"WhatsApp API error \[131030\]: Synthetic bad request",
    ):
        await service.send_message("620000000004", "Hello")



@pytest.mark.asyncio
async def test_send_message_warns_once_when_the_accepted_body_is_unreadable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = make_service(
        FakeResponse(body=b"not-json", json_error=json.JSONDecodeError("bad", "", 0)),
    )

    with caplog.at_level("WARNING"):
        assert await service.send_message("620000000006", "Hello") == {}

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "status=200" in message
    assert "body_length=8" in message
    assert "620000000006" not in message


@pytest.mark.asyncio
async def test_send_message_reads_an_error_that_is_not_an_object() -> None:
    service = make_service(
        FakeResponse({"error": "refused"}, status_code=400, body=b"{}"),
    )

    with pytest.raises(ValueError, match=r"WhatsApp API error \[400\]: Unknown error"):
        await service.send_message("620000000007", "Hello")
