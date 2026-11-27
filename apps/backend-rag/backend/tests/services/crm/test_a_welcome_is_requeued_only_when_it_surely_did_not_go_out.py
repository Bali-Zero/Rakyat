"""A welcome mail goes back to its queue only when it SURELY did not go out.

The drain loop deletes a queue row to claim it, before the send. A failed welcome was therefore
gone for good, and putting it back is right only when the mail certainly did not reach the client:
anything else (a read timeout, a 502, a body that cannot be read) may be a delivered mail, and a
second send is a duplicate to a client. The invariants, swept rather than listed:

- I1 a requeue needs PROOF of non-delivery: a pre-write connect/pool error, a refusing status
  {400, 401, 403, 404, 422}, or a readable 200 that says not delivered. Everything else is UNCERTAIN:
  audited, paged once, never requeued.
- I2 no audit row, no POST: the row goes back to the queue and nothing is sent.
- I3 the cap (three) counts every audited attempt that did not end sent, uncertain ones included.
- I4 a delivered mail stays delivered when the alert row cannot be written.

The guilt half is a sweep over the INSTALLED httpx exception tree and over every status code.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid5

import httpx
import pytest

import backend.services.crm.welcome.welcome_email_service as welcome
from backend.app.modules.notifications import router as notifications_router

CLIENT = {
    "id": 7,
    "full_name": "Contoh Client",
    "email": "client@example.test",
    "nationality": "Italian",
    "assigned_to": None,
}
SENT_BODY = {"success": True, "message": "Email sent"}
NOT_SENT_BODY = {"success": False, "message": "All providers failed"}
UNCERTAIN_BODY = {"success": False, "message": "delivery uncertain", "delivery_uncertain": True}
REQUEST = httpx.Request("POST", "https://mail.example.test/send")
PRE_WRITE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
REFUSED = {400, 401, 403, 404, 422}


class LocalFailure(Exception):
    """An exception class no httpx release knows."""


class FakeConn:
    """Serves the SQL the welcome drain and sender issue, from a small state."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.s = state

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM email_send_log" in sql and "WHERE status = 'failed'" in sql:
            return [
                {
                    "id": row["id"],
                    "email_type": "welcome",
                    "to_email": "client@example.test",
                    "subject": "Welcome",
                    "practice_id": None,
                    "client_id": row["client_id"],
                    "attempt_number": 1,
                    "error_message": "delivery uncertain",
                    "created_at": datetime.now(timezone.utc),
                }
                for row in self.s["log"]
                if row["status"] == "failed"
            ]
        assert "FROM welcome_email_queue" in sql
        # Every queued row is due: the sweeps re-run the drain and must see the row again.
        return [dict(r) for r in self.s["queue"]]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "SELECT attempt_number, email_type FROM email_send_log" in sql:
            return next(
                (
                    {"attempt_number": row["attempt_number"], "email_type": row["email_type"]}
                    for row in self.s["log"]
                    if row["id"] == args[0]
                ),
                None,
            )
        assert "FROM clients" in sql
        return self.s["client"]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql.lstrip().startswith("DELETE FROM welcome_email_queue"):
            if self.s["lose_claim"]:
                return None
            kept = [r for r in self.s["queue"] if r["id"] != args[0]]
            hit = len(kept) != len(self.s["queue"])
            self.s["queue"] = kept
            return args[0] if hit else None
        if "FROM notification_alerts" in sql:
            # The idempotency question: the alert marker, and (when asked) the audit's 'sent'.
            asks_audit = "FROM email_send_log" in sql
            sent_row = any(
                r["status"] == "sent" and r["client_id"] == args[0] for r in self.s["log"]
            )
            return (
                1
                if (self.s["alerts"] or self.s["already_sent"] or (asks_audit and sent_row))
                else None
            )
        if "FROM email_send_log" in sql:
            # The cap: count the rows by the statuses THIS query names, not by a list kept here.
            assert "'welcome'" in sql
            self.s["events"].append("cap")
            listed = re.search(r"status IN \(([^)]*)\)", sql)
            assert listed, sql
            names = set(re.findall(r"'([a-z_]+)'", listed.group(1)))
            return sum(
                1
                for r in self.s["log"]
                if r["email_type"] == "welcome"
                and r["client_id"] == args[0]
                and r["status"] in names
            )
        if "FROM team_members" in sql:
            return None
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: Any) -> None:
        if "UPDATE email_send_log" in sql:
            if "SET status = $2" in sql:
                for row in self.s["log"]:
                    if row["id"] == args[0]:
                        row["status"] = args[1]
                return
            for row in self.s["log"]:
                if row["id"] in args[0]:
                    row["status"] = "escalated"
            return
        if "INSERT INTO welcome_email_queue" in sql:
            assert "ON CONFLICT (client_id) DO NOTHING" in sql
            if all(r["client_id"] != args[0] for r in self.s["queue"]):
                self.s["queue"].append({"id": 99, "client_id": args[0], "scheduled_at": args[1]})
            return
        if "INSERT INTO notification_alerts" in sql:
            if self.s["alert_insert_raises"]:
                raise RuntimeError("alert write failed")
            self.s["alerts"].append(args[0])
            return
        raise AssertionError(sql)


