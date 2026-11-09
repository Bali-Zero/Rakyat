"""Recipient redirects: an identity that must be DELIVERED to another mailbox.

Why this exists (reorganisation 2026-11-09): the retired Tax Manager addresses
(``tax@``, ``veronika@``, ``veronika.tax@``) and the mailbox they were read on
(``nakhoda@``) are no longer staffed. Every routing table still names ``tax@``,
so system mail addressed to any of them is delivered to the Tax Supervisor
(``angel.tax@balizero.com``), who takes over the ``tax@`` address. Rather than
touching every notifier, the rewrite happens once where mail leaves the
building: the ``/api/notifications/send-email`` handler and the passport/visa
alert bcc.
The redirected address takes the SAME seat (to stays to, cc stays cc, bcc
stays bcc), so a credential delivery keeps exactly one recipient and a hidden
courtesy copy stays hidden. Duplicates collapse: two identities of the same
person become one mailbox.
"""

from __future__ import annotations

RECIPIENT_REDIRECTS: dict[str, str] = {
    "tax@balizero.com": "angel.tax@balizero.com",
    "veronika@balizero.com": "angel.tax@balizero.com",
    "veronika.tax@balizero.com": "angel.tax@balizero.com",
    "nakhoda@balizero.com": "angel.tax@balizero.com",
}


def _norm(addr: object) -> str:
    if not isinstance(addr, str):
        return ""
    return addr.strip().lower()


def redirect_address(addr: object) -> object:
    """The mailbox an address is delivered to: the redirect when one is declared, else the address itself."""
    return RECIPIENT_REDIRECTS.get(_norm(addr), addr)


def redirect_recipients(
    to: object,
    cc_list: list | None,
    bcc_list: list | None,
) -> tuple[object, list | None, list | None]:
    """Return ``(to, cc_list, bcc_list)`` with every redirected address replaced in place, same seat, and an
    address that is already present in an earlier seat (or earlier in the same list) dropped. ``None`` lists stay
    ``None``; non-string entries pass through untouched."""
    new_to = redirect_address(to)
    seen: set[str] = {k for k in (_norm(new_to),) if k}

    def rewrite(lst: list | None) -> list | None:
        if lst is None:
            return None
        out: list = []
        for addr in lst:
            target = redirect_address(addr)
            key = _norm(target)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(target)
        return out

    return new_to, rewrite(cc_list), rewrite(bcc_list)
