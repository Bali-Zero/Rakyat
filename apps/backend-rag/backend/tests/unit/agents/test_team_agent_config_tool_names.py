"""Every tool name in the role configuration is a name the workspace agent really has.

`ToolAuthorizer` compares names as exact strings. The registry names come from
`create_agentic_rag` (plus the flag-gated team CRM tools); the long tail of other
entries are MCP tool names, which this repository does not register in the agentic
loop and which are pinned below. An entry that is neither is a name nothing can ever
call: a role that is meant to allow the tool is denied it, and a confirmation entry
for it never fires.
"""

from __future__ import annotations

from typing import Any

import pytest

import backend.services.agents.team_agent_config as team_agent_config
import backend.services.rag.agentic as agentic_module
from backend.services.agents.team_agent_config import (
    ROLE_ADMIN,
    ROLE_CRM_FULL,
    ROLE_EXECUTIVE_CONSULTANT,
    ROLE_FINANCE_VIEWER,
    ROLE_HR_MANAGER,
    ROLE_TAX_SPECIALIST,
    ROLE_VISA_SPECIALIST,
    AgentRole,
)
from backend.services.agents.tool_authorizer import AuthDecision, ToolAuthorizer

MCP_TOOL_NAMES = frozenset(
    """
    approve_staging_item ask_legal calculate_pricing chat_kbli check_health
    complete_journey_step compose_article create_client create_client_drive_folder
    create_drive_folder create_execution_plan create_journey create_lkpm_report
    create_owner_cashout create_practice delete_episode execute_plan federation_inbox
    federation_mark_read federation_send federation_status get_admin_logs get_all_prices
    get_article get_client get_client_stats get_client_timeline get_compliance_alerts
    get_compliance_summary get_critical_alerts get_expiry_alerts get_hr_dashboard
    get_intel_metrics get_intel_trends get_journey get_journey_next_steps
    get_lkpm_deadlines get_lkpm_status get_owner_cashout get_payroll_summary
    get_portal_dashboard get_portal_visa_status get_practice get_team_activity
    get_team_timesheets get_visa_details ingest_regulation inspect_kbli list_articles
    list_clients list_drive_files list_emails list_lkpm_reports list_owner_cashouts
    list_portal_documents list_portal_messages list_practices list_recent_episodes
    list_staging_items list_subscribers list_team_members list_telegram_conversations
    list_visa_types list_whatsapp_conversations log_interaction owner_cashout_report
    process_owner_cashout publish_article publish_intel recall_similar save_episode
    search_drive search_emails search_intel search_kbli search_service_pricing
    send_email send_portal_message send_whatsapp submit_scraper_job subscribe_newsletter
    update_lkpm_report update_practice_status
    """.split()
)

_LIST_FIELDS = (
    "allowed_read_tools",
    "allowed_write_tools",
    "blocked_tools",
    "requires_confirmation",
)


class _FakeOrchestrator:
    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        _FakeOrchestrator.last_kwargs = kwargs


@pytest.fixture(scope="module")
def registry_names() -> frozenset[str]:
    """Names of the tools the agentic loop registers, built by the real factory."""
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("WA_TEAM_CRM_TOOLS_ENABLED", "true")
        mp.setattr(agentic_module, "AgenticRAGOrchestrator", _FakeOrchestrator)
        agentic_module.create_agentic_rag(retriever=None, db_pool=None)
    finally:
        mp.undo()
    names = frozenset(t.name for t in _FakeOrchestrator.last_kwargs["tools"])
    assert {"get_pricing", "generate_image", "vision_analysis"} <= names
    return names


def _all_roles() -> list[AgentRole]:
    roles = {r.role_id: r for r in vars(team_agent_config).values() if isinstance(r, AgentRole)}
    roles.update({r.role_id: r for r in team_agent_config.TEAM_AGENTS.values()})
    return sorted(roles.values(), key=lambda r: r.role_id)


def test_every_configured_tool_name_is_a_registry_or_mcp_name(registry_names) -> None:
    stale = sorted(
        f"{role.role_id}.{field_name}: {name}"
        for role in _all_roles()
        for field_name in _LIST_FIELDS
        for name in getattr(role, field_name)
        if name not in registry_names and name not in MCP_TOOL_NAMES
    )
    assert not stale, f"names that no tool has: {stale}"


