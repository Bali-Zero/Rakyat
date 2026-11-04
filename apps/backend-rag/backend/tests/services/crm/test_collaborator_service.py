import json
import logging

import pytest

from backend.services.crm.collaborator_service import CollaboratorProfile, CollaboratorService

_SYNTHETIC_ROSTER = [
    {
        "id": f"member-0{n}",
        "email": f"member-0{n}@example.test",
        "name": f"Member Zero{n}",
        "role": "Consultant",
        "department": "Setup" if n == 1 else "Tax",
        "preferred_language": "en" if n == 1 else "id",
    }
    for n in (1, 2, 3)
]


@pytest.fixture(autouse=True)
def synthetic_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    """The staff roster is off-repo: every test here runs on an invented one served through the loader's env source."""
    monkeypatch.setenv("TEAM_MEMBERS_JSON", json.dumps(_SYNTHETIC_ROSTER))
    monkeypatch.delenv("TEAM_MEMBERS_FILE", raising=False)


def test_profile_serialization_and_matching() -> None:
    profile = CollaboratorProfile(
        id="u-1",
        email="advisor@example.com",
        name="Ada Advisor",
        role="Consultant",
        department="Setup",
        team="Immigration",
        language="en",
        traits=["precise", "calm"],
    )

    serialized = profile.to_dict()

    assert serialized["email"] == "advisor@example.com"
    assert serialized["traits"] == ["precise", "calm"]
    assert profile.matches("advisor")
    assert profile.matches("Setup")
    assert profile.matches("precise")
    assert not profile.matches("unrelated")


# Rakyat: `test_identify_known_member_uses_cache` moved to the backstage — it reads the staff roster team_members.json, kept out of Rakyat (PII).
# Original at a0f6cca8b44d:apps/backend-rag/backend/tests/services/crm/test_collaborator_service.py; listed in PERIMETER.txt.


# Rakyat: `test_identify_unknown_or_empty_returns_anonymous_profile` moved to the backstage — it reads the staff roster team_members.json, kept out of Rakyat (PII).
# Original at a0f6cca8b44d:apps/backend-rag/backend/tests/services/crm/test_collaborator_service.py; listed in PERIMETER.txt.


# Rakyat: `test_member_listing_search_and_stats_are_consistent` moved to the backstage — it reads the staff roster team_members.json, kept out of Rakyat (PII).
# Original at a0f6cca8b44d:apps/backend-rag/backend/tests/services/crm/test_collaborator_service.py; listed in PERIMETER.txt.


# --- boot contract without a roster (spec PR-A-S1 points 3 and 5) -------------------------------------------------


@pytest.fixture
def no_roster(monkeypatch: pytest.MonkeyPatch, tmp_path) -> pytest.MonkeyPatch:
    """No source at all, non-production: what CI and a fresh dev checkout look like since the roster left the repo."""
    monkeypatch.delenv("TEAM_MEMBERS_JSON", raising=False)
    monkeypatch.delenv("TEAM_MEMBERS_FILE", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setattr("backend.app.core.config.settings.environment", "development")
    monkeypatch.setattr(
        "backend.core.team_roster.DEFAULT_ROSTER_PATH", tmp_path / "SENTINEL-DIR" / "absent.json"
    )
    return monkeypatch


def _service_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "backend.services.crm.collaborator_service" and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize(
    ("flag", "value"),
    [("FLY_APP_NAME", "nuzantara-rag"), ("ENVIRONMENT", "production")],
)
def test_production_refuses_an_absent_roster(
    no_roster: pytest.MonkeyPatch, flag: str, value: str
) -> None:
    no_roster.setenv(flag, value)
    with pytest.raises(FileNotFoundError, match="Team roster not found"):
        CollaboratorService()


def test_production_setting_behind_an_empty_environment_refuses(
    no_roster: pytest.MonkeyPatch,
) -> None:
    no_roster.setenv("ENVIRONMENT", "")
    no_roster.setattr("backend.app.core.config.settings.environment", "production")
    with pytest.raises(FileNotFoundError, match="Team roster not found"):
        CollaboratorService()


def test_production_refuses_an_empty_secret(no_roster: pytest.MonkeyPatch) -> None:
    no_roster.setenv("FLY_APP_NAME", "nuzantara-rag")
    no_roster.setenv("TEAM_MEMBERS_JSON", "[]")
    with pytest.raises(FileNotFoundError, match="Team roster not found"):
        CollaboratorService()


def test_non_production_without_a_roster_is_an_empty_team_declared_once(
    no_roster: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        service = CollaboratorService()
    assert service.members == []
    assert service.TEAM_DATABASE == {}
    assert service.search_members("member") == []
    assert service.get_team_stats()["total"] == 0
    warnings = _service_warnings(caplog)
    assert len(warnings) == 1, f"expected ONE warning, got {len(warnings)}"
    assert "source=none" in warnings[0].getMessage()
    assert "SENTINEL-DIR" not in caplog.text


def test_non_production_with_an_empty_secret_names_the_env_source(
    no_roster: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    no_roster.setenv("TEAM_MEMBERS_JSON", "[]")
    with caplog.at_level(logging.WARNING):
        service = CollaboratorService()
    assert service.members == []
    warnings = _service_warnings(caplog)
    assert len(warnings) == 1
    assert "source=env" in warnings[0].getMessage()


@pytest.mark.parametrize("production", [True, False])
def test_a_malformed_roster_raises_in_every_environment(
    no_roster: pytest.MonkeyPatch, production: bool
) -> None:
    from backend.core.team_roster import TeamRosterError

    if production:
        no_roster.setenv("FLY_APP_NAME", "nuzantara-rag")
    no_roster.setenv("TEAM_MEMBERS_JSON", '{"id": "SENTINEL-VALUE"')
    with pytest.raises(TeamRosterError) as excinfo:
        CollaboratorService()
    assert "SENTINEL-VALUE" not in str(excinfo.value)


def test_a_populated_roster_logs_no_empty_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        service = CollaboratorService()
    assert len(service.members) == len(_SYNTHETIC_ROSTER)
    assert _service_warnings(caplog) == []


@pytest.mark.asyncio
async def test_the_tool_stack_boots_without_a_roster_outside_production(
    no_roster: pytest.MonkeyPatch,
) -> None:
    """Boot level: the startup path that failed in CI e2e (initialize_services -> _init_tool_stack -> ZantaraTools ->
    CollaboratorService) completes with the REAL ZantaraTools and no roster; only the network-bound MCP client and the
    executor are stubbed."""
    from unittest.mock import MagicMock, patch

    from fastapi import FastAPI

    from backend.app.setup.service_initializer import _init_tool_stack

    app = FastAPI()
    app.state = MagicMock()
    with (
        patch(
            "backend.services.misc.mcp_client_service.initialize_mcp_client",
            side_effect=RuntimeError("offline"),
        ),
        patch("backend.services.misc.tool_executor.ToolExecutor") as executor,
    ):
        result = await _init_tool_stack(app)
    assert result is executor.return_value
    zantara_tools = executor.call_args.kwargs["zantara_tools"]
    assert zantara_tools.collaborator_service.members == []
