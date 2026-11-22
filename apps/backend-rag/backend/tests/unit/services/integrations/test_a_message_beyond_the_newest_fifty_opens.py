"""A message older than the newest fifty of its folder can be opened.

``get_email`` took a message's metadata from the first page of the folder's list (50 entries)
and answered "not found" for any message beyond it, while the mailbox lists older messages
too. It now asks Zoho for that one message: ``GET /folders/{folderId}/messages/{messageId}/
details`` ("Get meta data of an email" in the Zoho Mail API reference). The answer below has
the keys that reference prints, with invented values.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.integrations.zoho_email_service import ZohoEmailService

FOLDER = "9000000000001"
OLD_MESSAGE = "1700000000051"
DETAILS_PATH = f"/folders/{FOLDER}/messages/{OLD_MESSAGE}/details"
CONTENT_PATH = f"/folders/{FOLDER}/messages/{OLD_MESSAGE}/content"

# The keys of the reference's sample answer, in its order.
DETAILS = {
    "summary": "An older message",
    "sentDateInGMT": "1700000000000",
    "calendarType": 0,
    "subject": "Zz older subject",
    "messageId": OLD_MESSAGE,
    "flagid": "flag_not_set",
    "status2": "0",
    "priority": "3",
    "hasInline": "false",
    "toAddress": "team@example.test",
    "folderId": FOLDER,
    "ccAddress": "Not Provided",
    "hasAttachment": "0",
    "size": "1024",
    "sender": "Zz Sender",
    "receivedTime": "1700000000500",
    "fromAddress": "sender@example.test",
    "status": "1",
}
NEWEST_FIFTY = {
    "data": [{"messageId": str(1800000000000 + index), "folderId": FOLDER} for index in range(50)]
}
BODY = {"data": {"content": "<p>Zz body</p>"}}


def _service(*answers: Any) -> tuple[ZohoEmailService, AsyncMock]:
    service = ZohoEmailService(MagicMock())
    service._request = AsyncMock(side_effect=list(answers))
    service.mark_read = AsyncMock()
    return service, service._request


def _asked(request: AsyncMock) -> list[str]:
    return [call.args[2] for call in request.await_args_list]


@pytest.mark.asyncio
async def test_a_message_beyond_the_first_page_is_opened_by_its_details() -> None:
    service, request = _service(NEWEST_FIFTY, {"data": DETAILS}, BODY)

    opened = await service.get_email("user1", OLD_MESSAGE, folder_id=FOLDER)

    assert _asked(request) == ["/messages/view", DETAILS_PATH, CONTENT_PATH]
    assert {key: opened[key] for key in ("message_id", "folder_id", "subject", "snippet")} == {
        "message_id": "1700000000051",
        "folder_id": "9000000000001",
        "subject": "Zz older subject",
        "snippet": "An older message",
    }
    assert opened["from"] == {"address": "sender@example.test", "name": "Zz Sender"}
    assert opened["html_content"] == "<p>Zz body</p>"
    assert opened["date"] == "1700000000500"
    assert opened["thread_id"] is None  # the details answer carries no thread
    service.mark_read.assert_awaited_once_with("user1", [OLD_MESSAGE], is_read=True)


@pytest.mark.asyncio
async def test_a_message_of_the_first_page_is_opened_as_before() -> None:
    listed = {"data": [{"messageId": OLD_MESSAGE, "folderId": FOLDER, "threadId": "77"}]}
    service, request = _service(listed, BODY)

    opened = await service.get_email("user1", OLD_MESSAGE, folder_id=FOLDER)

    assert _asked(request) == ["/messages/view", CONTENT_PATH]
    assert opened["thread_id"] == "77"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        ValueError("API error: URL_RULE_NOT_CONFIGURED"),
        {"data": []},
        {"data": {**DETAILS, "messageId": "1700000000099"}},
        {"status": {"code": 200}},
    ],
    ids=["Zoho refuses", "a list", "another message", "no data"],
)
async def test_a_message_zoho_does_not_answer_with_is_not_found_and_nothing_is_read(
    answer: Any,
) -> None:
    service, request = _service(NEWEST_FIFTY, answer)

    with pytest.raises(ValueError, match=f"Email {OLD_MESSAGE} not found in folder {FOLDER}"):
        await service.get_email("user1", OLD_MESSAGE, folder_id=FOLDER)

    assert _asked(request) == ["/messages/view", DETAILS_PATH]
    service.mark_read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("folder", "message"),
    [
        ("inbox", OLD_MESSAGE),
        (FOLDER, "../../folders/1/messages/2"),
        (FOLDER, "17?x=1"),
        (FOLDER, ""),
    ],
    ids=["a named folder", "a path", "a query", "empty"],
)
async def test_an_id_that_is_not_a_number_is_never_put_in_the_address(
    folder: str, message: str
) -> None:
    service, request = _service(NEWEST_FIFTY)

    with pytest.raises(ValueError, match="not found in folder"):
        await service.get_email("user1", message, folder_id=folder)

    assert _asked(request) == ["/messages/view"]
