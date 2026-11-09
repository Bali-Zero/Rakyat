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


def _parts(addr: object) -> list:
    """The addresses a recipient value names: a comma-joined string is split, anything else is one entry."""
    if isinstance(addr, str) and "," in addr:
        return [part.strip() for part in addr.split(",") if part.strip()]
    return [addr]


def redirect_recipients(
    to: object,
    cc_list: list | None,
    bcc_list: list | None,
) -> tuple[object, list | None, list | None]:
    """Return ``(to, cc_list, bcc_list)`` with every redirected address replaced in place, same seat, and an
    address that is already present in an earlier seat (or earlier in the same list) dropped. A comma-joined
    string counts as the addresses it names, because notifiers do send ``"a, b"``. ``None`` lists stay ``None``;
    non-string entries pass through untouched; a ``to`` with nothing to change comes back byte-identical."""
    seen: set[str] = set()

    def rewrite_parts(addr: object) -> list:
        out: list = []
        for part in _parts(addr):
            target = redirect_address(part)
            key = _norm(target)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(target)
        return out

    def rewrite(lst: list | None) -> list | None:
        if lst is None:
            return None
        return [target for addr in lst for target in rewrite_parts(addr)]

    to_parts = _parts(to)
    new_to_parts = rewrite_parts(to)
    if new_to_parts == to_parts:
        new_to = to
    elif all(isinstance(part, str) for part in new_to_parts):
        new_to = ", ".join(new_to_parts)
    else:
        new_to = new_to_parts[0] if new_to_parts else to

    return new_to, rewrite(cc_list), rewrite(bcc_list)
