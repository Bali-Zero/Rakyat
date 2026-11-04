"""HR leave request routing — hard-coded org chart rules.

Tax team (*.tax@), setup team and Natalie → Kagecesa (tax@balizero.com),
Manager of both the tax department and the setup team (Zero, 2026-10-20).
Rina → Ruslana
Everyone else (including Veronika herself) → Zero

When the org chart changes, update SUPERVISOR_MAP and add tests.
No DB column, no migration: rules stable per Zero (2026-04-07, 2026-10-20).
"""

from __future__ import annotations

VERONIKA_EMAIL = "tax@balizero.com"
RUSLANA_EMAIL = "ruslana@balizero.com"

#: The tax department. Both spellings of Dewa Ayu's address are kept: the
#: roster carries `dewaayu.tax@`, the original map carried `dewa.ayu.tax@`.
TAX_TEAM_EMAILS: frozenset[str] = frozenset(
    {
        "kadek.tax@balizero.com",
        "angel.tax@balizero.com",
        "dewaayu.tax@balizero.com",
        "dewa.ayu.tax@balizero.com",
        "faysha.tax@balizero.com",
    }
)

#: The setup team (department `setup` in the off-repo staff roster).
SETUP_TEAM_EMAILS: frozenset[str] = frozenset(
    {
        "adit@balizero.com",
        "ari.firda@balizero.com",
        "krisna@balizero.com",
        "dea@balizero.com",
        "surya@balizero.com",
        "damar@balizero.com",
        "vino@balizero.com",
        "candra@balizero.com",
    }
)

#: Everyone Veronika manages: tax, setup, and Natalie (finance & client services).
VERONIKA_REPORTS: frozenset[str] = TAX_TEAM_EMAILS | SETUP_TEAM_EMAILS | {"natalie@balizero.com"}

SUPERVISOR_MAP: dict[str, str] = {
    **dict.fromkeys(sorted(VERONIKA_REPORTS), VERONIKA_EMAIL),
    "rina@balizero.com": RUSLANA_EMAIL,
}

ZERO_EMAIL = "zero@balizero.com"
ASYA_EMAIL = "asya@balizero.com"


def _normalize(email: str) -> str:
    return email.lower().strip()


def resolve_approver(requester_email: str) -> str:
    """Return the email of the user who should approve this leave request.

    Falls back to Zero when the requester has no specific supervisor
    configured (e.g. Veronika, Asya, Ruslana, and anyone not in the map).
    """
    return SUPERVISOR_MAP.get(_normalize(requester_email), ZERO_EMAIL)


def build_notification_recipients(
    requester_email: str,
) -> dict[str, str | list[str]]:
    """Return {to, cc[]} for the leave-request notification email.

    Rules:
    - TO: the approver from resolve_approver()
    - Zero always in CC unless he is already the TO
    - Asya always in CC unless she is the requester
    """
    requester = _normalize(requester_email)
    approver = resolve_approver(requester)
    cc: list[str] = []
    if approver != ZERO_EMAIL:
        cc.append(ZERO_EMAIL)
    if requester != ASYA_EMAIL:
        cc.append(ASYA_EMAIL)
    return {"to": approver, "cc": cc}


def build_review_recipients(
    requester_email: str,
    reviewer_email: str,
) -> dict[str, str | list[str]]:
    """Return {to, cc[]} for the approval/rejection notification email.

    Rules:
    - TO: the requester (the person who asked for leave)
    - Asya always in CC for HR audit, unless she is the reviewer or requester
    - Zero always in CC for visibility, unless he is the reviewer or requester
    """
    requester = _normalize(requester_email)
    reviewer = _normalize(reviewer_email)
    cc: list[str] = []
    if reviewer != ZERO_EMAIL and requester != ZERO_EMAIL:
        cc.append(ZERO_EMAIL)
    if reviewer != ASYA_EMAIL and requester != ASYA_EMAIL:
        cc.append(ASYA_EMAIL)
    return {"to": requester, "cc": cc}
