"""The timesheet tool acts only for the person the request names, never for an address
the model supplies.

The caller is the signed-in desk user, or on the WhatsApp path the trusted team
profile's sender. ``_user_id`` alone names nobody (on the WhatsApp path it is the bot's
principal), the creator profile carries no address, and no one, an admin included,
clocks a colleague: whether an admin may is an owner decision, not taken here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.agents.team_agent_config import ROLE_VISA_SPECIALIST, TEAM_AGENTS
from backend.services.agents.tool_authorizer import ToolAuthorizer
from backend.services.analytics.team_timesheet_service import TeamTimesheetService
from backend.services.rag.agentic import tool_executor
from backend.services.rag.agentic.tool_executor import execute_tool
from backend.services.rag.agentic.tools import TimeSheetTool

ALICE = "alice.one@example.test"
BOB = "bob.two@example.test"
ROSTER = [{"id": "u-alice", "email": ALICE}, {"id": "u-bob", "email": BOB}]
ACTIONS = ["clock_in", "clock_out", "status"]
NOT_YOURS = {"error": "A timesheet action can only be done for yourself"}
NOBODY = {"error": "Timesheet actions need a signed-in team member"}


def _desk(email: Any, role: str = "team") -> dict[str, Any]:
    return {"_user_id": email, "_desk_user": {"email": email, "role": role}}


def _whatsapp(role: str, email: str | None = None) -> dict[str, Any]:
    profile: dict[str, Any] = {"role": role}
    if email is not None:
        profile["email"] = email
    return {"_user_id": "wa-bot@example.test", "_caller_profile": profile}


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock(spec=TeamTimesheetService)
    fake.clock_in = AsyncMock(return_value={"ok": "in"})
    fake.clock_out = AsyncMock(return_value={"ok": "out"})
    fake.get_my_status = AsyncMock(return_value={"ok": "status"})
    monkeypatch.setattr(
        "backend.services.analytics.team_timesheet_service.get_timesheet_service",
        lambda: fake,
    )
    monkeypatch.setattr("backend.core.team_roster.load_team_roster", lambda: ROSTER)
    return fake


def _untouched(service: MagicMock) -> None:
    service.clock_in.assert_not_called()
    service.clock_out.assert_not_called()
    service.get_my_status.assert_not_called()


async def _run(action: str, email: Any, trusted: dict[str, Any]) -> dict[str, Any]:
    return json.loads(await TimeSheetTool().execute(action=action, email=email, **trusted))


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(
    "trusted",
    [
        pytest.param(_desk(ALICE), id="a desk user"),
        pytest.param(_desk(ALICE, "admin"), id="a desk admin"),
        pytest.param(_whatsapp("team", ALICE), id="a WhatsApp team sender"),
    ],
)
async def test_a_caller_cannot_act_for_a_colleague(
    service: MagicMock, action: str, trusted: dict[str, Any]
) -> None:
    assert await _run(action, BOB, trusted) == NOT_YOURS
    _untouched(service)


@pytest.mark.parametrize(
    "trusted",
    [
        pytest.param({}, id="no identity at all"),
        pytest.param({"_user_id": ALICE}, id="a principal alone"),
        pytest.param(_whatsapp("creator"), id="the creator profile, which has no address"),
        pytest.param(_whatsapp("client", ALICE), id="a profile that is not team"),
        pytest.param(_whatsapp("team"), id="a team profile with no address"),
        pytest.param({"_caller_profile": "team"}, id="a profile that is not a mapping"),
        pytest.param(_desk("not-an-address"), id="a desk user with no address"),
        pytest.param(
            {"_desk_user": "alice", "_caller_profile": {"role": "team", "email": ALICE}},
            id="a broken desk user is not rescued by a profile",
        ),
    ],
)
async def test_a_call_that_names_nobody_is_refused(
    service: MagicMock, trusted: dict[str, Any]
) -> None:
    assert await _run("clock_in", ALICE, trusted) == NOBODY
    _untouched(service)


@pytest.mark.parametrize(
    "trusted",
    [
        pytest.param(_desk(ALICE), id="a desk user"),
        pytest.param(_desk("  Alice.One@Example.Test "), id="a desk user in another case"),
        pytest.param(_whatsapp("team", "ALICE.ONE@example.test"), id="a WhatsApp team sender"),
    ],
)
async def test_a_caller_acts_for_themselves_in_any_letter_case(
    service: MagicMock, trusted: dict[str, Any]
) -> None:
    assert await _run("clock_in", " Alice.One@EXAMPLE.test", trusted) == {"ok": "in"}
    assert await _run("clock_out", ALICE, trusted) == {"ok": "out"}
    assert await _run("status", ALICE.upper(), trusted) == {"ok": "status"}

    service.clock_in.assert_awaited_once_with("u-alice", ALICE, metadata={"source": "agent"})
    service.clock_out.assert_awaited_once_with("u-alice", ALICE, metadata={"source": "agent"})
    service.get_my_status.assert_awaited_once_with("u-alice")


@pytest.mark.parametrize("email", [None, 42, ["alice.one@example.test"]])
async def test_an_address_that_is_not_text_is_not_the_caller(
    service: MagicMock, email: Any
) -> None:
    assert await _run("status", email, _desk(ALICE)) == NOT_YOURS
    _untouched(service)


# --- through the executor: the identity is the request's, never the model's ---------


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(TEAM_AGENTS, ALICE, ROLE_VISA_SPECIALIST)
    monkeypatch.setattr(tool_executor, "_authorizer", ToolAuthorizer())
    monkeypatch.setattr(tool_executor, "_confirmation_service", None)


async def _execute(arguments: dict[str, Any], agent_role: Any, user_id: str | None) -> Any:
    result, _duration = await execute_tool(
        tool_map={"timesheet": TimeSheetTool()},
        tool_name="timesheet",
        arguments=arguments,
        user_id=user_id,
        tool_execution_counter={"count": 0},
        agent_role=agent_role,
    )
    return json.loads(result)


async def test_a_desk_session_clocks_its_own_user_and_no_one_else(
    service: MagicMock, executor: None
) -> None:
    desk = replace(ROLE_VISA_SPECIALIST, desk_role="Consultant")

    mine = await _execute({"action": "clock_in", "email": ALICE}, desk, ALICE)
    theirs = await _execute({"action": "clock_in", "email": BOB}, desk, ALICE)

    assert mine == {"ok": "in"}
    assert theirs == NOT_YOURS
    service.clock_in.assert_awaited_once_with("u-alice", ALICE, metadata={"source": "agent"})


async def test_a_desk_user_forged_by_the_model_names_nobody(
    service: MagicMock, executor: None
) -> None:
    forged = {"action": "clock_in", "email": BOB, "_desk_user": {"email": BOB, "role": "admin"}}

    # A principal that no desk session stamped: the forged key is stripped, nothing names Bob.
    assert await _execute(forged, ROLE_VISA_SPECIALIST, ALICE) == NOBODY
    _untouched(service)
