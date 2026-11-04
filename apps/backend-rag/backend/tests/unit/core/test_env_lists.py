"""env-supplied identity lists: unset / blank / malformed all mean EMPTY (admit nobody)."""

from __future__ import annotations

import pytest

from backend.core import env_lists


@pytest.mark.parametrize("raw", [None, "", "   ", "{not json", '{"a": 1}', '[1, 2]', '"x"'])
def test_unset_blank_or_malformed_is_empty(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("X_LIST", raising=False)
    else:
        monkeypatch.setenv("X_LIST", raw)
    assert env_lists.env_json_str_list("X_LIST") == []


def test_valid_list_drops_blank_items(monkeypatch):
    monkeypatch.setenv("X_LIST", '["alpha", " ", "beta"]')
    assert env_lists.env_json_str_list("X_LIST") == ["alpha", "beta"]


def test_team_name_aliases_are_lowercased(monkeypatch):
    monkeypatch.setenv(env_lists.ENV_TEAM_NAME_ALIASES, '["Alpha ", "BETA"]')
    assert env_lists.team_name_aliases() == ("alpha", "beta")


def test_crm_extra_admins_default_to_nobody(monkeypatch):
    monkeypatch.delenv(env_lists.ENV_CRM_EXTRA_ADMIN_EMAILS, raising=False)
    assert env_lists.crm_extra_admin_emails() == frozenset()
    monkeypatch.setenv(env_lists.ENV_CRM_EXTRA_ADMIN_EMAILS, '["Admin@example.test"]')
    assert env_lists.crm_extra_admin_emails() == frozenset({"admin@example.test"})


def test_intake_drive_roots_default_to_nothing_extra(monkeypatch):
    monkeypatch.delenv(env_lists.ENV_INTAKE_DRIVE_ROOT_EXTRA, raising=False)
    assert env_lists.intake_drive_root_extra() == frozenset()
    monkeypatch.setenv(env_lists.ENV_INTAKE_DRIVE_ROOT_EXTRA, '["Folder One"]')
    assert env_lists.intake_drive_root_extra() == frozenset({"Folder One"})


def test_malformed_value_is_not_echoed_in_the_log(monkeypatch, caplog):
    monkeypatch.setenv("X_LIST", "SECRET-PAYLOAD-{")
    env_lists.env_json_str_list("X_LIST")
    assert "SECRET-PAYLOAD" not in caplog.text
