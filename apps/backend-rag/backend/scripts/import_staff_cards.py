"""Staff HR card import: the LAUNCHER, the only thing an operator runs.

    python backend/scripts/import_staff_cards.py            # dry-run, counts only
    python backend/scripts/import_staff_cards.py --apply    # one transaction, all or nothing
    python backend/scripts/import_staff_cards.py --apply --skip-not-found

The card document is read from STDIN. This file imports the standard library only and
does the real work in a child process (`_import_staff_cards_worker.py`) whose stdout and
stderr are discarded; the child reports over one private pipe, and this launcher prints
ONLY its own literals and integers rebuilt from a strictly validated result. No byte that
came from the child, the input, the database or a library is ever printed. The contract,
its table of outcomes and its limits: backend/app/services/hr/STAFF_CARD.md, clause C10.
"""

import warnings

if __name__ == "__main__":  # nothing of ours or a library's may speak: set before any import
    warnings.simplefilter("ignore")

import os
import re
import selectors
import signal
import subprocess
import sys
import time

# ─── the closed sets: everything the launcher can ever print besides integers ───

BASE_KEYS_APPLY = ("resolved", "not_found", "applied")
BASE_KEYS_DRY = ("resolved", "not_found", "would_update", "would_insert")

# Outcome keys the worker may report (a count per key). A key outside this tuple makes the
# whole record a protocol failure. Per-card rejection reasons are `rejected:<reason>`.
OUTCOME_KEYS = (
    "resolved",
    "not_found",
    "applied",
    "would_update",
    "would_insert",
    "blocked:not_found",
    "rejected:bad_shape",
    "rejected:unknown_key",
    "rejected:email_missing",
    "rejected:invalid_ptkp",
    "rejected:invalid_profile",
    "rejected:invalid_fields",
    "rejected:ambiguous_email",
    "rejected:member_changed",
    "rejected:not_applied",
    "rejected:bad_document",
    "rejected:hire_date_required",
    "rejected:invalid_input",
    "rejected:profile_not_object",
    "rejected:db_error",
    "rejected:lock_timeout",
)

# Failure codes the worker may carry in its result.
WORKER_CODES = (
    "import_failed",
    "bad_input",
    "connect_failed",
    "unexpected",
    "db_error",
    "lock_timeout",
    "hire_date_required",
    "invalid_input",
    "profile_not_object",
)

# Failure lines the launcher itself may write (with the worker's codes, the whole closed set).
LAUNCHER_CODES = (
    "spawn_failed",
    "timeout",
    "interrupted",
    "worker_killed",
    "no_result",
    "protocol_error",
    "worker_died_after_decision",
    "close_failed",
    "worker_exit_nonzero",
    "output_closed",
    "internal_error",
)

MAX_COUNT = 1_000_000
MAX_RESULT_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 15 * 60
TEST_TIMEOUT_ENV = "IMPORT_STAFF_CARDS_TEST_TIMEOUT_S"

_BAD_ARGS = b"import_staff_cards: invalid arguments (run with --help)\n"
_HELP = (
    b"usage: import_staff_cards.py [--apply] [--skip-not-found]\n"
    b"  reads the card document (JSON) from stdin; prints counts and fixed codes only\n"
    b"  --apply           write in one transaction, all or nothing (default is a dry-run)\n"
    b"  --skip-not-found  with --apply: skip emails that resolve to no staff member\n"
)
_WORKER_MODULE = "backend.scripts._import_staff_cards_worker"
_BACKEND_RAG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── the result grammar: one record per line, compact, closed literals, bounded integers ───

_INT = rb"(?:0|[1-9][0-9]{0,6})"
_KEYS = rb"(?:" + rb"|".join(re.escape(k.encode("ascii")) for k in OUTCOME_KEYS) + rb")"
_CODES = rb"(?:" + rb"|".join(re.escape(c.encode("ascii")) for c in WORKER_CODES) + rb")"
_PAIR = rb'"' + _KEYS + rb'":' + _INT
_BOOL = rb"(?:true|false)"
_DECIDED = re.compile(
    rb'\{"phase":"decided","committed":('
    + _BOOL
    + rb'),"outcomes":\{((?:'
    + _PAIR
    + rb"(?:,"
    + _PAIR
    + rb")*)?)\}\}"
)
_FAILED = re.compile(rb'\{"phase":"failed","code":"(' + _CODES + rb')"\}')
_CLOSED = re.compile(rb'\{"phase":"closed","close_ok":(' + _BOOL + rb")\}")
_ONE_PAIR = re.compile(rb'"([^"]+)":([0-9]+)')


