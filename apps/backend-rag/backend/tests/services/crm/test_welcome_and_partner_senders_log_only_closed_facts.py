"""The welcome senders log record ids and words written in the source, and nothing else.

A sent welcome wrote the client's address at INFO. A refused one wrote the far end's answer,
and every handler of an unexpected error wrote the exception with its traceback: its message,
its class name, the file and the function it came from. Any of those can quote the client's
address or phone, so these modules now log none of it: a status is digits, an error is one
word, a client is an id.

Each case drives a PUBLIC entry of a sender against a far end that answers, or fails, in
words that quote the client. It then reads EVERY record of EVERY backend logger: the line
formatted with its traceback, and the repr of every attribute of the record. It asserts the
line that IS written, so a case cannot pass because nothing was logged.

One exception, named where it is used: when the mail audit's own database write fails, the
audit helper (``services/notifications/email_audit.py``) logs that failure under its own
logger. That helper is not changed here; in those two cases its records are not judged, and
the senders' records still are.

The mail audit row is not a log. The diagnostic it stores is asserted unchanged.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest

import backend.services.crm.partners.emails as partner_mails
import backend.services.crm.welcome.welcome_email_service as welcome
import backend.services.crm.welcome.welcome_practice_service as kickoff
import backend.services.crm.welcome.welcome_whatsapp_service as whatsapp
from backend.services.notifications.email_audit import EmailNotDelivered, format_send_error

ADDRESS = "zz.client.probe@example.test"
ENCODED = "zz.client.probe%40example.test"
PHONE = "+62 812-0000-4242"
DIGITS = "6281200004242"
FAR_END_WORDS = "turned away by the far end"
OUTSIDE = f"recipient {ADDRESS} <{ENCODED}> at {PHONE} ({DIGITS}) {FAR_END_WORDS}"
FORBIDDEN = (
    ADDRESS,
    ENCODED,
    "zz.client.probe",
    "example.test",
    PHONE,
    DIGITS,
    "0000-4242",
    "00004242",
    FAR_END_WORDS,
)
AUDIT_LOGGER = "backend.services.notifications.email_audit"
SENDER_LOGGERS = (welcome.__name__, kickoff.__name__, whatsapp.__name__, partner_mails.__name__)
CLIENT = {
    "id": 7,
    "full_name": "Zz Client",
    "email": ADDRESS,
    "phone": PHONE,
    "nationality": "Italian",
    "assigned_to": None,
}
META_REFUSAL = {
    "error": {
        "message": OUTSIDE,
        "type": "OAuthException",
        "code": int(DIGITS),
        "error_subcode": int(DIGITS),
        "error_data": {"details": OUTSIDE},
    }
}


class _LyingInt(int):
    """An integer that prints the address wherever it is asked for text."""

    def __str__(self) -> str:
        return ADDRESS

    __repr__ = __str__

    def __format__(self, spec: str) -> str:
        return ADDRESS


class _LyingUUID(uuid.UUID):
    def __str__(self) -> str:
        return ADDRESS

    __repr__ = __str__


class _Db:
    """A pool with one connection that knows one client.

    A statement that holds a key of ``failing`` fails: with that error, or, when the value is
    a callable, with whatever the callable raises from its own frame.
    """

    def __init__(self, *, failing: dict[str, Any] | None = None) -> None:
        self.failing = failing or {}
        self.statements: list[str] = []

    def acquire(self) -> _Db:
        return self

    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def _run(self, sql: str) -> None:
        self.statements.append(sql)
        for part, failure in self.failing.items():
            if part in sql:
                if isinstance(failure, BaseException):
                    raise failure
                failure()

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._run(sql)
        return [{"id": 1, "client_id": 7, "scheduled_at": None}]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._run(sql)
        if "FROM clients" in sql:
            return dict(CLIENT)
        if "FROM email_send_log" in sql:
            return {"attempt_number": 1, "email_type": "welcome"}
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._run(sql)
        if "DELETE FROM welcome_email_queue" in sql or "INSERT INTO email_send_log" in sql:
            return 11
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self._run(sql)
        return "OK"


class _FarEnd:
    """What Meta, the mail endpoint and the brochure host answer; an exception is raised."""

    def __init__(self) -> None:
        self.mail: Any = httpx.Response(200, json={"success": True})
        self.meta: Any = httpx.Response(200, json={"messages": [{"id": "wamid.zz"}]})
        self.brochure: Any = httpx.Response(404)
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.asked.append("brochure")
            reply = self.brochure
        elif request.url.host == "graph.facebook.com":
            self.asked.append("meta")
            reply = self.meta
        else:
            self.asked.append("mail")
            reply = self.mail
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.fixture(autouse=True)
def far_end(monkeypatch: pytest.MonkeyPatch) -> _FarEnd:
    """Every HTTP client these modules open talks to the stand-in: no case reaches a network."""
    far = _FarEnd()
    real_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(far), **kwargs)

    async def mail_client() -> httpx.AsyncClient:
        return client()

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(welcome, "get_email_client", mail_client)
    monkeypatch.setattr(welcome, "_BROCHURE_PATH", Path("/nonexistent/zz/brochure.pdf"))
    monkeypatch.setattr(kickoff, "_BROCHURE_PATH", Path("/nonexistent/zz/brochure.pdf"))
    monkeypatch.setattr(welcome, "_EMAIL_WELCOME_ACTIVE", True)
    monkeypatch.setattr(kickoff, "_EMAIL_PRACTICE_ACTIVE", True)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "100000000000001")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "zz-test")
    return far


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stand-ins for the two audit calls of the client welcome mail, to read what they get."""
    calls = SimpleNamespace(attempted=AsyncMock(return_value=11), recorded=AsyncMock())
    monkeypatch.setattr(welcome, "log_email_attempt", calls.attempted)
    monkeypatch.setattr(welcome, "record_email_result", calls.recorded)
    return calls


