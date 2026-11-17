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


@pytest.mark.parametrize(
    "raw", [None, "", "  ", "{bad", '{"a": 1}', '["a@example.test"]', "[[1]]", "[[], [1]]"]
)
def test_client_pools_unset_or_malformed_is_no_pools(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv(env_lists.ENV_CRM_CLIENT_POOLS, raising=False)
    else:
        monkeypatch.setenv(env_lists.ENV_CRM_CLIENT_POOLS, raw)
    assert env_lists.crm_client_pools() == ()


def test_client_pools_are_normalised(monkeypatch):
    monkeypatch.setenv(
        env_lists.ENV_CRM_CLIENT_POOLS,
        '[[" A@Example.test", "b@example.test", " "], [], ["c@example.test"]]',
    )
    assert env_lists.crm_client_pools() == (
        frozenset({"a@example.test", "b@example.test"}),
        frozenset({"c@example.test"}),
    )


def test_client_pools_are_read_at_call_time(monkeypatch):
    monkeypatch.setenv(env_lists.ENV_CRM_CLIENT_POOLS, '[["a@example.test"]]')
    assert len(env_lists.crm_client_pools()) == 1
    monkeypatch.delenv(env_lists.ENV_CRM_CLIENT_POOLS)
    assert env_lists.crm_client_pools() == ()


def test_malformed_client_pools_are_not_echoed_in_the_log(monkeypatch, caplog):
    monkeypatch.setenv(env_lists.ENV_CRM_CLIENT_POOLS, "SECRET-PAYLOAD-{")
    env_lists.crm_client_pools()
    assert "SECRET-PAYLOAD" not in caplog.text
    assert env_lists.ENV_CRM_CLIENT_POOLS in caplog.text


def test_client_pools_blank_entries_are_still_dropped(monkeypatch):
    monkeypatch.setenv(env_lists.ENV_CRM_CLIENT_POOLS, '[["a@example.test", " "]]')
    assert env_lists.crm_client_pools() == (frozenset({"a@example.test"}),)


def test_client_pools_parsing_does_not_judge_how_an_entry_is_spelled(monkeypatch):
    """Validity is an entity question (team_members), answered by validate_client_pools."""
    monkeypatch.setenv(env_lists.ENV_CRM_CLIENT_POOLS, '[["x@.", "not-an-email"]]')
    assert env_lists.crm_client_pools() == (frozenset({"x@.", "not-an-email"}),)