def _pool(state: dict[str, Any]) -> MagicMock:
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=FakeConn(state))
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: acquired)
    return pool


def _state(**over: Any) -> dict[str, Any]:
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    base: dict[str, Any] = {
        "queue": [{"id": 1, "client_id": 7, "scheduled_at": past}],
        "client": dict(CLIENT),
        "already_sent": False,
        "lose_claim": False,
        "alerts": [],
        "alert_insert_raises": False,
        "audit_lost": False,  # log_email_attempt returns None, as when its insert fails
        "log": [],  # email_send_log rows
        "posts": 0,
        "payloads": [],
        "events": [],
        "paged": [],
        "raised": None,
    }
    base.update(over)
    return base


def _requeue_one(state: dict[str, Any]) -> None:
    """Hand the drain a fresh due row for the client, as the next cron tick would."""
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    state["queue"].append({"id": len(state["log"]) + 100, "client_id": 7, "scheduled_at": due})


def _wire(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any], post: Any) -> None:
    """``post`` is an async callable that returns a response or raises, like the email client."""

    def brochure_client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404)), **kwargs
        )

    async def counted_post(*args: Any, **kwargs: Any) -> Any:
        state["posts"] += 1
        state["payloads"].append(kwargs["json"])
        state["events"].append("post")
        return await post()

    async def log_attempt(pool: Any, **kw: Any) -> int | None:
        if state["audit_lost"]:
            return None
        state["events"].append("audit")
        row = {"id": len(state["log"]) + 1, "status": "sending", **kw}
        state["log"].append(row)
        return row["id"]

    def page(**kw: Any) -> None:
        state["paged"].append(kw)

    sender = SimpleNamespace(post=counted_post)
    monkeypatch.setattr(welcome, "httpx", SimpleNamespace(AsyncClient=brochure_client))
    monkeypatch.setattr(welcome, "_BROCHURE_PATH", Path("/nonexistent/brochure.pdf"))
    monkeypatch.setattr(welcome, "get_email_client", AsyncMock(return_value=sender))
    monkeypatch.setattr(welcome, "log_email_attempt", log_attempt)
    monkeypatch.setattr(welcome, "notify_email_failure_critical", page)


def _answers(status: int, body: Any = None, content: bytes | None = None) -> Any:
    async def post() -> httpx.Response:
        if content is not None:
            return httpx.Response(status, content=content, request=REQUEST)
        if body is None:
            return httpx.Response(status, content=b"", request=REQUEST)
        return httpx.Response(status, json=body, request=REQUEST)

    return post


def _raises(exc: BaseException) -> Any:
    async def post() -> Any:
        raise exc

    return post


async def _drain(state: dict[str, Any]) -> dict[str, int] | None:
    """One drain of the queue. An error escaping it is recorded, so the test asserts on the outcome."""
    try:
        return await welcome.process_pending_welcome_emails(_pool(state))
    except Exception as exc:  # noqa: BLE001 - the harness turns a crash into an assertion
        state["raised"] = exc
        return None


def _in_queue(state: dict[str, Any]) -> bool:
    return any(r["client_id"] == 7 for r in state["queue"])


def _assert_uncertain(state: dict[str, Any], stats: dict[str, int] | None) -> None:
    assert state["raised"] is None, repr(state["raised"])
    assert stats == {"processed": 1, "sent": 0, "skipped": 0, "failed": 1}
    assert state["posts"] == 1
    assert not _in_queue(state), "an uncertain send was put back in the queue: a possible duplicate"
    assert [r["status"] for r in state["log"]] == ["escalated"]  # audited and paged
    assert [p["email_type"] for p in state["paged"]] == ["welcome"]  # one page, no more
    assert state["alerts"] == []