def _whatsapp_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both WhatsApp senders are off in production by a module flag; a case turns them on."""
    monkeypatch.setattr(kickoff, "_WHATSAPP_PRACTICE_ACTIVE", True)
    monkeypatch.setattr(whatsapp, "_WHATSAPP_WELCOME_ACTIVE", True)


def _read(caplog: pytest.LogCaptureFixture, *, unjudged: tuple[str, ...] = ()) -> list[str]:
    """Every line of every backend logger, after judging everything its records carry."""
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    lines: list[str] = []
    carried: list[str] = []
    for record in caplog.records:
        if not record.name.startswith("backend.") or record.name in unjudged:
            continue
        formatted = formatter.format(record)  # the message, and the traceback when there is one
        lines.extend(formatted.splitlines())
        carried.append(formatted)
        carried.extend(repr(value) for value in vars(record).values())
        if record.name in SENDER_LOGGERS:
            # No object a caller passed is kept on a sender's record.
            kept = [type(argument) for argument in record.args or ()]
            assert [kind for kind in kept if kind not in (str, int, bool)] == []
    everything = "\n".join(carried)
    assert [token for token in FORBIDDEN if token in everything] == []
    return lines


def _line(lines: list[str], text: str) -> None:
    """One line ENDS with ``text``: nothing is printed after the fact."""
    assert [line for line in lines if line.endswith(text)], lines


# ── the client welcome mail ──────────────────────────────────────────────────

_MAIL_FAILURES: list[tuple[str, Callable[[], Any], str]] = [
    ("refused with JSON", lambda: httpx.Response(400, json={"detail": OUTSIDE}), "status=400"),
    ("an error page", lambda: httpx.Response(502, text=OUTSIDE * 3), "status=502"),
    (
        "not delivered",
        lambda: httpx.Response(200, json={"success": False, "message": OUTSIDE}),
        "not delivered",
    ),
    (
        "delivery uncertain",
        lambda: httpx.Response(
            200, json={"success": False, "message": OUTSIDE, "delivery_uncertain": True}
        ),
        "delivery uncertain",
    ),
    ("no connection", lambda: httpx.ConnectError(OUTSIDE), "connection error"),
    ("a timeout", lambda: httpx.ReadTimeout(OUTSIDE), "timeout error"),
]


@pytest.mark.asyncio
async def test_a_sent_welcome_mail_names_the_client_by_id(
    audit: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await welcome.process_pending_welcome_emails(_Db())

    lines = _read(caplog)
    assert audit.recorded.await_args.kwargs["status"] == "sent"
    _line(lines, "WelcomeEmail: sent to client 7 (lang=en)")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "reason"),
    [failure[1:] for failure in _MAIL_FAILURES],
    ids=[failure[0] for failure in _MAIL_FAILURES],
)
async def test_a_welcome_mail_that_is_not_sent_logs_a_reason_written_in_the_source(
    far_end: _FarEnd,
    audit: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    answer: Callable[[], Any],
    reason: str,
) -> None:
    far_end.mail = answer()

    with caplog.at_level(logging.DEBUG):
        await welcome.process_pending_welcome_emails(_Db())

    assert audit.recorded.await_args.kwargs["status"] == "failed"
    _line(_read(caplog), f"WelcomeEmail: send failed for client 7: {reason}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "word"),
    [
        (RuntimeError(OUTSIDE), "other"),
        (asyncpg.PostgresError(OUTSIDE), "database"),
        (KeyError(OUTSIDE), "lookup"),
    ],
    ids=["an unexpected error", "a database error", "a missing key"],
)
async def test_a_queued_welcome_that_fails_before_the_send_logs_one_word(
    audit: SimpleNamespace, caplog: pytest.LogCaptureFixture, error: Exception, word: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        stats = await welcome.process_pending_welcome_emails(_Db(failing={"FROM clients": error}))

    assert stats["failed"] == 1
    _line(_read(caplog), f"WelcomeEmail: send failed for client 7: {word} error")


@pytest.mark.asyncio
async def test_a_welcome_that_cannot_be_queued_logs_one_word(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refused = asyncpg.UniqueViolationError(OUTSIDE)

    with caplog.at_level(logging.DEBUG):
        await welcome.schedule_client_welcome_email(
            7, _Db(failing={"INSERT INTO welcome_email_queue": refused})
        )

    _line(_read(caplog), "WelcomeEmail: failed to queue for client 7: database error")


@pytest.mark.asyncio
async def test_a_queued_welcome_logs_the_client_and_a_clock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await welcome.schedule_client_welcome_email(7, _Db())

    lines = _read(caplog)
    assert [
        line
        for line in lines
        if re.search(r"WelcomeEmail: queued for client 7, will send at \d\d:\d\d UTC$", line)
    ], lines


# ── the diagnostic the audit row keeps is the one it always kept ─────────────


def _what_main_stores(answer: Any) -> str:
    """The ``error_message`` the client welcome mail has always handed to the audit row."""
    if isinstance(answer, Exception):
        return f"exception: {format_send_error(answer)}"
    if answer.status_code != 200:
        return f"status={answer.status_code} body={answer.text[:200]}"
    message = str(answer.json()["message"])[:200]
    return f"not delivered: {format_send_error(EmailNotDelivered(message))}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [failure[1] for failure in _MAIL_FAILURES],
    ids=[failure[0] for failure in _MAIL_FAILURES],
)
async def test_the_audit_row_of_a_failed_welcome_keeps_its_diagnostic_unchanged(
    far_end: _FarEnd, audit: SimpleNamespace, answer: Callable[[], Any]
) -> None:
    far_end.mail = answer()
    db = _Db()

    await welcome.process_pending_welcome_emails(db)

    assert audit.attempted.await_args.args == (db,)
    assert audit.attempted.await_args.kwargs == {
        "email_type": "welcome",
        "to_email": ADDRESS,
        "subject": "[CLIENT] " + welcome.WELCOME_EMAIL_SUBJECT["en"],
        "client_id": 7,
    }
    assert audit.recorded.await_args.args == (db, 11)
    assert audit.recorded.await_args.kwargs == {
        "status": "failed",
        "provider": "brevo",
        "error_message": _what_main_stores(answer()),
    }
    # The row is where a person finds the cause: it still quotes the far end.
    assert ADDRESS in audit.recorded.await_args.kwargs["error_message"]


# ── the practice kickoff: WhatsApp and mail ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_kickoff_sent_on_both_channels_logs_ids_and_three_booleans(
    monkeypatch: pytest.MonkeyPatch, far_end: _FarEnd, caplog: pytest.LogCaptureFixture
) -> None:
    _whatsapp_on(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db())

    lines = _read(caplog)
    assert far_end.asked == ["meta", "mail"]
    _line(lines, "PracticeKickoff WA: sent to client 7 practice 41 (lang=en)")
    _line(lines, "PracticeKickoff Email: sent to client 7 practice 41 (lang=en)")
    _line(
        lines,
        "PracticeKickoff: crm_welcome_runs UPSERT for client=7 practice=41 "
        "success=True whatsapp=True email=True",
    )


@pytest.mark.asyncio
async def test_a_kickoff_mail_that_is_refused_logs_the_status(
    far_end: _FarEnd, caplog: pytest.LogCaptureFixture
) -> None:
    far_end.mail = httpx.Response(400, json={"detail": OUTSIDE})

    with caplog.at_level(logging.DEBUG):
        await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db())

    lines = _read(caplog)
    _line(lines, "PracticeKickoff Email: send failed for practice 41: status=400")
    _line(
        lines,
        "PracticeKickoff: crm_welcome_runs UPSERT for client=7 practice=41 "
        "success=False whatsapp=False email=False",
    )


@pytest.mark.asyncio
async def test_a_kickoff_mail_answered_200_is_logged_as_sent_whatever_the_answer_says(
    far_end: _FarEnd, caplog: pytest.LogCaptureFixture
) -> None:
    """Not cured here and said so: this sender reads every 200 as sent. The line is closed."""
    far_end.mail = httpx.Response(200, json={"success": False, "message": OUTSIDE})

    with caplog.at_level(logging.DEBUG):
        await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db())

    lines = _read(caplog)
    _line(lines, "PracticeKickoff Email: sent to client 7 practice 41 (lang=en)")
    _line(
        lines,
        "PracticeKickoff: crm_welcome_runs UPSERT for client=7 practice=41 "
        "success=False whatsapp=False email=True",
    )


@pytest.mark.asyncio
async def test_a_kickoff_whatsapp_that_meta_refuses_logs_the_status_and_no_code(
    monkeypatch: pytest.MonkeyPatch, far_end: _FarEnd, caplog: pytest.LogCaptureFixture
) -> None:
    _whatsapp_on(monkeypatch)
    far_end.meta = httpx.Response(400, json=META_REFUSAL)

    with caplog.at_level(logging.DEBUG):
        await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db())

    lines = _read(caplog)
    _line(lines, "PracticeKickoff WA: Meta API error for practice 41: status=400")
    _line(
        lines,
        "PracticeKickoff: crm_welcome_runs UPSERT for client=7 practice=41 "
        "success=False whatsapp=False email=True",
    )


@pytest.mark.asyncio
async def test_a_kickoff_whose_run_cannot_be_recorded_logs_one_word(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = _Db(failing={"crm_welcome_runs": asyncpg.PostgresError(OUTSIDE)})

    with caplog.at_level(logging.DEBUG):
        await kickoff.send_practice_kickoff(41, 7, "zz_code", db)

    _line(
        _read(caplog),
        "PracticeKickoff: crm_welcome_runs UPSERT failed for client=7 practice=41: database error",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("far", ["meta", "mail"])
async def test_a_kickoff_that_fails_unexpectedly_logs_one_word(
    monkeypatch: pytest.MonkeyPatch, far_end: _FarEnd, caplog: pytest.LogCaptureFixture, far: str
) -> None:
    _whatsapp_on(monkeypatch)
    setattr(far_end, far, httpx.ConnectError(OUTSIDE))

    with caplog.at_level(logging.DEBUG):
        await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db())

    _line(
        _read(caplog),
        "PracticeKickoff: unhandled error for practice 41 (client 7): connection error",
    )


# ── the welcome WhatsApp ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "line"),
    [
        (
            lambda: httpx.Response(400, json=META_REFUSAL),
            "WelcomeWhatsApp: Meta API error for client 7: status=400",
        ),
        (
            lambda: httpx.Response(500, text=OUTSIDE),
            "WelcomeWhatsApp: Meta API error for client 7: status=500",
        ),
        (
            lambda: httpx.ConnectError(OUTSIDE),
            "WelcomeWhatsApp: unhandled error for client 7: connection error",
        ),
    ],
    ids=["refused", "an error page", "no connection"],
)
async def test_a_welcome_whatsapp_that_is_not_sent_logs_a_status_or_one_word(
    monkeypatch: pytest.MonkeyPatch,
    far_end: _FarEnd,
    caplog: pytest.LogCaptureFixture,
    answer: Callable[[], Any],
    line: str,
) -> None:
    _whatsapp_on(monkeypatch)
    far_end.meta = answer()

    with caplog.at_level(logging.DEBUG):
        await whatsapp.send_client_welcome(7, _Db())

    _line(_read(caplog), line)


@pytest.mark.asyncio
async def test_a_sent_welcome_whatsapp_prints_its_language_out_of_the_closed_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _whatsapp_on(monkeypatch)
    language = whatsapp.NATIONALITY_LANGUAGE_MAP.get(CLIENT["nationality"], "en")

    with caplog.at_level(logging.DEBUG):
        await whatsapp.send_client_welcome(7, _Db())

    _line(_read(caplog), f"WelcomeWhatsApp: sent to client 7 (lang={language})")


@pytest.mark.asyncio
async def test_a_language_that_is_not_in_the_set_is_printed_as_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A language table that came to hold outside text: the template is used, the text is not."""
    _whatsapp_on(monkeypatch)
    monkeypatch.setattr(whatsapp, "NATIONALITY_LANGUAGE_MAP", {"Italian": ADDRESS})
    monkeypatch.setattr(
        whatsapp, "WELCOME_WHATSAPP", {ADDRESS: "Ciao {first_name}, {advisor_name}"}
    )

    with caplog.at_level(logging.DEBUG):
        await whatsapp.send_client_welcome(7, _Db())

    _line(_read(caplog), "WelcomeWhatsApp: sent to client 7 (lang=?)")


