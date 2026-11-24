"""Regression tests for VisaExpiryTeamNotifier email rendering and counts."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.compliance.visa_expiry_team_notifier import VisaExpiryTeamNotifier


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "client_id": 900001,
        "client_name": "Person One",
        "client_email": "person.one@example.test",
        "client_phone": "+62000000001",
        "nationality": "Example",
        "assigned_to": "leader.one@example.test",
        "document_type": "visa",
        "expiry_date": date(2026, 5, 1),
        "days_until_expiry": 21,
        "current_visa_type": "B211",
        "current_visa_sponsor": "PT Contoh Satu",
        "passport_expiry": date(2027, 1, 1),
    }
    row.update(overrides)
    return row


def _http_client(post: AsyncMock) -> MagicMock:
    client = MagicMock()
    client.post = post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_escapes_database_values_in_team_and_summary_emails() -> None:
    post = AsyncMock(return_value=MagicMock())
    post.return_value.raise_for_status = MagicMock()
    notifier = VisaExpiryTeamNotifier(MagicMock())
    notifier._get_expiring_documents = AsyncMock(
        return_value=[_row(client_name="<b>Person One</b> & Co")]
    )

    with patch(
        "backend.services.compliance.visa_expiry_team_notifier.httpx.AsyncClient",
        return_value=_http_client(post),
    ):
        await notifier.check_and_notify()

    bodies = [call.kwargs["json"]["body"] for call in post.await_args_list]
    assert len(bodies) == 2
    for body in bodies:
        assert "&lt;b&gt;Person One&lt;/b&gt; &amp; Co" in body
        assert "<b>Person One</b> & Co" not in body


@pytest.mark.asyncio
async def test_counts_failed_email_attempts() -> None:
    post = AsyncMock(side_effect=RuntimeError("synthetic provider outage"))
    notifier = VisaExpiryTeamNotifier(MagicMock())
    notifier._get_expiring_documents = AsyncMock(
        return_value=[
            _row(),
            _row(client_id=900002, assigned_to="leader.two@example.test"),
        ]
    )

    with patch(
        "backend.services.compliance.visa_expiry_team_notifier.httpx.AsyncClient",
        return_value=_http_client(post),
    ):
        result = await notifier.check_and_notify()

    assert result["emails_sent"] == 0
    assert result["emails_failed"] == 3


@pytest.mark.asyncio
async def test_plain_values_keep_existing_html_and_success_counts() -> None:
    post = AsyncMock(return_value=MagicMock())
    post.return_value.raise_for_status = MagicMock()
    notifier = VisaExpiryTeamNotifier(MagicMock())
    notifier._get_expiring_documents = AsyncMock(
        return_value=[
            _row(),
            _row(client_id=900002, assigned_to="leader.two@example.test"),
        ]
    )

    with patch(
        "backend.services.compliance.visa_expiry_team_notifier.httpx.AsyncClient",
        return_value=_http_client(post),
    ):
        result = await notifier.check_and_notify()

    team_body = post.await_args_list[0].kwargs["json"]["body"]
    assert "<td style='padding:8px;'>Person One</td>" in team_body
    assert result["total_alerts"] == 2
    assert result["emails_sent"] == 3
    assert result["emails_failed"] == 0
