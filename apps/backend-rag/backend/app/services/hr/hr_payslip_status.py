"""The one rule for which payslips a member may see as final.

A payslip is final once its payroll period is `approved` or `paid`. A period still in `draft` or
`calculated` can be recalculated by the accountant, so a payslip of that period is provisional and
is never shown to the member as their take-home. Every member-facing read of a payslip that goes
through the HR service (the My HR overview, the personal dashboard) uses this pair.
routers/hr.py (the payslip list and detail routes) keeps its own copy of the same pair.
"""

from __future__ import annotations

# Period statuses from which a payslip is final.
PAYSLIP_VISIBLE_STATUSES: tuple[str, ...] = ("approved", "paid")
