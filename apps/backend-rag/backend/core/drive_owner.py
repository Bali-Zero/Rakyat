"""The Drive account the backups must land on (outside git).

Resolution, first that yields a value wins:

1. env ``BACKUP_DRIVE_OWNER_EMAIL`` (non-blank; must look like local@domain, else not configured);
2. the file named by env ``BACKUP_DRIVE_OWNER_EMAIL_FILE``, default ``~/.nuzantara/backup_drive_owner.env``:
   exactly ONE non-empty line (the address), a regular file (no symlink), mode 0600, owned by the current user;
3. nothing: not configured.

Not configured means no account matches, so callers refuse before any write. A file that exists but breaks
the rules is also "not configured" (fail closed). No error or log ever carries the address.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

ENV_NAME = "BACKUP_DRIVE_OWNER_EMAIL"
ENV_FILE = "BACKUP_DRIVE_OWNER_EMAIL_FILE"
DEFAULT_FILE = "~/.nuzantara/backup_drive_owner.env"


_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid(value: str) -> str | None:
    value = value.strip().lower()
    return value if _EMAIL_SHAPE.match(value) else None


def _from_file() -> str | None:
    path = Path(os.environ.get(ENV_FILE) or DEFAULT_FILE).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(4096)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return _valid(lines[0]) if len(lines) == 1 else None


def expected_drive_owner_email() -> str | None:
    """Normalised configured address, or ``None`` when not configured."""
    raw = (os.environ.get(ENV_NAME) or "").strip()
    if raw:
        return _valid(raw.lstrip("\ufeff"))
    return _from_file()


def drive_owner_matches(email: object) -> bool:
    """True only when an owner is configured AND ``email`` is that address."""
    expected = expected_drive_owner_email()
    if expected is None or not isinstance(email, str):
        return False
    return email.strip().lower() == expected


if __name__ == "__main__":
    sys.stdout.write("SET\n" if expected_drive_owner_email() else "UNSET\n")
