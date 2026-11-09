"""Roster decisions of 2026-10-20: the full practices view has a read-only half
(Ruslana) and the managing department heads (reorganisation 2026-11-09).
Only the read-only half is refused as a GARUDA actor and as an assignment target."""

from __future__ import annotations

from typing import Any

import pytest

from backend.app.utils.crm_utils import (
    PRACTICES_EXTRA_VIEW_EMAILS,
    PRACTICES_READ_ONLY_VIEW_EMAILS,
    can_view_all_practices,
)
from backend.services.garuda_portal.staff_auth import (
    can_manage_garuda_practices,
    is_valid_garuda_assignment_target,
)


class _Conn:
    """The validator's single role lookup, answered from a dict."""

    def __init__(self, roles: dict[str, str]) -> None:
        self._roles = roles

    async def fetchrow(self, _sql: str, email: str) -> dict[str, Any] | None:
        role = self._roles.get(email)
        return None if role is None else {"role": role}


def test_the_read_only_half_is_inside_the_full_view() -> None:
    assert PRACTICES_READ_ONLY_VIEW_EMAILS < PRACTICES_EXTRA_VIEW_EMAILS
    for email in ("angel.tax@balizero.com", "ari.firda@balizero.com", "adit@balizero.com"):
        assert email in PRACTICES_EXTRA_VIEW_EMAILS
        assert email not in PRACTICES_READ_ONLY_VIEW_EMAILS
    assert "tax@balizero.com" not in PRACTICES_EXTRA_VIEW_EMAILS
    assert "natalie@balizero.com" not in PRACTICES_READ_ONLY_VIEW_EMAILS
    assert "natalie@balizero.com" not in PRACTICES_EXTRA_VIEW_EMAILS


@pytest.mark.parametrize("email", sorted(PRACTICES_EXTRA_VIEW_EMAILS))
def test_everyone_in_the_full_view_sees_every_practice(email: str) -> None:
    assert can_view_all_practices({"email": email, "role": "member"}) is True


def test_department_heads_manage_garuda_practices() -> None:
    for email, role in (
        ("angel.tax@balizero.com", "Tax Supervisor"),
        ("adit@balizero.com", "Direktur"),
        ("ari.firda@balizero.com", "Tax Responsible"),
    ):
        assert can_manage_garuda_practices({"email": email, "role": role}) is True, email


@pytest.mark.parametrize("email", sorted(PRACTICES_READ_ONLY_VIEW_EMAILS))
def test_read_only_viewers_never_manage(email: str) -> None:
    assert can_manage_garuda_practices({"email": email, "role": "Board Member"}) is False


async def test_new_roles_are_valid_assignment_targets() -> None:
    conn = _Conn({"angel.tax@balizero.com": "Tax Supervisor", "adit@balizero.com": "Direktur"})
    assert await is_valid_garuda_assignment_target(conn, "angel.tax@balizero.com") is True
    assert await is_valid_garuda_assignment_target(conn, "adit@balizero.com") is True


@pytest.mark.parametrize("email", sorted(PRACTICES_READ_ONLY_VIEW_EMAILS))
async def test_read_only_viewers_are_never_assignment_targets(email: str) -> None:
    # Refused before any DB lookup: a conn that would say "yes" never gets asked.
    conn = _Conn({email: "Board Member"})
    assert await is_valid_garuda_assignment_target(conn, email) is False
