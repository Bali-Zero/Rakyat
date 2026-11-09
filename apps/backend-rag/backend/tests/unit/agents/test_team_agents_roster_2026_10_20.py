"""Roster decisions of 2026-10-20 and the 2026-11-09 reorganisation (Tax Supervisor and
Direktur are department heads with the manager role); the finance & client services role sees everything and manages nothing.

Offboarding 2026-11-03: the person-level mapping for natalie@ is gone."""

from backend.services.agents.team_agent_config import (
    ROLE_CRM_FULL,
    ROLE_FINANCE_VIEWER,
    ROLE_MANAGER,
    TEAM_AGENTS,
    get_agent_role,
    is_tool_allowed,
)


class TestDepartmentHeadManager:
    def test_retired_tax_login_has_no_agent_role(self) -> None:
        assert get_agent_role("tax@balizero.com") is None
        assert "tax@balizero.com" not in TEAM_AGENTS

    def test_tax_supervisor_and_direktur_are_managers(self) -> None:
        assert get_agent_role("angel.tax@balizero.com") is ROLE_MANAGER
        assert get_agent_role("adit@balizero.com") is ROLE_MANAGER

    def test_hr_and_setup_roles_are_unchanged(self) -> None:
        assert get_agent_role("asya@balizero.com").role_id == "hr_manager"
        assert get_agent_role("ari.firda@balizero.com").role_id == "executive_consultant"
        assert get_agent_role("krisna@balizero.com").role_id == "executive_consultant"

    def test_manager_role_shape(self) -> None:
        role = get_agent_role("angel.tax@balizero.com")
        assert role is ROLE_MANAGER
        assert role.role_id == "manager"
        assert role.language == "id"
        assert role.client_scope == "all"

    def test_manager_has_the_full_crm_perimeter(self) -> None:
        assert set(ROLE_MANAGER.allowed_read_tools) == set(ROLE_CRM_FULL.allowed_read_tools)
        assert set(ROLE_MANAGER.allowed_write_tools) == set(ROLE_CRM_FULL.allowed_write_tools)
        assert is_tool_allowed(ROLE_MANAGER, "update_practice_status")
        assert is_tool_allowed(ROLE_MANAGER, "create_practice")


class TestFinanceViewerRole:
    def test_offboarded_person_has_no_agent_role(self) -> None:
        assert get_agent_role("natalie@balizero.com") is None
        assert "natalie@balizero.com" not in TEAM_AGENTS
        assert ROLE_FINANCE_VIEWER not in TEAM_AGENTS.values()

    def test_role_definition_is_kept_for_reuse(self) -> None:
        assert ROLE_FINANCE_VIEWER.client_scope == "all"
        assert ROLE_FINANCE_VIEWER.language == "id"

    def test_sees_everything(self) -> None:
        for tool in (
            "list_clients",
            "get_client",
            "list_practices",
            "get_practice",
            "get_compliance_alerts",
        ):
            assert is_tool_allowed(ROLE_FINANCE_VIEWER, tool), tool

    def test_manages_nothing(self) -> None:
        for tool in (
            "update_practice_status",
            "create_client",
            "create_practice",
            "create_journey",
            "complete_journey_step",
            "send_portal_message",
            "create_drive_folder",
        ):
            assert not is_tool_allowed(ROLE_FINANCE_VIEWER, tool), tool
        assert is_tool_allowed(ROLE_FINANCE_VIEWER, "log_interaction")


def test_every_balizero_agent_email_is_lowercase_and_unique() -> None:
    emails = list(TEAM_AGENTS)
    assert emails == [e.lower() for e in emails]
    assert len(emails) == len(set(emails))
