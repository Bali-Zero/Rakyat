"""A test-owned ROOT for the import launcher.

The launcher pins the worker's code origin to ITS OWN tree: the directory three levels above
its resolved path. A test therefore builds a temporary root that holds a copy of the launcher
(and of the worker, or a stub in its place), symlinks to the real `backend` package entries,
and optionally a fake `asyncpg` package next to `backend`. Production's root contains no such
fake, and nothing in the environment can put one there: the worker runs isolated (`-I`).
"""

from __future__ import annotations

import shutil
from pathlib import Path

REAL_BACKEND = Path(__file__).resolve().parents[2]
REAL_SCRIPTS = REAL_BACKEND / "scripts"
LAUNCHER_NAME = "import_staff_cards.py"
WORKER_NAME = "_import_staff_cards_worker.py"

# A drop-in `asyncpg`: it runs the real package, then misbehaves as HRIMPORT_FAKE_MODE says.
FAKE_ASYNCPG = """
import os
import sys

_real = os.environ["HRIMPORT_REAL_ASYNCPG"]
__path__ = [_real]
exec(compile(open(_real + "/__init__.py").read(), _real + "/__init__.py", "exec"), globals())

import logging
import signal
import time
import warnings

_mode = os.environ.get("HRIMPORT_FAKE_MODE", "ok")
_S = os.environ.get("HRIMPORT_FAKE_SENTINEL", "")
_marker = os.environ.get("HRIMPORT_FAKE_MARKER")
_real_connect = connect
from . import connection as _connmod  # noqa: E402

_real_connection = _connmod.Connection


def _touch():
    if _marker:
        open(_marker, "w").close()


def _noise():
    print(_S)
    sys.stderr.write(_S + "\\n")
    os.write(1, _S.encode())
    os.write(2, _S.encode())
    warnings.warn(_S)
    logging.getLogger().warning(_S)
    logging.log(60, _S)
    logging.getLogger("backend.app").error(_S)


if _mode == "import_noise":
    _noise()

if _mode == "tty":
    _report = os.environ["HRIMPORT_TTY_REPORT"]
    try:
        _fd = os.open("/dev/tty", os.O_RDWR)
        os.write(_fd, (_S + "\\n").encode())
        open(_report, "w").write("opened")
    except OSError as _exc:
        open(_report, "w").write(str(_exc.errno))
    try:
        _f = open("/dev/tty", "w")
        _f.write(_S + "\\n")
        _f.flush()
        open(_report + ".w", "w").write("opened")
    except OSError as _exc:
        open(_report + ".w", "w").write(str(_exc.errno))


class _Tx:
    async def start(self):
        pass

    async def commit(self):
        if _mode == "commit":
            raise RuntimeError(_S)

    async def rollback(self):
        pass


class _Conn:
    async def set_type_codec(self, *a, **k):
        if _mode == "codec":
            raise RuntimeError(_S)
        if _mode == "noise":
            _noise()

    async def execute(self, *a):
        pass

    async def fetch(self, *a):
        if _mode == "txn":
            raise ValueError(_S)
        if _mode == "second_lookup_fails":
            self.n = getattr(self, "n", 0) + 1
            if self.n == 2:
                raise ValueError(_S)
        return []

    async def fetchval(self, *a):
        return None

    def transaction(self):
        return _Tx()

    async def close(self):
        if _mode == "close":
            raise OSError(_S)
        if _mode in ("noise", "warn"):
            _noise()
        elif _mode == "exit_close0":
            raise SystemExit(0)
        elif _mode == "exit_closes":
            raise SystemExit(_S)
        elif _mode == "exit_close7":
            raise SystemExit(7)
        elif _mode == "kill_close":
            os.kill(os.getpid(), signal.SIGKILL)
        elif _mode == "hang_close":
            _touch()
            time.sleep(3600)


async def _fake_connect(*a, **k):
    if _mode in ("warn", "noise"):
        _noise()
    if _mode == "connect":
        raise InvalidPasswordError(_S)
    if _mode == "exit_connect0":
        raise SystemExit(0)
    if _mode == "exit_connects":
        raise SystemExit(_S)
    if _mode == "exit_connect7":
        raise SystemExit(7)
    if _mode == "kill_connect":
        os.kill(os.getpid(), signal.SIGKILL)
    if _mode == "hang_connect":
        _touch()
        time.sleep(3600)
    return _Conn()


if _mode == "rename":  # a real driver; the member is renamed by a second connection just
    _orig_fetchval = _real_connection.fetchval  # before the row lock is taken

    async def _fetchval(self, query, *args, **kw):
        if "FOR SHARE" in query:
            other = await _real_connect(os.environ["HRIMPORT_RENAME_DSN"])
            try:
                await other.execute(
                    "UPDATE team_members SET email = $2 WHERE id = $1",
                    args[0],
                    "fake.renamed@balizero.com",
                )
            finally:
                await other.close()
        return await _orig_fetchval(self, query, *args, **kw)

    _real_connection.fetchval = _fetchval
else:
    connect = _fake_connect
"""


def build_root(tmp: Path, *, worker_source: str | None = None, fake_asyncpg: bool = False) -> Path:
    """Return the path of the launcher copy inside a fresh test root."""
    root = next(r for r in (tmp / f"root{n}" for n in range(1000)) if not r.exists())
    scripts = root / "backend" / "scripts"
    scripts.mkdir(parents=True)
    for entry in REAL_BACKEND.iterdir():
        if entry.name not in ("scripts", "__pycache__"):
            (root / "backend" / entry.name).symlink_to(entry)
    shutil.copy2(REAL_SCRIPTS / LAUNCHER_NAME, scripts / LAUNCHER_NAME)
    if worker_source is None:
        shutil.copy2(REAL_SCRIPTS / WORKER_NAME, scripts / WORKER_NAME)
    else:
        (scripts / WORKER_NAME).write_text(worker_source)
    if fake_asyncpg:
        (root / "asyncpg").mkdir()
        (root / "asyncpg" / "__init__.py").write_text(FAKE_ASYNCPG)
    return scripts / LAUNCHER_NAME
