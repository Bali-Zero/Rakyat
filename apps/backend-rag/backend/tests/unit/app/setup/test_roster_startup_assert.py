"""Staff roster off-repo: production refuses to boot without the TEAM_MEMBERS_JSON secret."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.app.setup.app_factory import _assert_roster_source

_DOC = json.dumps([{"id": "a", "role": "admin"}, {"id": "b", "role": "Founder"}])


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("TEAM_MEMBERS_JSON", "TEAM_MEMBERS_FILE", "FLY_APP_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")
    return monkeypatch


def _app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


def test_production_boots_from_the_env_secret(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("FLY_APP_NAME", "nuzantara-rag")
    clean_env.setenv("TEAM_MEMBERS_JSON", _DOC)
    app = _app()
    _assert_roster_source(app)
    assert (app.state.team_roster_count, app.state.team_roster_source) == (2, "env")


@pytest.mark.parametrize("flag", [("FLY_APP_NAME", "nuzantara-rag"), ("ENVIRONMENT", "production")])
def test_production_refuses_without_the_env_secret(clean_env: pytest.MonkeyPatch, flag: tuple[str, str]) -> None:
    clean_env.setenv(*flag)
    with pytest.raises(RuntimeError, match="TEAM_MEMBERS_JSON is not set"):
        _assert_roster_source(_app())


def test_production_refuses_a_file_source(clean_env: pytest.MonkeyPatch, tmp_path) -> None:
    roster = tmp_path / "roster.json"
    roster.write_text(_DOC)
    clean_env.setenv("FLY_APP_NAME", "nuzantara-rag")
    clean_env.setenv("TEAM_MEMBERS_FILE", str(roster))
    with pytest.raises(RuntimeError, match="TEAM_MEMBERS_JSON is not set"):
        _assert_roster_source(_app())


def test_production_refuses_an_empty_or_malformed_secret(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("FLY_APP_NAME", "nuzantara-rag")
    clean_env.setenv("TEAM_MEMBERS_JSON", "[]")
    with pytest.raises(RuntimeError, match="empty in production"):
        _assert_roster_source(_app())
    clean_env.setenv("TEAM_MEMBERS_JSON", "{not json")
    with pytest.raises(RuntimeError, match="unreadable in production"):
        _assert_roster_source(_app())


def test_the_refusal_never_carries_roster_content(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("FLY_APP_NAME", "nuzantara-rag")
    clean_env.setenv("TEAM_MEMBERS_JSON", '{"id": "SENTINEL-VALUE"')
    with pytest.raises(RuntimeError) as excinfo:
        _assert_roster_source(_app())
    assert "SENTINEL-VALUE" not in str(excinfo.value)
    assert "SENTINEL-VALUE" not in str(excinfo.value.__cause__)


def test_development_and_test_boot_without_a_roster(clean_env: pytest.MonkeyPatch, tmp_path) -> None:
    clean_env.setattr("backend.core.team_roster.DEFAULT_ROSTER_PATH", tmp_path / "absent.json")
    app = _app()
    _assert_roster_source(app)
    assert (app.state.team_roster_count, app.state.team_roster_source) == (0, "none")


@pytest.mark.parametrize("blank", ["   ", "\n\t"])
def test_blank_secret_with_a_file_roster_is_not_the_env_source(clean_env: pytest.MonkeyPatch, tmp_path, blank: str) -> None:
    """ADV-1/ADV-2: the loader strips a blank TEAM_MEMBERS_JSON and falls through to a file; production must refuse."""
    roster = tmp_path / "roster.json"
    roster.write_text(_DOC)
    clean_env.setenv("FLY_APP_NAME", "nuzantara-rag")
    clean_env.setenv("TEAM_MEMBERS_JSON", blank)
    clean_env.setenv("TEAM_MEMBERS_FILE", str(roster))
    with pytest.raises(RuntimeError, match="TEAM_MEMBERS_JSON is not set"):
        _assert_roster_source(_app())
    clean_env.delenv("TEAM_MEMBERS_FILE")
    clean_env.setattr("backend.core.team_roster.DEFAULT_ROSTER_PATH", roster)
    with pytest.raises(RuntimeError, match="TEAM_MEMBERS_JSON is not set"):
        _assert_roster_source(_app())


def test_empty_environment_variable_does_not_mask_a_production_setting(clean_env: pytest.MonkeyPatch, tmp_path) -> None:
    """ADV-3: ENVIRONMENT="" is present in os.environ; settings.environment == production must still count."""
    clean_env.setenv("ENVIRONMENT", "")
    clean_env.setattr("backend.app.setup.app_factory.settings.environment", "production")
    clean_env.setattr("backend.core.team_roster.DEFAULT_ROSTER_PATH", tmp_path / "absent.json")
    with pytest.raises(RuntimeError, match="TEAM_MEMBERS_JSON is not set"):
        _assert_roster_source(_app())


def test_the_logged_source_never_carries_a_path(clean_env: pytest.MonkeyPatch, tmp_path, caplog) -> None:
    roster = tmp_path / "SENTINEL-DIR" / "roster.json"
    roster.parent.mkdir()
    roster.write_text(_DOC)
    clean_env.setenv("TEAM_MEMBERS_FILE", str(roster))
    app = _app()
    with caplog.at_level("INFO"):
        _assert_roster_source(app)
    assert "SENTINEL-DIR" not in caplog.text
    assert app.state.team_roster_source == "file-source"