def _assert_requeued(state: dict[str, Any], stats: dict[str, int] | None) -> None:
    assert state["raised"] is None, repr(state["raised"])
    assert stats == {"processed": 1, "sent": 0, "skipped": 0, "failed": 1}
    assert state["posts"] == 1
    assert _in_queue(state), "a send that surely did not go out was lost"
    assert [r["status"] for r in state["log"]] == ["failed"]
    assert state["alerts"] == []


# ── T1: the exception sweep, over the httpx that is installed ─────────────────


def _httpx_error_classes() -> list[type[BaseException]]:
    seen: dict[str, type[BaseException]] = {}

    def walk(cls: type[BaseException]) -> None:
        for sub in cls.__subclasses__():
            if sub.__name__ not in seen and sub.__module__.startswith("httpx"):
                seen[sub.__name__] = sub
                walk(sub)

    seen[httpx.HTTPError.__name__] = httpx.HTTPError
    walk(httpx.HTTPError)
    return [seen[name] for name in sorted(seen)]


def _build(cls: type[BaseException]) -> BaseException:
    """An instance of any httpx error class: they differ in what the constructor needs."""
    if issubclass(cls, httpx.HTTPStatusError):
        response = httpx.Response(500, request=REQUEST)
        return cls("status error", request=REQUEST, response=response)
    for make in (
        lambda: cls("boom", request=REQUEST),
        lambda: cls("boom"),
        lambda: cls(REQUEST),
    ):
        try:
            return make()
        except TypeError:
            continue
    raise AssertionError(f"cannot construct {cls!r}: teach _build how")


EXCEPTIONS = [
    *(pytest.param(cls, id=cls.__name__) for cls in _httpx_error_classes()),
    pytest.param(ValueError, id="ValueError"),
    pytest.param(RuntimeError, id="RuntimeError"),
    pytest.param(LocalFailure, id="LocalFailure"),
]


def test_the_sweep_sees_the_three_requeue_classes_and_the_ones_that_must_not() -> None:
    names = {cls.__name__ for cls in _httpx_error_classes()}
    assert {"ConnectError", "ConnectTimeout", "PoolTimeout"} <= names
    assert {"ReadTimeout", "WriteTimeout", "DecodingError", "RemoteProtocolError"} <= names
    assert len(names) >= 15  # the tree is not accidentally empty


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", EXCEPTIONS)
async def test_a_post_that_raises_is_requeued_only_when_nothing_was_written(
    monkeypatch: pytest.MonkeyPatch, cls: type[BaseException]
) -> None:
    state = _state()
    exc = cls("boom") if not issubclass(cls, httpx.HTTPError) else _build(cls)
    _wire(monkeypatch, state, _raises(exc))

    stats = await _drain(state)

    if issubclass(cls, PRE_WRITE):
        _assert_requeued(state, stats)
    else:
        _assert_uncertain(state, stats)


