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

# MCP tool names that this repository cannot verify: the MCP server that registers them
# is outside the Rakyat perimeter (PERIMETER.txt), so no registry is importable here.
# Only the names whose registration was confirmed against the server's own definitions are
# listed in MCP_TOOL_NAMES; the rest are pinned separately below as UNVERIFIED so the debt
# stays visible. Every one of them was already in these role lists on origin/main.
MCP_TOOL_NAMES = frozenset(
    """
    approve_staging_item ask_legal calculate_pricing chat_kbli check_health
    complete_journey_step compose_article create_client create_client_drive_folder
    create_drive_folder create_execution_plan create_journey create_practice
    delete_episode execute_plan federation_inbox federation_mark_read federation_send
    federation_status get_admin_logs get_all_prices get_article get_client
    get_client_stats get_client_timeline get_compliance_alerts get_compliance_summary
    get_critical_alerts get_expiry_alerts get_intel_metrics get_intel_trends
    get_journey get_journey_next_steps get_portal_dashboard get_portal_visa_status
    get_practice get_visa_details ingest_regulation inspect_kbli list_articles
    list_clients list_drive_files list_emails list_portal_documents
    list_portal_messages list_practices list_recent_episodes list_staging_items
    list_subscribers list_visa_types list_whatsapp_conversations log_interaction
    publish_article publish_intel recall_similar save_episode search_drive
    search_emails search_intel search_kbli search_service_pricing send_email
    send_portal_message send_whatsapp submit_scraper_job subscribe_newsletter
    update_practice_status
    """.split()
)

UNVERIFIED_MCP_TOOL_NAMES = frozenset(
    """
    create_lkpm_report create_owner_cashout get_hr_dashboard get_lkpm_deadlines
    get_lkpm_status get_owner_cashout get_payroll_summary get_team_activity
    get_team_timesheets list_lkpm_reports list_owner_cashouts list_team_members
    list_telegram_conversations owner_cashout_report process_owner_cashout
    update_lkpm_report
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
    assert {"get_pricing", "generate_image", "vision_analysis", "knowledge_graph_search"} <= names
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
        if name not in registry_names
        and name not in MCP_TOOL_NAMES
        and name not in UNVERIFIED_MCP_TOOL_NAMES
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
@pytest.mark.parametrize("tool", ["get_pricing", "knowledge_graph_search"])
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


# ---------------------------------------------------------------------------
# Round 2: the renames must not widen any role beyond the intended tools.
# ---------------------------------------------------------------------------

# The effective set of each explicit-list role on origin/main (allowed lists intersected with
# the registered tools, minus blocked tools), pinned from the main file. Admin and
# growth_admin have empty allowed lists on main and here, so they are "everything" in both.
MAIN_EFFECTIVE = {
    "crm_full": {"calculator", "team_knowledge", "vector_search", "web_search"},
    "manager": {"calculator", "team_knowledge", "vector_search", "web_search"},
    "finance_viewer": {"calculator", "team_knowledge", "vector_search", "web_search"},
    "hr_manager": {"calculator", "team_knowledge", "timesheet", "vector_search", "web_search"},
    "tax_specialist": {"calculator", "team_knowledge", "timesheet", "vector_search", "web_search"},
    "executive_consultant": {
        "calculator",
        "crm_query",
        "team_knowledge",
        "timesheet",
        "vector_search",
        "web_search",
    },
    "visa_specialist": {
        "calculator",
        "crm_query",
        "team_knowledge",
        "timesheet",
        "vector_search",
        "web_search",
    },
}

# What the renames are allowed to hand a role: two plain reads (no scope beyond the caller's
# role, no cost) and the image tool, which must stay behind a confirmation.
GAINABLE_READS = frozenset({"get_pricing", "knowledge_graph_search"})
GAINABLE_CONFIRMED = frozenset({"generate_image"})
# Never granted by the lists: it reads any file under the shared upload directories with no
# ownership check and makes paid Gemini calls. Reaching it is its own finding.
NEVER_GRANTED = frozenset({"vision_analysis"})


def _effective(role: AgentRole, registry: frozenset[str]) -> set[str]:
    listed = set(role.allowed_read_tools) | set(role.allowed_write_tools)
    return (listed & registry) - set(role.blocked_tools)


def test_no_role_list_names_vision_analysis(registry_names) -> None:
    holders = sorted(
        f"{role.role_id}.{field_name}"
        for role in _all_roles()
        for field_name in _LIST_FIELDS
        if NEVER_GRANTED & set(getattr(role, field_name))
    )
    assert not holders, f"vision_analysis is listed in: {holders}"
    for role in _all_roles():
        if role.allowed_read_tools or role.allowed_write_tools:
            assert not (NEVER_GRANTED & _effective(role, registry_names)), role.role_id


def test_main_effective_snapshot_covers_every_explicit_list_role() -> None:
    explicit = {r.role_id for r in _all_roles() if r.allowed_read_tools or r.allowed_write_tools}
    assert explicit == set(MAIN_EFFECTIVE)
    # The roles with empty allowed lists are unchanged by the renames (nothing to rename).
    for role in (ROLE_ADMIN, team_agent_config.ROLE_GROWTH_ADMIN):
        assert not role.allowed_read_tools and not role.allowed_write_tools


def test_tools_gained_through_the_renames_are_reads_or_confirmed(registry_names) -> None:
    roles = {r.role_id: r for r in _all_roles()}
    for role_id, before in MAIN_EFFECTIVE.items():
        role = roles[role_id]
        gained = _effective(role, registry_names) - before
        lost = before - _effective(role, registry_names)
        assert not lost, f"{role_id} lost {sorted(lost)}"
        assert gained <= GAINABLE_READS | GAINABLE_CONFIRMED, (role_id, sorted(gained))
        for tool in gained & GAINABLE_CONFIRMED:
            assert tool in role.requires_confirmation, f"{role_id} gains {tool} unconfirmed"


def test_the_gain_is_exactly_the_intended_one(registry_names) -> None:
    roles = {r.role_id: r for r in _all_roles()}
    for role_id, before in MAIN_EFFECTIVE.items():
        gained = _effective(roles[role_id], registry_names) - before
        expected = set(GAINABLE_READS)
        if role_id != "finance_viewer":
            expected |= GAINABLE_CONFIRMED
        assert gained == expected, role_id


# ---------------------------------------------------------------------------
# Round 2: the MCP names this repository cannot verify stay pinned and visible.
# ---------------------------------------------------------------------------


def test_unverified_mcp_names_are_pinned_listed_and_not_registered(registry_names) -> None:
    assert len(UNVERIFIED_MCP_TOOL_NAMES) == 16
    assert not UNVERIFIED_MCP_TOOL_NAMES & MCP_TOOL_NAMES
    # If one becomes registered in the agentic loop it moves to the verified side.
    assert not UNVERIFIED_MCP_TOOL_NAMES & registry_names
    listed = {name for role in _all_roles() for f in _LIST_FIELDS for name in getattr(role, f)}
    assert UNVERIFIED_MCP_TOOL_NAMES <= listed, "a pinned name no role lists any more"


def test_a_name_outside_registry_and_pins_is_rejected(registry_names) -> None:
    known = registry_names | MCP_TOOL_NAMES | UNVERIFIED_MCP_TOOL_NAMES
    assert "definitely_not_a_tool" not in known
    assert "vision" not in known and "image_generation" not in known