class State:
    """What the launcher knows. Filled only from validated records and its own observations."""

    def __init__(self) -> None:
        self.decided: tuple[bool, dict[str, int]] | None = None
        self.failed: str | None = None
        self.closed: bool | None = None
        self.protocol_error = False
        self.nothing_ran = False  # the worker was never started: nothing can have been written
        self.spawn_failed = False
        self.timed_out = False
        self.interrupted = False
        self.returncode: int | None = None
        self.buffer = b""
        self.received = 0

    def accept(self, line: bytes) -> None:
        """Take one result line if, and only if, it matches the closed grammar."""
        if self.closed is not None:  # nothing may follow the last phase
            self.protocol_error = True
            return
        match = _DECIDED.fullmatch(line)
        if match is not None:
            outcomes: dict[str, int] = {}
            for raw_key, raw_value in _ONE_PAIR.findall(match.group(2)):
                key, value = raw_key.decode("ascii"), int(raw_value)
                if key in outcomes or value > MAX_COUNT:
                    self.protocol_error = True
                    return
                outcomes[key] = value
            if self.decided is not None:
                self.protocol_error = True
                return
            self.decided = (match.group(1) == b"true", outcomes)
            return
        match = _FAILED.fullmatch(line)
        if match is not None:
            if self.failed is not None:
                self.protocol_error = True
                return
            self.failed = match.group(1).decode("ascii")
            return
        match = _CLOSED.fullmatch(line)
        if match is not None:
            self.closed = match.group(1) == b"true"
            return
        self.protocol_error = True  # anything else is discarded, never echoed

    def feed(self, data: bytes) -> None:
        self.received += len(data)
        if self.received > MAX_RESULT_BYTES:
            self.protocol_error = True
            self.buffer = b""
            return
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.accept(line)

    def end_of_stream(self) -> None:
        if self.buffer:  # a record without its terminator is not a record
            self.protocol_error = True
            self.buffer = b""


# ─── the truthfulness table, as code: State -> (stdout lines, failure codes, exit code) ───


def build_report(apply: bool, st: State) -> tuple[list[str], list[str], int]:
    lines = [f"mode: {'apply' if apply else 'dry-run'}"]
    base = BASE_KEYS_APPLY if apply else BASE_KEYS_DRY
    if st.decided is not None:
        committed, outcomes = st.decided
        committed = committed and apply  # a dry-run never writes, whatever a worker claims
        for key in OUTCOME_KEYS:
            if key in base or outcomes.get(key, 0):
                lines.append(f"{key}: {outcomes.get(key, 0)}")
        lines.append(f"committed: {'yes' if committed else 'no'}")
    elif st.nothing_ran or not apply:
        lines.append("committed: no")  # never started, or a dry-run, which never writes
    else:
        lines.append("committed: unknown")  # the transaction may or may not have committed

    codes: list[str] = []
    if st.failed is not None:
        codes.append(st.failed)
    flags = (
        (st.spawn_failed, "spawn_failed"),
        (st.interrupted, "interrupted"),
        (st.timed_out, "timeout"),
        (st.protocol_error, "protocol_error"),
    )
    codes += [code for present, code in flags if present]
    if st.decided is None and st.failed is None and not codes:
        killed = st.returncode is not None and st.returncode < 0
        codes.append("worker_killed" if killed else "no_result")
    if st.decided is not None and st.closed is None:
        codes.append("worker_died_after_decision")
    elif st.closed is False:
        codes.append("close_failed")
    elif st.closed is True and st.returncode != 0 and st.failed is None:
        codes.append("worker_exit_nonzero")

    clean = st.decided is not None and not codes and st.closed is True and st.returncode == 0
    if clean and apply:
        committed, outcomes = st.decided  # type: ignore[misc]
        clean = committed and not any(k.startswith(("rejected:", "blocked:")) for k in outcomes)
    return lines, codes, 0 if clean else 1


# ─── plumbing: nothing here can raise a message that names a value ───


def _write_all(fd: int, data: bytes) -> bool:
    view = memoryview(data)
    try:
        while view:
            written = os.write(fd, view)
            view = view[written:]
    except BaseException:  # a closed or full stream: the caller decides what that means
        return False
    return True


def _timeout_seconds() -> float:
    """Test-only knob: it can only SHORTEN the wall-clock limit, never remove or raise it."""
    try:
        value = float(os.environ.get(TEST_TIMEOUT_ENV, ""))
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT_S)
    if 0.2 <= value < DEFAULT_TIMEOUT_S:
        return value
    return float(DEFAULT_TIMEOUT_S)