# ── T2: the status sweep ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [pytest.param(s, id=str(s)) for s in range(100, 600)])
async def test_only_a_refusing_status_is_requeued(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    state = _state()
    _wire(monkeypatch, state, _answers(status))  # an empty body

    stats = await _drain(state)

    if status in REFUSED:
        _assert_requeued(state, stats)
    else:
        # Including 200 with an empty (unreadable) body.
        _assert_uncertain(state, stats)


@pytest.mark.asyncio
async def test_a_200_success_is_sent_and_never_requeued(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    _wire(monkeypatch, state, _answers(200, SENT_BODY))

    stats = await _drain(state)

    assert state["raised"] is None, repr(state["raised"])
    assert stats == {"processed": 1, "sent": 1, "skipped": 0, "failed": 0}
    assert not _in_queue(state)
    assert [r["status"] for r in state["log"]] == ["sent"]
    assert state["paged"] == []
    assert state["alerts"] == [7]


@pytest.mark.asyncio
async def test_a_200_that_says_not_delivered_is_requeued(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    _wire(monkeypatch, state, _answers(200, NOT_SENT_BODY))

    stats = await _drain(state)

    _assert_requeued(state, stats)
    assert [p["email_type"] for p in state["paged"]] == ["welcome"]


@pytest.mark.asyncio
async def test_a_200_that_says_delivery_uncertain_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    _wire(monkeypatch, state, _answers(200, UNCERTAIN_BODY))

    _assert_uncertain(state, await _drain(state))


@pytest.mark.asyncio
async def test_a_keyed_brevo_read_timeout_stays_uncertain_and_is_not_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real endpoint preserves a lost provider answer for the welcome sender."""
    key = UUID("12345678-1234-5678-1234-567812345678")
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ReadTimeout("lost answer", request=REQUEST))
    monkeypatch.setenv("SENDGRID_API_KEY", "xkeysib-synthetic-test-key")
    monkeypatch.setattr(notifications_router, "get_email_client", AsyncMock(return_value=client))

    endpoint_answer = await notifications_router.send_direct_email(
        notifications_router.SendEmailRequest(
            to="client@example.test",
            subject="Welcome",
            body="Synthetic welcome",
            idempotency_key=key,
        )
    )

    assert endpoint_answer.success is False
    assert endpoint_answer.delivery_uncertain is True

    state = _state()
    _wire(monkeypatch, state, _answers(200, endpoint_answer.model_dump()))
    _assert_uncertain(state, await _drain(state))
    namespace = getattr(welcome, "WELCOME_IDEMPOTENCY_NAMESPACE", None)
    assert namespace is not None, "no welcome idempotency namespace"
    assert state["payloads"][0].get("idempotency_key") == str(uuid5(namespace, "welcome:1"))


@pytest.mark.asyncio
async def test_a_keyed_brevo_rate_limit_stays_uncertain_and_is_not_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=httpx.Response(429, request=REQUEST))
    monkeypatch.setenv("SENDGRID_API_KEY", "xkeysib-synthetic-test-key")
    monkeypatch.setattr(notifications_router, "get_email_client", AsyncMock(return_value=client))

    endpoint_answer = await notifications_router.send_direct_email(
        notifications_router.SendEmailRequest(
            to="client@example.test",
            subject="Welcome",
            body="Synthetic welcome",
            idempotency_key=UUID("12345678-1234-5678-1234-567812345678"),
        )
    )

    assert endpoint_answer.success is False
    assert endpoint_answer.delivery_uncertain is True
    state = _state()
    _wire(monkeypatch, state, _answers(200, endpoint_answer.model_dump()))
    _assert_uncertain(state, await _drain(state))


@pytest.mark.asyncio
async def test_each_welcome_attempt_uses_its_audit_row_for_a_stable_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    _wire(monkeypatch, state, _answers(422))

    await _drain(state)

    namespace = getattr(welcome, "WELCOME_IDEMPOTENCY_NAMESPACE", None)
    assert namespace is not None, "no welcome idempotency namespace"
    assert state["payloads"][0].get("idempotency_key") == str(uuid5(namespace, "welcome:1"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"<html>ok</html>", id="html"),
        pytest.param(b"OK", id="text"),
        pytest.param(b"[1, 2]", id="json list"),
        pytest.param(b"{}", id="object without a success flag"),
        pytest.param(b'{"success": "yes"}', id="success is not a boolean"),
        pytest.param(b"null", id="json null"),
    ],
)
async def test_a_200_that_does_not_say_sent_or_not_sent_is_uncertain(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    state = _state()
    _wire(monkeypatch, state, _answers(200, content=content))

    _assert_uncertain(state, await _drain(state))


@pytest.mark.asyncio
async def test_a_refusal_whose_body_cannot_be_read_is_still_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Unreadable:
        status_code = 422

        @property
        def text(self) -> str:
            raise httpx.DecodingError("bad body")

    async def post() -> Any:
        return Unreadable()

    state = _state()
    _wire(monkeypatch, state, post)

    _assert_requeued(state, await _drain(state))


# ── T3: the POST returned, then reading the answer fails ─────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [pytest.param(c, id=c.__name__) for c in _httpx_error_classes()])
async def test_an_answer_that_cannot_be_read_after_the_post_is_uncertain(
    monkeypatch: pytest.MonkeyPatch, cls: type[BaseException]
) -> None:
    class Unreadable:
        status_code = 200

        def json(self) -> Any:
            raise _build(cls)

    async def post() -> Any:
        return Unreadable()

    state = _state()
    _wire(monkeypatch, state, post)

    _assert_uncertain(state, await _drain(state))


@pytest.mark.asyncio
async def test_a_decoding_error_reading_the_body_text_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Unreadable:
        status_code = 500

        @property
        def text(self) -> str:
            raise httpx.DecodingError("bad body", request=REQUEST)

    async def post() -> Any:
        return Unreadable()

    state = _state()
    _wire(monkeypatch, state, post)

    _assert_uncertain(state, await _drain(state))


@pytest.mark.asyncio
async def test_an_error_escaping_the_sender_is_not_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    _wire(monkeypatch, state, _answers(200, SENT_BODY))
    monkeypatch.setattr(welcome, "_send_client_welcome_impl", AsyncMock(side_effect=RuntimeError))

    stats = await _drain(state)

    assert state["raised"] is None
    assert stats == {"processed": 1, "sent": 0, "skipped": 0, "failed": 1}
    assert not _in_queue(state)  # nothing proves the mail did not go out


# ── T4: no audit row, no POST ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_lost_audit_row_means_no_post_and_the_row_goes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(audit_lost=True)
    _wire(monkeypatch, state, _answers(200, SENT_BODY))

    stats = await _drain(state)

    assert state["raised"] is None, repr(state["raised"])
    assert state["posts"] == 0
    assert _in_queue(state)
    assert state["paged"] == []
    assert state["alerts"] == []
    assert stats == {"processed": 1, "sent": 0, "skipped": 0, "failed": 1}


@pytest.mark.asyncio
async def test_four_runs_with_a_lost_audit_row_make_no_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(audit_lost=True)
    _wire(monkeypatch, state, _answers(200, SENT_BODY))

    for _ in range(4):
        await _drain(state)
        assert _in_queue(state)

    assert state["raised"] is None, repr(state["raised"])
    assert state["posts"] == 0
    assert state["log"] == []


@pytest.mark.asyncio
async def test_the_lost_audit_line_is_the_closed_form_and_names_no_address(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    state = _state(audit_lost=True)
    _wire(monkeypatch, state, _answers(200, SENT_BODY))

    with caplog.at_level("WARNING", logger=welcome.logger.name):
        await _drain(state)

    lines = [r.getMessage() for r in caplog.records if "no audit row" in r.getMessage()]
    assert lines == ["WelcomeEmail: no audit row for client 7, nothing sent, queued again"]
    assert "example.test" not in caplog.text


# ── T5: the cap ───────────────────────────────────────────────────────────────


def _seed(state: dict[str, Any], *statuses: str) -> None:
    for status in statuses:
        state["log"].append(
            {"id": len(state["log"]) + 1, "status": status, "email_type": "welcome", "client_id": 7}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior", [("failed", "failed", "failed"), ("failed", "escalated", "sending")]
)
async def test_the_cap_stops_before_a_new_audit_or_post(
    monkeypatch: pytest.MonkeyPatch, prior: tuple[str, ...], caplog: pytest.LogCaptureFixture
) -> None:
    state = _state()
    _seed(state, *prior)
    _wire(monkeypatch, state, _answers(422))

    with caplog.at_level("INFO", logger=welcome.logger.name):
        stats = await _drain(state)

    assert stats == {"processed": 1, "sent": 0, "skipped": 0, "failed": 1}
    assert state["posts"] == 0
    assert len(state["log"]) == 3
    assert not _in_queue(state)
    assert [r.getMessage() for r in caplog.records if "cap reached" in r.getMessage()] == [
        "WelcomeEmail: welcome cap reached before send"
    ]
    assert "example.test" not in caplog.text


@pytest.mark.asyncio
async def test_two_prior_attempts_check_the_cap_before_one_new_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    _seed(state, "failed", "sending")
    _wire(monkeypatch, state, _answers(422))

    await _drain(state)

    assert state["posts"] == 1
    assert state["events"][:3] == ["cap", "audit", "post"]


@pytest.mark.asyncio
async def test_an_uncertain_attempt_is_not_selected_for_a_second_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.services.notifications.email_health_monitor as health_monitor
    from backend.services.notifications.email_health_monitor import EmailHealthMonitor

    state = _state()
    _wire(monkeypatch, state, _answers(200, UNCERTAIN_BODY))
    monitor_page = MagicMock()
    monkeypatch.setattr(health_monitor, "_post_telegram", monitor_page)

    _assert_uncertain(state, await _drain(state))
    stats = await EmailHealthMonitor(_pool(state)).escalate_unrecoverable()

    assert [row["status"] for row in state["log"]] == ["escalated"]
    assert len(state["paged"]) == 1
    assert stats == {"escalated": 0}
    monitor_page.assert_not_called()


@pytest.mark.asyncio
async def test_the_cap_is_three_definite_failures_then_the_row_stays_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert getattr(welcome, "WELCOME_MAX_FAILED_ATTEMPTS", None) == 3
    state = _state()
    _wire(monkeypatch, state, _answers(422))

    runs = 0
    while _in_queue(state) and runs < 10:
        await _drain(state)
        runs += 1

    assert state["raised"] is None, repr(state["raised"])
    assert state["posts"] == 3
    assert not _in_queue(state)
    assert [r["status"] for r in state["log"]] == ["failed"] * 3


@pytest.mark.asyncio
async def test_an_uncertain_attempt_counts_toward_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    answers = iter([_answers(200, UNCERTAIN_BODY)] + [_answers(422)] * 10)

    async def post() -> Any:
        return await next(answers)()

    _wire(monkeypatch, state, post)

    await _drain(state)  # the uncertain send: audited, never requeued
    assert not _in_queue(state)
    _requeue_one(state)  # a later flow queues the client again
    runs = 0
    while _in_queue(state) and runs < 10:
        await _drain(state)
        runs += 1

    # One uncertain + two refusals are three audited non-sent attempts: the cap stops there.
    # Were the uncertain one not counted, a fourth POST would follow.
    assert state["raised"] is None, repr(state["raised"])
    assert state["posts"] == 3
    assert [r["status"] for r in state["log"]] == ["escalated", "failed", "failed"]
    assert not _in_queue(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior", "requeued"),
    [
        pytest.param(("failed",), True, id="1 prior failed + this one = 2"),
        pytest.param(("failed", "failed"), False, id="2 prior failed + this one = 3"),
        pytest.param(("escalated", "escalated"), False, id="escalated rows count"),
        pytest.param(("failed", "escalated"), False, id="failed and escalated"),
        pytest.param(("sent", "failed"), True, id="a sent row is not a failed attempt"),
        pytest.param(("skipped_idempotent", "failed"), True, id="a skipped row is no attempt"),
    ],
)
async def test_the_cap_counts_the_audited_attempts_that_did_not_end_sent(
    monkeypatch: pytest.MonkeyPatch, prior: tuple[str, ...], requeued: bool
) -> None:
    state = _state()
    _seed(state, *prior)
    # A prior 'sent' row would stop the send at the idempotency check: audit it for another client.
    for row in state["log"]:
        if row["status"] == "sent":
            row["client_id"] = 8
    _wire(monkeypatch, state, _answers(422))

    await _drain(state)

    assert state["raised"] is None, repr(state["raised"])
    assert state["posts"] == 1
    assert _in_queue(state) is requeued


# ── T6: a delivered mail stays delivered ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_lost_alert_row_after_a_good_post_stays_sent_and_is_not_posted_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(alert_insert_raises=True)
    _wire(monkeypatch, state, _answers(200, SENT_BODY))

    first = await _drain(state)

    assert state["raised"] is None, repr(state["raised"])
    assert first == {"processed": 1, "sent": 1, "skipped": 0, "failed": 0}
    assert not _in_queue(state)
    assert [r["status"] for r in state["log"]] == ["sent"]
    assert state["paged"] == []

    _requeue_one(state)
    second = await _drain(state)

    assert second == {"processed": 1, "sent": 0, "skipped": 1, "failed": 0}
    assert state["posts"] == 1


# ── unchanged behaviour ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"client": None}, id="client missing"),
        pytest.param({"client": {**CLIENT, "email": " "}}, id="no email"),
        pytest.param({"already_sent": True}, id="already sent"),
    ],
)
async def test_a_skipped_welcome_is_counted_skipped_and_not_requeued(
    monkeypatch: pytest.MonkeyPatch, over: dict[str, Any]
) -> None:
    state = _state(**over)
    _wire(monkeypatch, state, _answers(200, SENT_BODY))

    stats = await _drain(state)

    assert stats == {"processed": 1, "sent": 0, "skipped": 1, "failed": 0}
    assert state["posts"] == 0
    assert not _in_queue(state)


@pytest.mark.asyncio
async def test_a_worker_that_loses_the_claim_skips_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(lose_claim=True)
    _wire(monkeypatch, state, _answers(422))

    stats = await _drain(state)

    assert stats == {"processed": 1, "sent": 0, "skipped": 1, "failed": 0}
    assert state["posts"] == 0
    assert len(state["queue"]) == 1  # the other worker's row is untouched
