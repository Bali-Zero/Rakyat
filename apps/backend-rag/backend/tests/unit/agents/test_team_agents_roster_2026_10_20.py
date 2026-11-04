"""Roster decisions of 2026-10-20 (Zero): Veronika is Manager of tax + setup,
Natalie (finance & client services) sees everything and manages nothing."""

from backend.services.agents.team_agent_config import (
    ROLE_CRM_FULL,
    ROLE_FINANCE_VIEWER,
    ROLE_MANAGER,
    TEAM_AGENTS,
    get_agent_role,
    is_tool_allowed,
)


class TestVeronikaManager:
    def test_login_identity_maps_to_manager(self) -> None:
        role = get_agent_role("tax@balizero.com")
        assert role is ROLE_MANAGER
        assert role.role_id == "manager"
        assert role.language == "id"
        assert role.client_scope == "all"

    def test_manager_has_the_full_crm_perimeter(self) -> None:
        assert set(ROLE_MANAGER.allowed_read_tools) == set(ROLE_CRM_FULL.allowed_read_tools)
        assert set(ROLE_MANAGER.allowed_write_tools) == set(ROLE_CRM_FULL.allowed_write_tools)
        assert is_tool_allowed(ROLE_MANAGER, "update_practice_status")
        assert is_tool_allowed(ROLE_MANAGER, "create_practice")


class TestNatalieFinanceViewer:
    def test_maps_to_finance_viewer(self) -> None:
        role = get_agent_role("natalie@balizero.com")
        assert role is ROLE_FINANCE_VIEWER
        assert role.client_scope == "all"
        assert role.language == "id"

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