def test_roles_under_test_cover_the_restricted_roles() -> None:
    ids = {r.role_id for r in _all_roles()}
    assert {
        "visa_specialist",
        "executive_consultant",
        "hr_manager",
        "crm_full",
        "tax_specialist",
        "manager",
        "finance_viewer",
    } <= ids


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [ROLE_VISA_SPECIALIST, ROLE_EXECUTIVE_CONSULTANT])
async def test_image_tool_needs_confirmation_under_its_registry_name(
    registry_names, role: AgentRole
) -> None:
    assert "generate_image" in registry_names
    result = await ToolAuthorizer().authorize(
        user_email="staff@example.test",
        agent_role=role,
        tool_name="generate_image",
        args={"prompt": "a lotus"},
    )
    assert result.decision == AuthDecision.NEEDS_CONFIRMATION
    assert "generate_image" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [
        ROLE_VISA_SPECIALIST,
        ROLE_EXECUTIVE_CONSULTANT,
        ROLE_HR_MANAGER,
        ROLE_CRM_FULL,
        ROLE_TAX_SPECIALIST,
        ROLE_FINANCE_VIEWER,
    ],
)
@pytest.mark.parametrize("tool", ["get_pricing", "vision_analysis", "knowledge_graph_search"])
async def test_allowed_runtime_tools_are_allowed_under_registry_names(
    registry_names, role: AgentRole, tool: str
) -> None:
    assert tool in registry_names
    result = await ToolAuthorizer().authorize(
        user_email="staff@example.test", agent_role=role, tool_name=tool, args={}
    )
    assert result.decision == AuthDecision.ALLOWED


@pytest.mark.asyncio
async def test_tool_a_role_does_not_list_is_still_denied(registry_names) -> None:
    authorizer = ToolAuthorizer()
    for role, tool in (
        (ROLE_HR_MANAGER, "kbli_lookup"),
        (ROLE_HR_MANAGER, "crm_query"),
        (ROLE_CRM_FULL, "crm_query"),
        (ROLE_VISA_SPECIALIST, "kbli_lookup"),
        (ROLE_TAX_SPECIALIST, "team_my_clients"),
    ):
        assert tool in registry_names
        result = await authorizer.authorize(
            user_email="staff@example.test", agent_role=role, tool_name=tool, args={}
        )
        assert result.is_denied, f"{role.role_id} must still be denied {tool}"


@pytest.mark.asyncio
async def test_blocked_image_tool_stays_blocked_under_its_registry_name() -> None:
    result = await ToolAuthorizer().authorize(
        user_email="staff@example.test",
        agent_role=ROLE_FINANCE_VIEWER,
        tool_name="generate_image",
        args={},
    )
    assert result.is_denied
    assert "explicitly blocked" in result.reason


@pytest.mark.asyncio
async def test_admin_still_allows_every_registry_tool_without_confirmation(
    registry_names,
) -> None:
    authorizer = ToolAuthorizer()
    for tool in sorted(registry_names):
        result = await authorizer.authorize(
            user_email="admin@example.test", agent_role=ROLE_ADMIN, tool_name=tool, args={}
        )
        assert result.is_allowed, tool


def _holds_image_tool(role: AgentRole) -> bool:
    if "generate_image" in role.blocked_tools:
        return False
    if not role.allowed_read_tools and not role.allowed_write_tools:
        return True
    return "generate_image" in role.allowed_read_tools + role.allowed_write_tools


def test_every_role_that_can_generate_images_asks_for_confirmation() -> None:
    drifted = sorted(
        role.role_id
        for role in _all_roles()
        if _holds_image_tool(role)
        and role.allowed_write_tools
        and "generate_image" not in role.requires_confirmation
    )
    assert not drifted, f"image generation runs unconfirmed in: {drifted}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [
        ROLE_VISA_SPECIALIST,
        ROLE_EXECUTIVE_CONSULTANT,
        ROLE_HR_MANAGER,
        ROLE_CRM_FULL,
        ROLE_TAX_SPECIALIST,
        team_agent_config.ROLE_MANAGER,
    ],
)
async def test_image_tool_needs_confirmation_in_every_role_that_allows_it(
    role: AgentRole,
) -> None:
    result = await ToolAuthorizer().authorize(
        user_email="staff@example.test",
        agent_role=role,
        tool_name="generate_image",
        args={"prompt": "a lotus"},
    )
    assert result.decision == AuthDecision.NEEDS_CONFIRMATION
