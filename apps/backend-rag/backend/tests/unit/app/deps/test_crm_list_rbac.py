"""RBAC: CRM read-list ownership filter is ARMED for non-admins.

Root cause this locks: get_crm_user_filter() used to gate on
can_view_all_clients() (which returns True for everyone), so it returned None
for every user and silently disarmed the ownership filter across ~12 CRM
read-list endpoints. It now gates on is_crm_admin().
"""

from __future__ import annotations

from backend.app.deps import crm_access

# --------------------------------------------------------------------------- #
# get_crm_user_filter — admin sees all (None), non-admin scoped to own email   #
# --------------------------------------------------------------------------- #

def test_admin_gets_no_filter():
    # zero@ is in the CRM admin set -> full view (None means no WHERE filter)
    admin = {"email": "zero@balizero.com", "role": "founder"}
    assert crm_access.get_crm_user_filter(admin) is None


def test_admin_by_role_gets_no_filter():
    admin = {"email": "someone@balizero.com", "role": "admin"}
    assert crm_access.get_crm_user_filter(admin) is None


def test_non_admin_scoped_to_own_email():
    # Sahira: real role from prod, NOT an admin -> filtered to her own email
    sahira = {"email": "Sahira@balizero.com", "role": "Marketing & Accounting"}
    assert crm_access.get_crm_user_filter(sahira) == [sahira["email"].lower()]


def test_non_admin_junior_consultant_scoped():
    damar = {"email": "damar@balizero.com", "role": "Junior Consultant"}
    assert crm_access.get_crm_user_filter(damar) == [damar["email"].lower()]


def test_empty_user_is_filtered_not_full_view():
    # defense: an empty/anon user must NOT get the full book
    assert crm_access.get_crm_user_filter({}) == [""]


def test_accounting_full_view_gets_no_filter_without_being_crm_admin():
    from backend.app.utils.crm_utils import PRACTICES_EXTRA_VIEW_EMAILS

    accounting_email = next(iter(PRACTICES_EXTRA_VIEW_EMAILS))
    accounting_user = {"email": accounting_email, "role": "Accounting"}
    assert crm_access.is_crm_admin(accounting_user) is False
    assert crm_access.get_crm_user_filter(accounting_user) is None
    assert crm_access.get_practices_user_filter(accounting_user) is None


# --------------------------------------------------------------------------- #
# can_view_all_clients stays True — write-guards in wa_actions/omnichannel     #
# that call it directly must NOT regress.                                      #
# --------------------------------------------------------------------------- #

def test_can_view_all_clients_unchanged_for_non_admin():
    # The list filter is now is_crm_admin-gated, but the blanket guard helper
    # stays True so direct write-guards keep their current behaviour.
    sahira = {"email": "sahira@balizero.com", "role": "Marketing & Accounting"}
    assert crm_access.can_view_all_clients(sahira) is True
