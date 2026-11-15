"""Owner order 2026-11-15: "Tax Consultant" is a staff title with Tax Lead's access,
but it is NOT an automatic lead assignee."""

from backend.services.crm.partners.service import INTERNAL_ROLES_ALWAYS_ALLOWED
from backend.services.crm_assignment_routing import ASSIGNABLE_ROLES


def test_tax_consultant_is_not_an_automatic_lead_assignee() -> None:
    assert "Tax Consultant" not in ASSIGNABLE_ROLES
    assert "tax consultant" not in {r.lower() for r in ASSIGNABLE_ROLES}


def test_tax_lead_is_still_assignable() -> None:
    # guilt control: the pin above must not pass because the set is empty.
    assert "Tax Lead" in ASSIGNABLE_ROLES


def test_tax_consultant_is_always_allowed_like_tax_lead_in_partner_access() -> None:
    assert "tax lead" in INTERNAL_ROLES_ALWAYS_ALLOWED
    assert "tax consultant" in INTERNAL_ROLES_ALWAYS_ALLOWED
