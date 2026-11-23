"""The partner mail logs name the partner and the commission by id, never by address.

Every welcome and every commission mail wrote the partner's address at INFO, twice (queued and
sent), and the partner insert wrote it at DEBUG. Each test drives the real function on real
rows, then reads every record of the partner loggers: the formatted message, its arguments and
every attribute. The ids are the driver's own objects, printed in their canonical form.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

import backend.services.crm.partners.emails as emails_mod
from backend.services.crm.partners.emails import (
    enqueue_commission_earned,
    enqueue_welcome,
    send_commission_earned,
    send_welcome,
)
from backend.services.crm.partners.repository import PartnersRepository

ADDRESS = "zz.partner.probe@test.invalid"
_PARTNER_LOGGERS = "backend.services.crm.partners"


def _said(caplog: pytest.LogCaptureFixture) -> str:
    """Everything the partner loggers put on a record, as one text."""
    parts: list[str] = []
    for record in caplog.records:
        if not record.name.startswith(_PARTNER_LOGGERS):
            continue
        parts.append(record.getMessage())
        parts.extend(repr(value) for value in vars(record).values())
        kept = [type(argument) for argument in record.args or ()]
        assert [kind for kind in kept if kind not in (str, int, bool)] == []
    said = "\n".join(parts)
    assert "zz.partner.probe" not in said and "test.invalid" not in said
    return said


@pytest.fixture
def quiet_mail():
    with (
        patch.object(emails_mod, "_post_email", new=AsyncMock()) as post,
        patch.object(emails_mod, "_build_pricing_services", return_value=[]),
    ):
        yield post


@pytest.mark.asyncio
async def test_a_queued_and_a_sent_welcome_name_the_partner_by_id(
    db_conn, partner_factory, quiet_mail, caplog: pytest.LogCaptureFixture
) -> None:
    partner_id = uuid.UUID(int=(await partner_factory(email=ADDRESS)).int)
    other_id = uuid.UUID(int=(await partner_factory(email=f"second.{ADDRESS}")).int)

    with caplog.at_level(logging.DEBUG, logger=_PARTNER_LOGGERS):
        await enqueue_welcome(db_conn, partner_id)
        await send_welcome(db_conn, other_id)

    said = _said(caplog)
    assert f"enqueue_welcome: enqueued for partner {partner_id}\n" in said
    assert f"send_welcome: sent to partner {other_id}\n" in said
    assert quiet_mail.await_args.kwargs["to"] == f"second.{ADDRESS}"  # the mail still goes there


@pytest.mark.asyncio
async def test_a_queued_and_a_sent_commission_mail_name_the_commission_by_id(
    db_conn, partner_factory, commission_factory, client_factory, quiet_mail, caplog
) -> None:
    client = await client_factory(full_name="Zz Client")
    partner = await partner_factory(email=ADDRESS)
    queued = await commission_factory(
        partner_id=partner.id,
        practice_client_id=int(client.id),
        status="paid",
        net_amount_idr=Decimal("500000"),
    )
    sent = await commission_factory(
        partner_id=partner.id,
        practice_client_id=int(client.id),
        status="paid",
        net_amount_idr=Decimal("700000"),
    )

    with caplog.at_level(logging.DEBUG, logger=_PARTNER_LOGGERS):
        await enqueue_commission_earned(db_conn, queued.id)
        await send_commission_earned(db_conn, sent.id)

    said = _said(caplog)
    assert f"enqueue_commission_earned: enqueued for commission {queued.id}\n" in said
    assert f"send_commission_earned: sent for commission {sent.id}\n" in said
    assert quiet_mail.await_args.kwargs["to"] == ADDRESS


@pytest.mark.asyncio
async def test_a_new_partner_is_logged_by_the_id_the_database_gave_it(
    db_conn, user_factory, caplog
) -> None:
    owner = await user_factory()

    with caplog.at_level(logging.DEBUG, logger=_PARTNER_LOGGERS):
        partner_id = await PartnersRepository(db_conn).insert_partner(
            full_name="Zz Partner",
            email=ADDRESS,
            entity_type="individual",
            assigned_to=owner.id,
            created_by=owner.id,
        )

    said = _said(caplog)
    assert type(partner_id) is not uuid.UUID  # the driver's own class: a subclass
    assert f"insert_partner id={partner_id}\n" in said
