"""Zero 2026-10-20: Subhi (Growth Systems Owner) is admin on every app surface
except the owner cashout, which stays with the owner in code and in the UI."""

from backend.services.agents.team_agent_config import (
    OWNER_CASHOUT_TOOLS,
    ROLE_ADMIN,
    ROLE_GROWTH_ADMIN,
    get_agent_role,
    is_tool_allowed,
)


def test_subhi_maps_to_growth_admin_with_the_admin_perimeter() -> None:
    role = get_agent_role("subhi@balizero.com")
    assert role is ROLE_GROWTH_ADMIN
    assert role.client_scope == "all"
    assert role.allowed_read_tools == [] and role.allowed_write_tools == []
    for tool in ("list_clients", "get_client", "list_practices", "execute_plan", "publish_article"):
        assert is_tool_allowed(role, tool), tool


def test_subhi_is_refused_every_owner_cashout_tool() -> None:
    role = get_agent_role("subhi@balizero.com")
    assert len(OWNER_CASHOUT_TOOLS) == 5
    for tool in OWNER_CASHOUT_TOOLS:
        assert not is_tool_allowed(role, tool), tool


def test_the_owner_keeps_the_unblocked_admin_role() -> None:
    role = get_agent_role("zero@balizero.com")
    assert role is ROLE_ADMIN
    assert role.blocked_tools == []
    for tool in OWNER_CASHOUT_TOOLS:
        assert is_tool_allowed(role, tool), tool
