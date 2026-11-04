"""Recipient redirects (Zero 2026-10-20/21): xug@, kagecesa@ and kagecesa.xug@ are Veronika's identities;
every automated email addressed to them is delivered to nakhoda@, in the same seat, never duplicated."""

from __future__ import annotations

from backend.services.notifications.recipient_redirects import (
    RECIPIENT_REDIRECTS,
    redirect_address,
    redirect_recipients,
)

NAKHODA = "nakhoda@balizero.com"


def test_veronika_identities_redirect_to_nakhoda():
    assert RECIPIENT_REDIRECTS == {
        "tax@balizero.com": NAKHODA,
        "veronika@balizero.com": NAKHODA,
        "veronika.tax@balizero.com": NAKHODA,
    }
    assert redirect_address("Tax@BaliZero.com ") == NAKHODA
    assert redirect_address("asya@balizero.com") == "asya@balizero.com"


def test_to_tax_is_delivered_to_nakhoda_with_no_copy_added():
    assert redirect_recipients("tax@balizero.com", None, None) == (NAKHODA, None, None)


def test_cc_and_bcc_are_rewritten_in_their_own_seat():
    to, cc, bcc = redirect_recipients(
        "kadek.tax@balizero.com",
        ["veronika@balizero.com", "zero@balizero.com"],
        ["veronika.tax@balizero.com"],
    )
    assert to == "kadek.tax@balizero.com"
    assert cc == [NAKHODA, "zero@balizero.com"]
    assert bcc == []  # the same mailbox already sits in cc: one person, one seat


def test_two_identities_of_the_same_person_collapse_to_one_mailbox():
    to, cc, bcc = redirect_recipients(
        "tax@balizero.com", ["veronika@balizero.com", "Nakhoda@balizero.com"], None
    )
    assert (to, cc, bcc) == (NAKHODA, [], None)
    to, cc, bcc = redirect_recipients(
        "alice@example.com",
        ["tax@balizero.com", "veronika.tax@balizero.com"],
        ["veronika@balizero.com"],
    )
    assert (to, cc, bcc) == ("alice@example.com", [NAKHODA], [])


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
