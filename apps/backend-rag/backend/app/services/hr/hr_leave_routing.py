"""HR leave request routing — hard-coded org chart rules.

Tax team (*.tax@) → the Tax Supervisor (angel.tax@balizero.com)
Setup team, the Tax Responsible and HR & Accounting → the Direktur (adit@balizero.com)
Rina, Rio, Dea and Candra → Ruslana, whatever their department
Everyone else (including the Tax Supervisor and the Direktur themselves) → Zero

Reorganisation 2026-11-09 (owner order). Ruslana's reports widened 2026-11-15
(owner order): Dea and Candra are Tax Consultants in the tax department, Rio
is in the setup department, and Ruslana approves all three.

When the org chart changes, update SUPERVISOR_MAP and add tests.
No DB column, no migration: rules stable per Zero (2026-04-07, 2026-10-20).
"""

from __future__ import annotations

TAX_SUPERVISOR_EMAIL = "angel.tax@balizero.com"
SETUP_DIRECTOR_EMAIL = "adit@balizero.com"
RUSLANA_EMAIL = "ruslana@balizero.com"

#: The tax department, including its supervisor. Both spellings of Dewa Ayu's
#: address are kept: the roster carries `dewaayu.tax@`, the original map carried
#: `dewa.ayu.tax@`. Dea and Candra (Tax Consultants, plain addresses) are in the
#: department but report to Ruslana, see RUSLANA_REPORTS.
TAX_TEAM_EMAILS: frozenset[str] = frozenset(
    {
        "kadek.tax@balizero.com",
        "angel.tax@balizero.com",
        "dewaayu.tax@balizero.com",
        "dewa.ayu.tax@balizero.com",
        "faysha.tax@balizero.com",
        "dea@balizero.com",
        "candra@balizero.com",
    }
)

#: The setup team (department `setup` in the off-repo staff roster). Rio is in
#: the department but reports to Ruslana, see RUSLANA_REPORTS.
SETUP_TEAM_EMAILS: frozenset[str] = frozenset(
    {
        "adit@balizero.com",
        "ari.firda@balizero.com",
        "krisna@balizero.com",
        "surya@balizero.com",
        "damar@balizero.com",
        "vino@balizero.com",
        "rio@balizero.com",
    }
)

#: Everyone Ruslana approves, whatever their department (owner orders 2026-10-20
#: for Rina, 2026-11-15 for Rio, Dea and Candra). Taken out of both department
#: heads' reports below, so each person has exactly one approver.
RUSLANA_REPORTS: frozenset[str] = frozenset(
    {
        "rina@balizero.com",
        "rio@balizero.com",
        "dea@balizero.com",
        "candra@balizero.com",
    }
)

ZERO_EMAIL = "zero@balizero.com"
ASYA_EMAIL = "asya@balizero.com"

#: Everyone the Tax Supervisor approves: the tax team minus the supervisor and
#: minus Ruslana's reports.
TAX_SUPERVISOR_REPORTS: frozenset[str] = TAX_TEAM_EMAILS - {TAX_SUPERVISOR_EMAIL} - RUSLANA_REPORTS

#: Everyone the Direktur approves: the setup team minus the Direktur and minus
#: Ruslana's reports, plus HR & Accounting (direct assistant). The Tax
#: Responsible (ari) sits in the setup team.
SETUP_DIRECTOR_REPORTS: frozenset[str] = (
    SETUP_TEAM_EMAILS - {SETUP_DIRECTOR_EMAIL} - RUSLANA_REPORTS
) | {ASYA_EMAIL}

SUPERVISOR_MAP: dict[str, str] = {
    **dict.fromkeys(sorted(TAX_SUPERVISOR_REPORTS), TAX_SUPERVISOR_EMAIL),
    **dict.fromkeys(sorted(SETUP_DIRECTOR_REPORTS), SETUP_DIRECTOR_EMAIL),
    **dict.fromkeys(sorted(RUSLANA_REPORTS), RUSLANA_EMAIL),
}


def _normalize(email: str) -> str:
    return email.lower().strip()


def reports_of(approver_email: str) -> frozenset[str]:
    """The requester emails whose leave `approver_email` approves (empty for a non-approver)."""
    approver = _normalize(approver_email)
    if not approver:
        return frozenset()
    return frozenset(req for req, appr in SUPERVISOR_MAP.items() if appr == approver)


def resolve_approver(requester_email: str) -> str:
    """Return the email of the user who should approve this leave request.

    Falls back to Zero when the requester has no specific supervisor
    configured (e.g. the Tax Supervisor, the Direktur, Ruslana and anyone not in the map).
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
