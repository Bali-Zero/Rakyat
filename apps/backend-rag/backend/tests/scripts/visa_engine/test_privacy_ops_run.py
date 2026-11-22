"""``privacy_ops.run``: a hold or a release that changed nothing does not exit zero.

The legal-hold capability answers false both for a decision id that matches no row and for a
decision already in the requested state. The script logged that answer and exited 0 either
way, so a mistyped id, or the id of another environment, looked like a hold that was now in
place, and the decision it was meant to protect stayed open to the retention purge. The erase
action already exits 2 when it deleted nothing; hold and release now do the same.

No database: the pool, the operator-boundary check and the two capabilities are recording
stand-ins, so every case also says what was called and that the pool was closed.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import pytest

from backend.scripts.visa_engine import privacy_ops

DECISION = "11111111-1111-4111-8111-111111111111"
DSN = "postgresql://zz-privacy.invalid/zz"


class _Ceremony:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.hold_answer: Any = True
        self.erased = 1
        self.boundary_error: BaseException | None = None
        self.hold_calls: list[dict[str, Any]] = []
        ceremony = self

        class _Pool:
            async def close(self) -> None:
                ceremony.events.append("close")

        async def create_pool(url: str, **_: Any) -> _Pool:
            assert url == DSN
            ceremony.events.append("open")
            return _Pool()

        async def boundary(_pool: _Pool, *, function_signature: str) -> None:
            ceremony.events.append("boundary " + function_signature.split("(")[0])
            if ceremony.boundary_error is not None:
                raise ceremony.boundary_error

        async def hold(_pool: _Pool, **kwargs: Any) -> Any:
            ceremony.events.append("hold capability")
            ceremony.hold_calls.append(kwargs)
            return ceremony.hold_answer

        async def erase(_pool: _Pool, **_: Any) -> int:
            ceremony.events.append("erase capability")
            return ceremony.erased

        monkeypatch.setenv(privacy_ops.PRIVACY_OPERATOR_DSN_ENV, DSN)
        monkeypatch.setattr(privacy_ops.asyncpg, "create_pool", create_pool)
        monkeypatch.setattr(privacy_ops, "_assert_operator_boundary", boundary)
        monkeypatch.setattr(privacy_ops.retention, "set_decision_legal_hold", hold)
        monkeypatch.setattr(privacy_ops.retention, "erase_decision_for_dsr", erase)

    @staticmethod
    def args(action: str, *extra: str) -> Any:
        """What the operator types, through the script's own parser."""
        governance = (
            ["--reason-code", "ZZ-LEGAL-CLAIM", "--approved-by", "zz.approver"]
            if action != "erase"
            else []
        )
        due = ["--review-due-at", "2027-01-04T00:00:00+08:00"] if action == "hold" else []
        return privacy_ops._parse_args(
            [action, "--decision-id", DECISION, "--case-reference", "ZZ-2026-001",
             "--actor", "zz.operator", *governance, *due, *extra]
        )  # fmt: skip


@pytest.fixture
def ceremony(monkeypatch: pytest.MonkeyPatch) -> _Ceremony:
    return _Ceremony(monkeypatch)


_HOLD_EVENTS = ["open", "boundary public.set_visa_decision_legal_hold", "hold capability", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hold", "release"])
async def test_a_hold_or_a_release_that_changed_the_decision_exits_zero(
    ceremony: _Ceremony, action: str
) -> None:
    assert await privacy_ops.run(ceremony.args(action, "--apply")) == 0

    assert ceremony.events == _HOLD_EVENTS
    (call,) = ceremony.hold_calls
    assert call["decision_id"] == UUID(DECISION)
    assert call["legal_hold"] is (action == "hold")
    assert (call["review_due_at"] is None) is (action == "release")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hold", "release"])
@pytest.mark.parametrize("answer", [False, None], ids=["false", "no answer"])
async def test_a_hold_or_a_release_that_changed_nothing_exits_two_and_says_so(
    ceremony: _Ceremony, caplog: pytest.LogCaptureFixture, action: str, answer: Any
) -> None:
    ceremony.hold_answer = answer

    with caplog.at_level(logging.INFO, logger="visa_engine.privacy_ops"):
        assert await privacy_ops.run(ceremony.args(action, "--apply")) == 2

    assert ceremony.events == _HOLD_EVENTS
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [
        f"privacy_operation_result action={action} no_change=true: the decision is absent or "
        "already in that state; verify it before relying on this run"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("deleted", "code"), [(1, 0), (0, 2)], ids=["one row", "no row"])
async def test_an_erase_exits_zero_only_for_the_one_row_it_deleted(
    ceremony: _Ceremony, deleted: int, code: int
) -> None:
    ceremony.erased = deleted

    assert await privacy_ops.run(ceremony.args("erase", "--apply")) == code

    assert ceremony.events == [
        "open",
        "boundary public.erase_visa_decision_for_dsr",
        "erase capability",
        "close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hold", "release", "erase"])
async def test_without_apply_nothing_is_opened_and_the_run_exits_zero(
    ceremony: _Ceremony, action: str
) -> None:
    assert await privacy_ops.run(ceremony.args(action)) == 0

    assert ceremony.events == []


@pytest.mark.asyncio
async def test_apply_without_the_operator_database_url_stops_before_any_pool(
    ceremony: _Ceremony, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(privacy_ops.PRIVACY_OPERATOR_DSN_ENV)

    with pytest.raises(RuntimeError, match="is required with --apply"):
        await privacy_ops.run(ceremony.args("hold", "--apply"))

    assert ceremony.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hold", "release", "erase"])
async def test_a_refused_operator_boundary_calls_no_capability_and_closes_the_pool(
    ceremony: _Ceremony, action: str
) -> None:
    ceremony.boundary_error = RuntimeError("zz: not the privacy operator")

    with pytest.raises(RuntimeError, match="not the privacy operator"):
        await privacy_ops.run(ceremony.args(action, "--apply"))

    assert ceremony.events[0] == "open" and ceremony.events[-1] == "close"
    assert not [event for event in ceremony.events if event.endswith("capability")]
