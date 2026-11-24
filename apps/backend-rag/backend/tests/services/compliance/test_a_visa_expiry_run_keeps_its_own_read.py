"""What a visa expiry run could not read belongs to that run, and a healthy run is untouched.

Two statements, neither of which needs a database:

* a run that read every document type posts, byte for byte, the mails it posted before the
  notice existed. ``fixtures/visa_expiry_team_notifier/healthy_run_mails.json`` holds the two
  mails the notifier rendered for the rows below before this change;
* the document types a run could not read travel with that run. One notifier used by two
  runs, one after the other or one inside the other, marks the mails of the run that failed
  and no others.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import backend.services.compliance.visa_expiry_team_notifier as visa_mod
from backend.services.compliance.visa_expiry_team_notifier import VisaExpiryTeamNotifier

pytestmark = pytest.mark.asyncio

LEADER = "leader.a@example.test"
KITAS_ROW: dict[str, Any] = {
    "client_id": 1,
    "client_name": "Zz One",
    "client_email": "one@example.test",
    "client_phone": "000",
    "nationality": "ZZ",
    "assigned_to": LEADER,
    "document_type": "kitas",
    "expiry_date": date(2031, 1, 15),
    "days_until_expiry": 6,
    "current_visa_type": "E28A",
    "current_visa_sponsor": "Zz Sponsor",
    "passport_expiry": date(2031, 1, 10),
}
PASSPORT_ROW: dict[str, Any] = {
    "client_id": 2,
    "client_name": "Zz Two",
    "client_email": None,
    "client_phone": None,
    "nationality": None,
    "assigned_to": LEADER,
    "document_type": "passport",
    "expiry_date": date(2031, 2, 20),
    "days_until_expiry": 42,
    "current_visa_type": None,
    "current_visa_sponsor": None,
    "passport_expiry": date(2031, 2, 20),
}
BASE_MAILS: list[dict[str, str]] = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "visa_expiry_team_notifier"
        / "healthy_run_mails.json"
    ).read_text(encoding="utf-8")
)


def _kitas_notice() -> str:
    """Read when a test needs it, so the rendering tests also run on a tree without it."""
    return visa_mod._incomplete_notice(("kitas",))


class _Clients:
    """Stands in for the pool: the rows of each document type, or a failure for it."""

    def __init__(self, rows_by_type: dict[str, list[dict[str, Any]]]) -> None:
        self.rows_by_type = rows_by_type
        self.failing: set[str] = set()

    def acquire(self) -> _Clients:
        return self

    async def __aenter__(self) -> _Clients:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def fetch(self, _sql: str, doc_type: str) -> list[dict[str, Any]]:
        if doc_type in self.failing:
            raise RuntimeError("synthetic query failure")
        return [dict(row) for row in self.rows_by_type.get(doc_type, [])]


class _Outbox:
    """Stands in for the HTTP client of every run of one notifier.

    The notifier keeps its client on the instance, as it did before this change, so a second
    real client would be closed under a first run still sending. That is not what these
    tests are about: one outbox serves all the runs and is never closed.
    """

    def __init__(self) -> None:
        self.mails: list[dict[str, Any]] = []
        self.hold_the_first = False
        self.holding = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self) -> _Outbox:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
        self.mails.append(json)
        if self.hold_the_first and len(self.mails) == 1:
            self.holding.set()
            await self.release.wait()
        return httpx.Response(200, json={"success": True}, request=httpx.Request("POST", url))


def _notifier(monkeypatch: pytest.MonkeyPatch, pool: Any) -> tuple[VisaExpiryTeamNotifier, _Outbox]:
    outbox = _Outbox()
    monkeypatch.setattr(VisaExpiryTeamNotifier, "_get_http_client", staticmethod(lambda: outbox))
    return VisaExpiryTeamNotifier(pool), outbox


def _marked(mail: dict[str, Any]) -> bool:
    return "incomplete" in mail["subject"] or "incomplete" in mail["body"].lower()


@pytest.mark.parametrize("rows_from", ["the caller's own rows", "the notifier's own read"])
async def test_a_run_that_read_everything_posts_what_it_posted_before(
    rows_from: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if rows_from == "the caller's own rows":
        notifier, outbox = _notifier(monkeypatch, MagicMock())
        notifier._get_expiring_documents = AsyncMock(
            return_value=[dict(KITAS_ROW), dict(PASSPORT_ROW)]
        )
    else:
        pool = _Clients({"kitas": [KITAS_ROW], "passport": [PASSPORT_ROW]})
        notifier, outbox = _notifier(monkeypatch, pool)

    result = await notifier.check_and_notify()

    assert result == {"total_alerts": 2, "emails_sent": 2, "emails_failed": 0}
    assert [set(mail) for mail in outbox.mails] == [{"to", "subject", "body"}] * 2
    assert [mail["to"] for mail in outbox.mails] == [LEADER, visa_mod.ADMIN_EMAIL]
    assert [
        {"subject": mail["subject"], "body": mail["body"]} for mail in outbox.mails
    ] == BASE_MAILS


async def test_two_runs_of_one_notifier_that_overlap_keep_their_own_unread_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Clients({"passport": [PASSPORT_ROW]})
    notifier, outbox = _notifier(monkeypatch, pool)

    # The first run cannot read KITAS, and stops inside the send of its leader mail.
    pool.failing = {"kitas"}
    outbox.hold_the_first = True
    first = asyncio.create_task(notifier.check_and_notify())
    await asyncio.wait_for(outbox.holding.wait(), timeout=5)

    # A second run of the same notifier reads everything, start to end.
    pool.failing = set()
    second_result = await notifier.check_and_notify()

    # The first run goes on to its summary.
    outbox.release.set()
    first_result = await asyncio.wait_for(first, timeout=5)

    assert first_result == {
        "total_alerts": 1,
        "emails_sent": 2,
        "emails_failed": 0,
        "failed_queries": ["kitas"],
    }
    assert second_result == {"total_alerts": 1, "emails_sent": 2, "emails_failed": 0}
    first_leader, second_leader, second_summary, first_summary = outbox.mails
    assert [mail["to"] for mail in outbox.mails] == [
        LEADER,
        LEADER,
        visa_mod.ADMIN_EMAIL,
        visa_mod.ADMIN_EMAIL,
    ]
    assert not _marked(second_leader)
    assert not _marked(second_summary)
    # The first run's mails are the second run's, plus the notice and the subject mark.
    assert first_leader["subject"] == second_leader["subject"] + " (incomplete list)"
    assert first_summary["subject"] == second_summary["subject"] + " (incomplete)"
    notice = _kitas_notice()
    for marked, plain in ((first_leader, second_leader), (first_summary, second_summary)):
        assert marked["body"].count(notice) == 1
        assert marked["body"].replace(notice, "") == plain["body"]


async def test_one_notifier_used_run_after_run_marks_only_the_runs_that_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Clients({"passport": [PASSPORT_ROW]})
    notifier, outbox = _notifier(monkeypatch, pool)
    results = []
    for failing in ({"kitas"}, set(), {"visa", "kitas"}, set()):
        pool.failing = failing
        results.append(await notifier.check_and_notify())

    assert [result.get("failed_queries") for result in results] == [
        ["kitas"],
        None,
        ["visa", "kitas"],
        None,
    ]
    by_run = [outbox.mails[index : index + 2] for index in range(0, 8, 2)]
    assert [[_marked(mail) for mail in run] for run in by_run] == [
        [True, True],
        [False, False],
        [True, True],
        [False, False],
    ]
    assert by_run[1] == by_run[3]


async def test_the_notice_brings_its_own_line_break_and_is_empty_for_a_full_read() -> None:
    assert visa_mod._incomplete_notice(()) == ""
    notice = _kitas_notice()
    assert notice.startswith("\n<p ")
    assert "could not be read in this run: KITAS." in notice
