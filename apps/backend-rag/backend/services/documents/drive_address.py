"""Which Drive files a document's address fields name.

The Drive proxy reads the run of ``[A-Za-z0-9_-]`` that follows ``/d/`` or a
``?id=`` / ``&id=`` parameter in a stored address as that row's Drive file. The
write side names the same runs, so each can be checked like an explicit ``file_id``.
"""

from __future__ import annotations

import re

# A lookahead, so overlapping occurrences are all seen and none hides another.
_ADDRESS_RUN_RE = re.compile(r"(?=(?:/d/|(?<=[?&])id=)([A-Za-z0-9_-]+))")
MIN_DRIVE_ID_LENGTH = 10
MAX_DRIVE_ID_LENGTH = 256


def address_drive_ids(*addresses: str | None) -> list[str]:
    """Return the requestable Drive ids named by the addresses, in order, once each."""
    ids: list[str] = []
    for address in addresses:
        if not isinstance(address, str):
            continue
        for run in _ADDRESS_RUN_RE.findall(address):
            if MIN_DRIVE_ID_LENGTH <= len(run) <= MAX_DRIVE_ID_LENGTH and run not in ids:
                ids.append(run)
    return ids
