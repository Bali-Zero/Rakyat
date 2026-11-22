"""Unit tests for stale staff-transition customer emails."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from backend.services.garuda_orders.outbox_consumer import OutboxJob
from backend.services.garuda_orders.outbox_handlers import (
    _PRACTICE_TRANSITION_EMAILS,
    EmailSendFailed,
    PracticeTransitionEmailFacts,
    PracticeTransitionEmailHandler,
    _practice_transition_body,
    build_handlers,
)
from backend.services.garuda_portal.staff_transitions import TRANSITIONS


class _Pool:
    pass


class _Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send(self, *, to: str, subject: str, html_body: str) -> None:
        self.calls.append((to, subject, html_body))


def _facts(state: str) -> PracticeTransitionEmailFacts:
    return PracticeTransitionEmailFacts(
        order_id="ord_900001",
        email="person.one@example.test",
        case_type="visa_on_arrival",
        state=state,
        customer_reason_key=None,
        required_action_key=None,
        artifact_available=False,
    )


def _job(job_type: str) -> OutboxJob:
    return OutboxJob(
        id=1,
        order_id="ord_900001",
        journal_event_id="evt_900001",
        job_type=job_type,
        payload={},
        attempts=1,
    )


def _handler(entry: tuple, facts: PracticeTransitionEmailFacts):
    # Built without the new argument and read by length, so that on a table
    # without announced states a guilt test fails on its own assertion.
    job_type, subject, message, *rest = entry
    sender = _Sender()
    handler = PracticeTransitionEmailHandler(
        _Pool(), sender, job_type=job_type, subject=subject, body=_practice_transition_body(message)
    )
    handler._announced_states = frozenset(rest[0]) if rest else None
    handler._load = AsyncMock(return_value=facts)
    return handler, sender


def _entry(job_type: str) -> tuple:
    return next(entry for entry in _PRACTICE_TRANSITION_EMAILS if entry[0] == job_type)


async def test_approved_job_in_delivered_state_resolves_without_sending(caplog):
    handler, sender = _handler(_entry("practice_approved_email"), _facts("Delivered"))

    with caplog.at_level(logging.WARNING, logger="garuda.orders.outbox_handlers"):
        await handler(_job("practice_approved_email"))

    assert sender.calls == []
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert warnings == [
        "outbox practice_approved_email resolved WITHOUT sending for order ord_900001: "
        "announced states ['Approved'], current state Delivered"
    ]
    assert "person.one@example.test" not in warnings[0]


async def test_action_needed_job_after_resume_resolves_without_sending():
    handler, sender = _handler(_entry("practice_blocked_email"), _facts("In_review"))

    await handler(_job("practice_blocked_email"))

    assert sender.calls == []


@pytest.mark.parametrize("entry", _PRACTICE_TRANSITION_EMAILS, ids=lambda entry: entry[0])
async def test_announced_state_sends_its_own_subject(entry):
    job_type, subject = entry[0], entry[1]
    state = entry[3][0] if len(entry) > 3 else "Approved"
    handler, sender = _handler(entry, _facts(state))

    await handler(_job(job_type))

    assert [call[1] for call in sender.calls] == [subject]


async def test_missing_order_raises_the_existing_error():
    entry = _entry("practice_approved_email")
    handler, _sender = _handler(entry, _facts("Approved"))
    handler._load = AsyncMock(return_value=None)

    with pytest.raises(EmailSendFailed, match="order ord_900001 .* practice_approved_email"):
        await handler(_job("practice_approved_email"))


def test_every_transition_email_has_announced_states():
    assert all(len(entry) == 4 and entry[3] for entry in _PRACTICE_TRANSITION_EMAILS)


def test_announced_states_mirror_the_staff_transition_table():
    """Each job type announces exactly the states its transitions lead to."""
    expected: dict[str, set[str]] = {}
    for spec in TRANSITIONS.values():
        job_type = spec.event_name.replace(".", "_") + "_email"
        expected.setdefault(job_type, set()).add(spec.to_state)

    announced = {
        entry[0]: set(entry[3:4][0]) if entry[3:4] else set()
        for entry in _PRACTICE_TRANSITION_EMAILS
    }

    assert announced == expected


async def test_the_registry_hands_each_handler_its_announced_states():
    sender = _Sender()
    handler = build_handlers(_Pool(), sender)["practice_approved_email"]
    handler._load = AsyncMock(return_value=_facts("Delivered"))

    await handler(_job("practice_approved_email"))

    assert sender.calls == []
