"""Re-sending a portal invitation sends the invitation e-mail, on a real PostgreSQL.

Reuses the throwaway-schema fixture of the stored-address invite proof; only the mail sender is
mocked.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

from backend.app.routers.portal_invite import resend_invitation
from backend.services.portal import InviteService
from backend.tests.routers.test_portal_invite_stored_email_postgres import (  # noqa: F401
    STAFF,
    STORED,
    invitation_rows,
    pool,
    seed_client,
)


async def test_resend_sends_the_invitation_email_to_the_stored_address(pool) -> None:
    client_id = await seed_client(pool, STORED)
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        response = await resend_invitation(
            client_id,
            current_user=STAFF,
            invite_service=InviteService(pool),
            db_pool=pool,
        )
    sender.assert_awaited_once()
    kwargs = sender.await_args.kwargs
    assert kwargs["to"] == STORED
    assert kwargs["client_id"] == client_id
    assert "token=" in kwargs["invite_url"]
    assert response["success"] is True
    assert response["email_sent"] is True
    assert response["email_error"] is None
    assert "token" not in response["data"]
    assert "invite_url" not in response["data"]
    assert await invitation_rows(pool, client_id) == [STORED]


async def test_resend_reports_an_email_failure_without_its_text(pool, caplog) -> None:
    client_id = await seed_client(pool, STORED)
    diagnostic = f"550 mailbox {STORED} rejected by relay-internal-7"
    caplog.set_level(logging.DEBUG)
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email",
        new=AsyncMock(side_effect=RuntimeError(diagnostic)),
    ):
        response = await resend_invitation(
            client_id,
            current_user=STAFF,
            invite_service=InviteService(pool),
            db_pool=pool,
        )
    assert response["success"] is True
    assert response["email_sent"] is False
    assert response["email_error"] == "delivery_failed"
    assert "check email service" in response["message"]
    assert diagnostic not in str(response)
    assert "relay-internal-7" not in caplog.text
    assert "RuntimeError" in caplog.text
