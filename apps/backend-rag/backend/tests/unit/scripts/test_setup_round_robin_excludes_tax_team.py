"""Owner order 2026-11-15: the tax title gets no automatic leads.

Three operator scripts hand clients round-robin to a hard-coded SETUP_TEAM list, ignoring the
DB role and ASSIGNABLE_ROLES. A member of the tax team must never be in those lists, or the
scripts keep assigning new clients to them after the move. Read with ast: the scripts are not
imported (they open DB connections and log files at import time).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.app.services.hr.hr_leave_routing import SETUP_TEAM_EMAILS, TAX_TEAM_EMAILS

SCRIPTS = Path(__file__).resolve().parents[4] / "scripts"
ROUND_ROBIN_SCRIPTS = (
    "crm_automation_engine.py",
    "crm_b1_data_quality.py",
    "assign_unassigned_clients.py",
)


def _setup_team(script: str) -> list[str]:
    tree = ast.parse((SCRIPTS / script).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SETUP_TEAM" for t in node.targets
        ):
            return [ast.literal_eval(e).lower() for e in node.value.elts]  # type: ignore[attr-defined]
    raise AssertionError(f"{script}: no module-level SETUP_TEAM list")


@pytest.mark.parametrize("script", ROUND_ROBIN_SCRIPTS)
def test_no_tax_team_member_in_the_setup_round_robin(script: str) -> None:
    team = _setup_team(script)
    # guilt control: the list is real and still names setup people.
    assert team and set(team) & SETUP_TEAM_EMAILS
    assert not set(team) & {e.lower() for e in TAX_TEAM_EMAILS}