# ── an error that names the client in its place and in its class ─────────────


def _raises_from_a_named_place() -> Callable[[], None]:
    """Code compiled under a file name that is the address, in a function named with the phone."""
    namespace: dict[str, Any] = {}
    source = f"def send_to_{DIGITS}():\n    raise RuntimeError({OUTSIDE!r})\n"
    exec(compile(source, ADDRESS, "exec"), namespace)  # noqa: S102
    return namespace[f"send_to_{DIGITS}"]


@pytest.mark.asyncio
async def test_an_error_raised_from_a_file_and_a_function_that_name_the_client_logs_one_word(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raiser = _raises_from_a_named_place()

    with caplog.at_level(logging.DEBUG):
        await welcome.schedule_client_welcome_email(
            7, _Db(failing={"INSERT INTO welcome_email_queue": raiser})
        )
        await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db(failing={"FROM clients": raiser}))

    lines = _read(caplog)
    _line(lines, "WelcomeEmail: failed to queue for client 7: other error")
    _line(lines, "PracticeKickoff: unhandled error for practice 41 (client 7): other error")


def _misnamed(base: type[Exception], name: str) -> type[Exception]:
    """A class named ``name`` that presents itself as a known library's connection error."""
    cls = type(name, (base,), {})
    cls.__module__ = "httpx"
    cls.__qualname__ = "ConnectError"
    return cls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "word"),
    [
        (_misnamed(ValueError, ADDRESS)(OUTSIDE), "value"),
        (_misnamed(KeyError, f"E{DIGITS}")(OUTSIDE), "lookup"),
        (
            ExceptionGroup(  # noqa: F821
                OUTSIDE, [_misnamed(ValueError, ADDRESS)(OUTSIDE), KeyError(OUTSIDE)]
            ),
            "other",
        ),
    ],
    ids=["a class named as the address", "a class named with the phone", "a group of both"],
)
async def test_an_error_whose_class_names_the_client_logs_the_word_of_its_real_base(
    caplog: pytest.LogCaptureFixture, error: Exception, word: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        await welcome.schedule_client_welcome_email(
            7, _Db(failing={"INSERT INTO welcome_email_queue": error})
        )

    _line(_read(caplog), f"WelcomeEmail: failed to queue for client 7: {word} error")


# ── numbers that are not what they are called ────────────────────────────────


async def _welcome_whatsapp(far: _FarEnd, status: Any) -> None:
    far.meta = httpx.Response(status, json=META_REFUSAL)
    await whatsapp.send_client_welcome(7, _Db())


async def _kickoff_whatsapp(far: _FarEnd, status: Any) -> None:
    far.meta = httpx.Response(status, json=META_REFUSAL)
    await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db())


