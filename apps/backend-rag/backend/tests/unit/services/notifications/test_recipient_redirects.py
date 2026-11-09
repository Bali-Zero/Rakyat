"""Recipient redirects (reorganisation 2026-11-09): tax@, veronika@, veronika.tax@ and nakhoda@ are retired
identities; every automated email addressed to them is delivered to the Tax Supervisor, in the same seat,
never duplicated."""

from __future__ import annotations

from backend.services.notifications.recipient_redirects import (
    RECIPIENT_REDIRECTS,
    redirect_address,
    redirect_recipients,
)

TARGET = "angel.tax@balizero.com"


def test_retired_identities_redirect_to_the_tax_supervisor():
    assert RECIPIENT_REDIRECTS == {
        "tax@balizero.com": TARGET,
        "veronika@balizero.com": TARGET,
        "veronika.tax@balizero.com": TARGET,
        "nakhoda@balizero.com": TARGET,
    }
    assert redirect_address("Tax@BaliZero.com ") == TARGET
    assert redirect_address("asya@balizero.com") == "asya@balizero.com"


def test_to_tax_is_delivered_to_the_tax_supervisor_with_no_copy_added():
    assert redirect_recipients("tax@balizero.com", None, None) == (TARGET, None, None)


def test_cc_and_bcc_are_rewritten_in_their_own_seat():
    to, cc, bcc = redirect_recipients(
        "kadek.tax@balizero.com",
        ["veronika@balizero.com", "zero@balizero.com"],
        ["veronika.tax@balizero.com"],
    )
    assert to == "kadek.tax@balizero.com"
    assert cc == [TARGET, "zero@balizero.com"]
    assert bcc == []  # the same mailbox already sits in cc: one person, one seat


def test_two_identities_of_the_same_person_collapse_to_one_mailbox():
    to, cc, bcc = redirect_recipients(
        "tax@balizero.com", ["veronika@balizero.com", "Nakhoda@balizero.com", "angel.tax@balizero.com"], None
    )
    assert (to, cc, bcc) == (TARGET, [], None)
    to, cc, bcc = redirect_recipients(
        "alice@example.com",
        ["tax@balizero.com", "veronika.tax@balizero.com"],
        ["veronika@balizero.com"],
    )
    assert (to, cc, bcc) == ("alice@example.com", [TARGET], [])


def test_unredirected_recipients_and_non_strings_pass_through():
    assert redirect_recipients("asya@balizero.com", None, None) == ("asya@balizero.com", None, None)
    to, cc, bcc = redirect_recipients(
        "alice@example.com", ["krisna@balizero.com", None], ["zero@balizero.com"]
    )
    assert (to, cc, bcc) == (
        "alice@example.com",
        ["krisna@balizero.com", None],
        ["zero@balizero.com"],
    )
