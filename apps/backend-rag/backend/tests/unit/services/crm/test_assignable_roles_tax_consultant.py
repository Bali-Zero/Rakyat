"""Owner order 2026-11-15: "Tax Consultant" is a staff title with Tax Lead's access,
but it is NOT an automatic lead assignee.

Two different things are called "assignment" here:
- automatic: the least-loaded picker (`ASSIGNABLE_ROLES`, an exact `tm.role IN (...)` filter)
  run by the CRM lead agent and by the GARUDA order writer. Tax Consultant must never be picked.
- manual: a staff member choosing a colleague as the target of a GARUDA practice
  (`is_valid_garuda_assignment_target`). Tax Lead is a valid manual target, so Tax Consultant
  is one too: that is the parity the order asks for, not an automatic lead.
"""

from __future__ import annotations

from typing import Any

from backend.services.crm.partners.service import INTERNAL_ROLES_ALWAYS_ALLOWED
from backend.services.crm_assignment_routing import ASSIGNABLE_ROLES
from backend.services.garuda_ops.adapters_pg import PostgresCrmWriter
from backend.services.garuda_portal.staff_auth import (
    can_manage_garuda_practices,
    is_valid_garuda_assignment_target,
)


class _PickerConn:
    """Answers the automatic picker like Postgres would: only rows whose role is in the filter."""

    def __init__(self, rows: dict[str, str]) -> None:
        self._rows = rows
        self.role_filters: list[set[str]] = []

    async def fetchval(self, sql: str, *args: Any) -> str | None:
        n_roles = sql.count("$") - (1 if "LOWER(tm.department)" in sql else 0)
        roles = set(args[:n_roles])
        self.role_filters.append(roles)
        for email, role in sorted(self._rows.items()):
            if role in roles:
                return email
        return None


class _TargetConn:
    def __init__(self, roles: dict[str, str]) -> None:
        self._roles = roles

    async def fetchrow(self, _sql: str, email: str) -> dict[str, Any] | None:
        role = self._roles.get(email)
        return None if role is None else {"role": role}


def test_tax_consultant_is_not_an_automatic_lead_assignee() -> None:
    assert "Tax Consultant" not in ASSIGNABLE_ROLES
    assert "tax consultant" not in {r.lower() for r in ASSIGNABLE_ROLES}


def test_tax_lead_is_still_assignable() -> None:
    # guilt control: the pin above must not pass because the set is empty.
    assert "Tax Lead" in ASSIGNABLE_ROLES


async def test_the_garuda_automatic_picker_never_picks_a_tax_consultant() -> None:
    writer = PostgresCrmWriter(pool=None)  # type: ignore[arg-type]
    only_consultant = _PickerConn({"a@example.test": "Tax Consultant"})
    assert await writer._pick_assignee(only_consultant, practice_type_code="tax") is None
    assert only_consultant.role_filters
    assert all("Tax Consultant" not in f for f in only_consultant.role_filters)

    # guilt control: the same picker does pick a Tax Lead.
    with_lead = _PickerConn({"a@example.test": "Tax Consultant", "b@example.test": "Tax Lead"})
    assert await writer._pick_assignee(with_lead, practice_type_code="tax") == "b@example.test"


async def test_manual_garuda_target_and_actor_parity_with_tax_lead() -> None:
    conn = _TargetConn({"lead@example.test": "Tax Lead", "tc@example.test": "Tax Consultant"})
    assert await is_valid_garuda_assignment_target(conn, "lead@example.test") is True
    assert await is_valid_garuda_assignment_target(conn, "tc@example.test") is True
    assert can_manage_garuda_practices({"email": "tc@example.test", "role": "Tax Consultant"}) is True
    # innocence: an unknown title is still refused on the same path.
    unknown = _TargetConn({"x@example.test": "Tax Intern"})
    assert await is_valid_garuda_assignment_target(unknown, "x@example.test") is False


def test_tax_consultant_is_always_allowed_like_tax_lead_in_partner_access() -> None:
    assert "tax lead" in INTERNAL_ROLES_ALWAYS_ALLOWED
    assert "tax consultant" in INTERNAL_ROLES_ALWAYS_ALLOWED
