"""Assignment routing constants — the light home.

Extracted from `services/crm/assignment.py` (2026-11-11, cross-family
gate on the GARUDA wiring lane): importing two constants from
`assignment` executes `services/crm/__init__.py`, which pulls LangGraph
and application config (772 modules in an isolated probe, then a
settings ValidationError). Consumers that only need the routing table
— the GARUDA CRM adapter is one — import from here instead.
`assignment.py` re-exports both names, so existing importers are
unaffected.
"""

from __future__ import annotations

# Roles eligible for lead assignment (excludes admin, client, support roles)
ASSIGNABLE_ROLES = {
    "Supervisor",
    "Team Leader",
    "Executive Consultant",
    "Junior Consultant",
    "Specialist Advisor",
    "Advisory",
    "Tax Lead",
    "Tax Care",
    "Tax Manager",
    "Senior Consultant",
    "Tax Responsible",
    "Tax Supervisor",
}

# Map practice types to departments for specialty routing.
#
# THIS TABLE SPANS TWO CATALOGUE GENERATIONS, and the lookup is EXACT
# (`.get(practice_type_code)`, no prefix or substring match), so a key
# from the wrong generation simply never fires. Measured 2026-08-26
# across BOTH migration systems — 135 legacy `backend/migrations/*.py`
# and 169 `db/migrations_v2/*.sql`:
#
#   SEEDED, so these four keys DO route:
#     `visa`, `kitas`, `tax`   -> migration_044_seed_practice_types.py
#     `tax_registration`       -> migration_066_populate_practice_types
#   NOT SEEDED ANYWHERE, so these eight are dead weight:
#     `kitap`, `pt_pma`, `investor_visa`, `work_permit`, `company_setup`,
#     `immigration`, `tax_reporting`, `npwp`
#
# The four live ones are all LEGACY short codes. Not one key here is
# seeded by `migrations_v2/`, whose codes are shaped `visa_*` / `ext_*`
# / `tax_*` / `other_*` / `merp_*`. So the department branch fires for
# the old generation and is dead for the whole new one, which silently
# falls through to the round-robin least-workload fallback.
#
# Curing the ~51 modern codes is a CRM-lane job with a real business
# call behind it (which department owns `other_lapor_lahir_under`?),
# tracked in `.claude/skills/modus/PENDING-ARMS.md`. Do NOT "fix" it
# here with a prefix match.
#
# The two GARUDA VOA entries below are catalogue-real: both are seeded
# by `db/migrations_v2/221_practice_types_b1_voa.sql`.
PRACTICE_DEPARTMENT_MAP: dict[str, str] = {
    "visa_b1_voa": "setup",
    "ext_b1_voa": "setup",
    "kitas": "setup",
    "kitap": "setup",
    "pt_pma": "setup",
    "investor_visa": "setup",
    "work_permit": "setup",
    "company_setup": "setup",
    "visa": "setup",
    "immigration": "setup",
    "tax": "tax",
    "tax_reporting": "tax",
    "tax_registration": "tax",
    "npwp": "tax",
}