async def _kickoff_mail(far: _FarEnd, status: Any) -> None:
    far.mail = httpx.Response(status, json={"detail": OUTSIDE})
    await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db())


async def _welcome_mail(far: _FarEnd, status: Any) -> None:
    far.mail = httpx.Response(status, json={"detail": OUTSIDE})
    await welcome.process_pending_welcome_emails(_Db())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send", "line"),
    [
        (_welcome_whatsapp, "WelcomeWhatsApp: Meta API error for client 7: status=?"),
        (_kickoff_whatsapp, "PracticeKickoff WA: Meta API error for practice 41: status=?"),
        (_kickoff_mail, "PracticeKickoff Email: send failed for practice 41: status=?"),
        (_welcome_mail, "WelcomeEmail: send failed for client 7: status=?"),
    ],
    ids=["welcome whatsapp", "kickoff whatsapp", "kickoff mail", "welcome mail"],
)
@pytest.mark.parametrize(
    "status",
    [int(DIGITS), _LyingInt(400)],
    ids=["the phone's digits", "an int subclass that prints the address"],
)
async def test_a_status_that_is_not_an_exact_http_status_is_printed_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    far_end: _FarEnd,
    audit: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    send: Callable[[_FarEnd, Any], Any],
    line: str,
    status: Any,
) -> None:
    _whatsapp_on(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        await send(far_end, status)

    _line(_read(caplog), line)


@pytest.mark.asyncio
async def test_an_id_that_prints_the_address_is_printed_as_unknown_or_in_canonical_form(
    caplog: pytest.LogCaptureFixture,
) -> None:
    partner = _LyingUUID(int=42)
    assert str(partner) == ADDRESS and f"{_LyingInt(7)}" == ADDRESS

    with caplog.at_level(logging.DEBUG):
        await welcome.schedule_client_welcome_email(_LyingInt(7), _Db())
        await partner_mails.send_welcome(_Db(), partner)

    lines = _read(caplog)
    assert [
        line
        for line in lines
        if re.search(r"WelcomeEmail: queued for client \?, will send at \d\d:\d\d UTC$", line)
    ], lines
    _line(
        lines,
        "send_welcome: partner 00000000-0000-0000-0000-00000000002a not found — skip",
    )


# ── the brochure handlers ────────────────────────────────────────────────────


def _unreadable_brochure(tmp_path: Path) -> Path:
    """A path that exists and cannot be read as a file; its name is the client's address."""
    path = tmp_path / ADDRESS
    path.mkdir()
    return path


@pytest.mark.asyncio
async def test_a_welcome_brochure_that_cannot_be_fetched_or_read_logs_one_word(
    monkeypatch: pytest.MonkeyPatch,
    far_end: _FarEnd,
    audit: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    far_end.brochure = httpx.ConnectError(OUTSIDE)  # the download fails first
    monkeypatch.setattr(welcome, "_BROCHURE_PATH", _unreadable_brochure(tmp_path))  # then the file

    with caplog.at_level(logging.DEBUG):
        await welcome.process_pending_welcome_emails(_Db())

    lines = _read(caplog)
    assert far_end.asked == ["brochure", "mail"]
    _line(lines, "WelcomeEmail: could not attach brochure for client 7: os error")
    _line(lines, "WelcomeEmail: sent to client 7 (lang=en)")


@pytest.mark.asyncio
async def test_a_welcome_brochure_that_is_nowhere_and_one_that_is_fetched_log_the_client_id(
    far_end: _FarEnd, audit: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    far_end.brochure = httpx.ConnectError(OUTSIDE)
    with caplog.at_level(logging.DEBUG):
        await welcome.process_pending_welcome_emails(_Db())
    _line(_read(caplog), "WelcomeEmail: brochure unavailable for client 7")

    caplog.clear()
    far_end.brochure = httpx.Response(200, content=b"%PDF-zz")
    with caplog.at_level(logging.DEBUG):
        await welcome.process_pending_welcome_emails(_Db())
    _line(_read(caplog), "WelcomeEmail: brochure attached from URL (7 bytes)")


@pytest.mark.asyncio
async def test_a_kickoff_brochure_that_cannot_be_read_logs_one_word(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(kickoff, "_BROCHURE_PATH", _unreadable_brochure(tmp_path))

    with caplog.at_level(logging.DEBUG):
        await kickoff.send_practice_kickoff(41, 7, "zz_code", _Db())

    lines = _read(caplog)
    _line(lines, "PracticeKickoff Email: could not read brochure for practice 41: os error")
    _line(lines, "PracticeKickoff Email: sent to client 7 practice 41 (lang=en)")


# ── the audit's own write fails: the real helper in place ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statement", "helper_line"),
    [
        ("INSERT INTO email_send_log", "email_audit: log_email_attempt failed"),
        ("UPDATE email_send_log", "email_audit: record_email_result failed for 11"),
    ],
    ids=["the audit insert fails", "the audit update fails"],
)
async def test_when_the_audit_write_fails_the_senders_own_lines_stay_closed(
    far_end: _FarEnd, caplog: pytest.LogCaptureFixture, statement: str, helper_line: str
) -> None:
    """The audit helper is the real one. Its own records are the one named exception.

    In the update case the insert succeeds first, so the helper holds a row id: without one
    it returns before any write.
    """
    far_end.mail = httpx.Response(400, json={"detail": OUTSIDE})
    db = _Db(failing={statement: asyncpg.PostgresError(OUTSIDE)})

    with caplog.at_level(logging.DEBUG):
        await welcome.process_pending_welcome_emails(db)

    # The helper did meet the failure and did log it: the exception is used, not assumed.
    assert [
        record
        for record in caplog.records
        if record.name == AUDIT_LOGGER and helper_line in record.getMessage()
    ]
    assert any(statement in run for run in db.statements)
    _line(
        _read(caplog, unjudged=(AUDIT_LOGGER,)),
        "WelcomeEmail: send failed for client 7: status=400",
    )
