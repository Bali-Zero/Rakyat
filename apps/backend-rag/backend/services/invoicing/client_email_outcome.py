"""What actually happened to the client's own invoice mail, and the words that say so.

One rule shared by every text that talks about it: "sent" (the mail went out), "failed" (an
address existed and the send raised) or "no_email" (the client has no address, nothing was
attempted). A text must never claim the client was emailed unless the outcome is "sent".
"""

from __future__ import annotations

SENT = "sent"
FAILED = "failed"
NO_EMAIL = "no_email"


def client_email_outcome(email_sent: bool, email_attempted: bool) -> str:
    if email_sent:
        return SENT
    return FAILED if email_attempted else NO_EMAIL


def regenerated_message(invoice_number: str | None, outcome: str) -> str:
    """Message of the manual regenerate endpoint, true to the outcome."""
    label = f"Invoice {invoice_number}" if invoice_number else "Invoice"
    if outcome == SENT:
        return f"{label} regenerated and emailed to the client"
    if outcome == FAILED:
        return (
            f"{label} regenerated, but the email to the client FAILED: the client has NOT "
            "received it, send it to them yourself (WhatsApp or another channel)"
        )
    return (
        f"{label} regenerated, but the client has NO email on file and was NOT emailed: "
        "contact them by WhatsApp or another channel"
    )


def activity_description(invoice_number: str | None, outcome: str) -> str:
    """Team-visible activity-log line for a generated invoice, true to the outcome."""
    label = f"Invoice {invoice_number}" if invoice_number else "Invoice"
    if outcome == SENT:
        return f"{label} created and sent"
    if outcome == FAILED:
        return f"{label} created; the email to the client FAILED, client NOT emailed"
    return f"{label} created; the client has NO email on file, client NOT emailed"