def parse_args(argv: list[str]) -> tuple[bool, bool] | int:
    """(apply, skip_not_found), or the exit code to end with. Never echoes an argument."""
    apply = skip = False
    for arg in argv:
        if arg in ("--help", "-h"):
            _write_all(1, _HELP)
            return 0
        if arg == "--apply" and not apply:
            apply = True
        elif arg == "--skip-not-found" and not skip:
            skip = True
        else:
            _write_all(2, _BAD_ARGS)
            return 2
    return apply, skip


def _read_stdin() -> bytes | None:
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(0, 1 << 20)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except BaseException:
        return None


_CHILD: subprocess.Popen[bytes] | None = None
_STOP = {"flag": False}


def _on_signal(signum: int, frame: object) -> None:
    _STOP["flag"] = True


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Terminate, then kill; never leaves the child behind."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    except BaseException:
        try:
            proc.kill()
        except BaseException:
            pass


def _supervise(proc: subprocess.Popen[bytes], payload: bytes, result_fd: int, st: State) -> None:
    os.set_blocking(result_fd, False)
    stdin_fd = proc.stdin.fileno() if proc.stdin else -1
    sel = selectors.DefaultSelector()
    sel.register(result_fd, selectors.EVENT_READ, "result")
    offset = 0
    if payload and stdin_fd >= 0:
        os.set_blocking(stdin_fd, False)
        sel.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
    elif proc.stdin:
        proc.stdin.close()
    result_open = True
    deadline = time.monotonic() + _timeout_seconds()
    while True:
        if _STOP["flag"]:
            st.interrupted = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            st.timed_out = True
            break
        if st.protocol_error:
            break
        for key, _ in sel.select(min(0.1, remaining)):
            if key.data == "result":
                try:
                    data = os.read(result_fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
                if data:
                    st.feed(data)
                else:
                    st.end_of_stream()
                    sel.unregister(result_fd)
                    result_open = False
            else:
                try:
                    offset += os.write(stdin_fd, payload[offset : offset + 65536])
                    done = offset >= len(payload)
                except BlockingIOError:
                    done = False
                except OSError:  # the worker stopped reading: it will say why, or not
                    done = True
                if done:
                    sel.unregister(stdin_fd)
                    proc.stdin.close()  # type: ignore[union-attr]
        if not result_open and proc.poll() is not None:
            break
        if not result_open:
            time.sleep(0.05)
    if proc.poll() is None:
        _reap(proc)
    # what the worker already wrote before it ended is still the truth: take it
    if result_open:
        end = time.monotonic() + 1.5
        while time.monotonic() < end and not st.protocol_error:
            try:
                data = os.read(result_fd, 65536)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            except OSError:
                break
            if not data:
                st.end_of_stream()
                break
            st.feed(data)
    st.returncode = proc.returncode


def _emit(apply: bool, st: State) -> int:
    lines, codes, exit_code = build_report(apply, st)
    report = ("\n".join(lines) + "\n").encode("ascii")
    failures = b"".join(f"import_staff_cards: failed ({c})\n".encode("ascii") for c in codes)
    if _write_all(1, report):
        if failures and not _write_all(2, failures):
            return 1
        return exit_code
    # stdout is gone: the report still goes to stderr, with the one fixed line saying why
    _write_all(2, report + failures + b"import_staff_cards: failed (output_closed)\n")
    return 1


def run_launcher(argv: list[str]) -> int:
    global _CHILD
    parsed = parse_args(argv)
    if isinstance(parsed, int):
        return parsed
    apply, skip = parsed
    st = State()
    payload = _read_stdin()
    if payload is None:
        st.nothing_ran = True
        st.failed = "bad_input"
        return _emit(apply, st)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    read_end, write_end = os.pipe()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (env.get("PYTHONPATH"), _BACKEND_RAG) if p)
    cmd = [sys.executable, "-m", _WORKER_MODULE, f"--result-fd={write_end}"]
    cmd += (["--apply"] if apply else []) + (["--skip-not-found"] if skip else [])
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_end,),
            env=env,
            bufsize=0,
        )
    except BaseException:
        os.close(write_end)
        os.close(read_end)
        st.spawn_failed = True
        st.nothing_ran = True
        return _emit(apply, st)
    _CHILD = proc
    os.close(write_end)
    try:
        _supervise(proc, payload, read_end, st)
    finally:
        _reap(proc)
        os.close(read_end)
    # from here on a signal must not cost the operator the truth about a decided batch
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    return _emit(apply, st)


def main(argv: list[str]) -> None:
    try:
        code = run_launcher(argv)
    except BaseException:  # SystemExit included: after argument parsing it is a failure
        try:
            if _CHILD is not None:
                _CHILD.kill()
        except BaseException:
            pass
        _write_all(2, b"import_staff_cards: failed (internal_error)\n")
        os._exit(1)
    os._exit(code)


if __name__ == "__main__":
    main(sys.argv[1:])
