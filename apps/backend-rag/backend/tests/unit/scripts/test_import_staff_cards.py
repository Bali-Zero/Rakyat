"""The import's process boundary (STAFF_CARD.md C10): the REAL launcher in a subprocess.

Two families of test. (1) The real worker with a fake database driver injected through a
`sitecustomize` (gated to the worker process) that misbehaves in every way a driver or a
library can. (2) A stub worker module of the SAME module name, found first through the
child's cwd, that writes whatever it likes on the result pipe. In both, a sentinel must
never reach the launcher's stdout or stderr, no traceback may appear, and the printed
outcome and exit code must match the truthfulness table of the spec, row for row.
"""

from __future__ import annotations

import ast
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.scripts import import_staff_cards as launcher

LAUNCHER = Path(launcher.__file__).resolve()
BACKEND_RAG = LAUNCHER.parents[2]
SENT = "ZQXSENTINEL7"
FIXED_BAD_ARGS = "import_staff_cards: invalid arguments (run with --help)"
LINE = re.compile(r"mode: (apply|dry-run)|[a-z_]+(:[a-z_]+)?: [0-9]+|committed: (yes|no|unknown)")

GHOST = json.dumps({"cards": [{"email": "fake.ghost@balizero.com"}]}).encode()  # resolves nobody
ONE = json.dumps({"cards": [{"email": "fake.one@balizero.com"}]}).encode()
EMPTY = json.dumps({"cards": []}).encode()


def _env(tmp_path: Path, extra: dict[str, str] | None = None, pythonpath: bool = False) -> dict:
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none",  # any real DB access fails
        "JWT_SECRET_KEY": "j" * 40,  # settings need a 32+ char value to import; unused here
        "API_KEYS": "k" * 12,
        "HRIMPORT_FAKE_SENTINEL": SENT,
        "STUB_SENTINEL": SENT,
        "IMPORT_STAFF_CARDS_TEST_TIMEOUT_S": "60",
    }
    if pythonpath:
        env["PYTHONPATH"] = str(tmp_path)
    return {**env, **(extra or {})}


def _popen(tmp_path, *, args=(), extra=None, py_flags=(), stdout=subprocess.PIPE, pythonpath=False):
    return subprocess.Popen(
        [sys.executable, *py_flags, str(LAUNCHER), *args],
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=_env(tmp_path, extra, pythonpath),
    )


def _run(tmp_path, stdin=b"", **kw) -> subprocess.CompletedProcess:
    proc = _popen(tmp_path, **kw)
    out, err = proc.communicate(stdin, timeout=120)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _check(run, *, committed: str, codes: list[str], rc: int, lines: tuple[str, ...] = ()) -> None:
    out, err = run.stdout.decode(), run.stderr.decode()
    assert SENT not in out + err and "Traceback" not in out + err
    out_lines = out.splitlines()
    assert all(LINE.fullmatch(line) for line in out_lines), out_lines  # launcher text only
    assert f"committed: {committed}" in out_lines
    assert err.splitlines() == [f"import_staff_cards: failed ({c})" for c in codes]
    assert run.returncode == rc
    for line in lines:
        assert line in out_lines


# ─── family 1: the real worker, a fake driver that misbehaves ───

_FAKE_DRIVER = """
import os, sys
if any("_import_staff_cards_worker" in a for a in sys.orig_argv):
    import importlib.abc, importlib.machinery, logging, signal, time, warnings
    mode = os.environ.get("HRIMPORT_FAKE_MODE", "ok")
    S = os.environ["HRIMPORT_FAKE_SENTINEL"]
    marker = os.environ.get("HRIMPORT_FAKE_MARKER")

    def touch():
        if marker:
            open(marker, "w").close()

    def noise():
        print(S)
        sys.stderr.write(S + "\\n")
        os.write(1, S.encode())
        os.write(2, S.encode())
        warnings.warn(S)
        logging.getLogger().warning(S)
        logging.log(60, S)
        logging.getLogger("backend.app").error(S)

    if mode == "import_noise":
        class Hook(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name != "backend.core.pg_json_codec":
                    return None
                spec = importlib.machinery.PathFinder.find_spec(name, path)
                real = spec.loader.exec_module

                def exec_module(module):
                    noise()
                    real(module)

                spec.loader.exec_module = exec_module
                return spec

        sys.meta_path.insert(0, Hook())

    import asyncpg

    class Tx:
        async def start(self):
            pass

        async def commit(self):
            if mode == "commit":
                raise RuntimeError(S)

        async def rollback(self):
            pass

    class Conn:
        async def set_type_codec(self, *a, **k):
            if mode == "codec":
                raise RuntimeError(S)
            if mode == "noise":
                noise()

        async def execute(self, *a):
            pass

        async def fetch(self, *a):
            if mode == "txn":
                raise ValueError(S)
            return []

        async def fetchval(self, *a):
            return None

        def transaction(self):
            return Tx()

        async def close(self):
            if mode in ("close", "noise", "warn"):
                if mode == "close":
                    raise OSError(S)
                noise()
            elif mode == "exit_close0":
                raise SystemExit(0)
            elif mode == "exit_closes":
                raise SystemExit(S)
            elif mode == "exit_close7":
                raise SystemExit(7)
            elif mode == "kill_close":
                os.kill(os.getpid(), signal.SIGKILL)
            elif mode == "hang_close":
                touch()
                time.sleep(3600)

    async def connect(*a, **k):
        if mode in ("warn", "noise"):
            noise()
        if mode == "connect":
            raise asyncpg.InvalidPasswordError(S)
        if mode == "exit_connect0":
            raise SystemExit(0)
        if mode == "exit_connects":
            raise SystemExit(S)
        if mode == "exit_connect7":
            raise SystemExit(7)
        if mode == "kill_connect":
            os.kill(os.getpid(), signal.SIGKILL)
        if mode == "hang_connect":
            touch()
            time.sleep(3600)
        return Conn()

    asyncpg.connect = connect
"""


def _fake(tmp_path, mode, stdin, args=("--apply", "--skip-not-found"), extra=None):
    (tmp_path / "sitecustomize.py").write_text(_FAKE_DRIVER)
    return _run(
        tmp_path,
        stdin,
        args=args,
        extra={"HRIMPORT_FAKE_MODE": mode, **(extra or {})},
        pythonpath=True,
    )


APPLY_SKIP = ("--apply", "--skip-not-found")
C_YES = ("resolved: 0", "not_found: 1", "applied: 0")


@pytest.mark.parametrize("mode", ["warn", "import_noise", "noise"])
def test_family1_noise_from_the_driver_a_dependency_or_logging_never_reaches_a_stream(
    tmp_path, mode
):
    """Warnings, import-time output, logging.log(60, ...), prints and raw writes."""
    run = _fake(tmp_path, mode, GHOST)
    _check(run, committed="yes", codes=[], rc=0, lines=C_YES)


@pytest.mark.parametrize(
    ("mode", "stdin", "args", "committed", "codes", "lines"),
    [
        # connect / codec / transaction / commit / close, each failing with the sentinel
        ("connect", GHOST, APPLY_SKIP, "no", ["connect_failed"], ()),
        ("codec", GHOST, APPLY_SKIP, "no", ["unexpected"], ()),
        ("txn", ONE, APPLY_SKIP, "no", ["unexpected"], ()),  # rolled back, then reported
        ("commit", GHOST, APPLY_SKIP, "unknown", ["unexpected"], ()),  # may have committed
        ("close", GHOST, APPLY_SKIP, "yes", ["close_failed"], C_YES),  # close after commit
        ("close", GHOST, ("--apply",), "no", ["close_failed"], ("blocked:not_found: 1",)),
        # SystemExit(0), SystemExit(sentinel), SystemExit(7) at connect and at close
        ("exit_connect0", GHOST, APPLY_SKIP, "no", ["connect_failed"], ()),
        ("exit_connects", GHOST, APPLY_SKIP, "no", ["connect_failed"], ()),
        ("exit_connect7", GHOST, APPLY_SKIP, "no", ["connect_failed"], ()),
        ("exit_close0", GHOST, APPLY_SKIP, "yes", ["close_failed"], C_YES),
        ("exit_closes", GHOST, APPLY_SKIP, "yes", ["close_failed"], C_YES),
        ("exit_close7", GHOST, APPLY_SKIP, "yes", ["close_failed"], C_YES),
        ("exit_close7", GHOST, ("--apply",), "no", ["close_failed"], ("blocked:not_found: 1",)),
        # killed by SIGKILL before and after the decision
        ("kill_connect", GHOST, APPLY_SKIP, "unknown", ["worker_killed"], ()),
        ("kill_close", GHOST, APPLY_SKIP, "yes", ["worker_died_after_decision"], C_YES),
        # input: invalid JSON, wrong shapes, all carrying the sentinel
        ("ok", b'{"cards": [ ' + SENT.encode(), APPLY_SKIP, "no", ["bad_input"], ()),
        ("ok", json.dumps([SENT]).encode(), APPLY_SKIP, "no", [], ("rejected:bad_document: 1",)),
        ("ok", json.dumps(SENT).encode(), APPLY_SKIP, "no", [], ("rejected:bad_document: 1",)),
        ("ok", b"\xff\xfe" + SENT.encode(), APPLY_SKIP, "no", ["bad_input"], ()),
        # dry-run: the worker never writes; an unknown outcome is still "no"
        ("ok", GHOST, (), "no", [], ("not_found: 1", "would_update: 0")),
        ("connect", GHOST, (), "no", ["connect_failed"], ()),
        ("kill_connect", GHOST, (), "no", ["worker_killed"], ()),
    ],
)
def test_family1_every_failure_path_prints_only_launcher_text_and_the_table_row(
    tmp_path, mode, stdin, args, committed, codes, lines
):
    run = _fake(tmp_path, mode, stdin, args)
    rc = 0 if (mode == "ok" and not args and not codes) else 1
    _check(run, committed=committed, codes=codes, rc=rc, lines=lines)


def test_family1_a_clean_apply_exits_zero_and_says_committed_yes(tmp_path):
    run = _fake(tmp_path, "ok", EMPTY, ("--apply",))
    _check(run, committed="yes", codes=[], rc=0)


@pytest.mark.parametrize(
    ("mode", "committed", "codes"),
    [
        ("hang_connect", "unknown", ["timeout"]),
        ("hang_close", "yes", ["timeout", "worker_died_after_decision"]),
    ],
)
def test_family1_a_hung_worker_is_ended_at_the_deadline_and_reported(
    tmp_path, mode, committed, codes
):
    started = time.monotonic()
    run = _fake(tmp_path, mode, GHOST, extra={"IMPORT_STAFF_CARDS_TEST_TIMEOUT_S": "2"})
    assert time.monotonic() - started < 40
    _check(run, committed=committed, codes=codes, rc=1)


@pytest.mark.parametrize(
    ("mode", "committed", "codes"),
    [
        ("hang_connect", "unknown", ["interrupted"]),  # SIGINT before the decision
        ("hang_close", "yes", ["interrupted", "worker_died_after_decision"]),  # after it
    ],
)
def test_family1_sigint_to_the_launcher_before_and_after_the_decision(
    tmp_path, mode, committed, codes
):
    (tmp_path / "sitecustomize.py").write_text(_FAKE_DRIVER)
    marker = tmp_path / "in-flight"
    proc = _popen(
        tmp_path,
        args=APPLY_SKIP,
        extra={"HRIMPORT_FAKE_MODE": mode, "HRIMPORT_FAKE_MARKER": str(marker)},
        pythonpath=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(GHOST)
    proc.stdin.close()
    proc.stdin = None  # communicate() must not flush a closed handle
    for _ in range(600):
        if marker.exists():
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("the worker never reached the hang point")
    time.sleep(0.3)  # let a decision already written reach the launcher
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=60)
    run = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    _check(run, committed=committed, codes=codes, rc=1, lines=C_YES if committed == "yes" else ())


def test_family1_the_launcher_finds_its_worker_from_any_cwd(tmp_path):
    """No fake driver, cwd elsewhere: the real worker starts and answers bad_input."""
    run = _run(tmp_path, b"{ not json " + SENT.encode(), args=("--apply",))
    _check(run, committed="no", codes=["bad_input"], rc=1)


# ─── family 2: a stub worker of the SAME module name writes what it likes on the pipe ───

_STUB = """
import os, signal, sys, time
S = os.environ["STUB_SENTINEL"]
mode = os.environ["STUB_MODE"]
fd = int([a for a in sys.argv if a.startswith("--result-fd=")][0].split("=")[1])


def w(data):
    try:
        os.write(fd, data if isinstance(data, bytes) else data.encode())
    except OSError:
        pass


D = '{"phase":"decided","committed":true,"outcomes":{"applied":2}}\\n'
R = '{"phase":"decided","committed":false,"outcomes":{"rejected:invalid_fields":1}}\\n'
C = '{"phase":"closed","close_ok":true}\\n'
print(S)
sys.stderr.write(S + "\\n")
sys.stdout.flush()
sys.stderr.flush()
kill = lambda: os.kill(os.getpid(), signal.SIGKILL)
if mode == "good":
    w(D + C)
elif mode == "rejected":
    w(R + C)
elif mode == "close_false":
    w(D + '{"phase":"closed","close_ok":false}\\n')
elif mode == "d_kill":
    w(D); kill()
elif mode == "d_hang":
    w(D); time.sleep(3600)
elif mode == "d_exit0":
    w(D); sys.exit(0)
elif mode == "d_closed_exit3":
    w(D + C); sys.exit(3)
elif mode == "nothing_exit0":
    sys.exit(0)
elif mode == "nothing_exit3":
    sys.exit(3)
elif mode == "kill_first":
    kill()
elif mode == "hang_first":
    time.sleep(3600)
elif mode == "failed_only":
    w('{"phase":"failed","code":"unexpected"}\\n' + C)
elif mode == "garbage":
    w(S + "\\n")
elif mode == "oversized":
    w(b"a" * 200000 + b"\\n")
elif mode == "oversized_valid":
    w(C * 6000)
elif mode == "no_newline":
    w(D.strip())
elif mode == "unknown_key":
    w('{"phase":"decided","committed":true,"outcomes":{"salary":1}}\\n' + C)
elif mode == "unknown_extra_field":
    w('{"phase":"decided","committed":true,"outcomes":{"applied":1},"note":"x"}\\n' + C)
elif mode == "bad_code":
    w('{"phase":"failed","code":"leak"}\\n' + C)
elif mode == "negative":
    w('{"phase":"decided","committed":true,"outcomes":{"applied":-1}}\\n' + C)
elif mode == "huge":
    w('{"phase":"decided","committed":true,"outcomes":{"applied":99999999999}}\\n' + C)
elif mode == "over_bound":
    w('{"phase":"decided","committed":true,"outcomes":{"applied":9999999}}\\n' + C)
elif mode == "sentinel_string":
    w('{"phase":"failed","code":"' + S + '"}\\n' + C)
elif mode == "sentinel_in_phase":
    w('{"phase":"' + S + '","committed":true,"outcomes":{}}\\n' + C)
elif mode == "sentinel_in_key":
    w('{"phase":"decided","committed":true,"outcomes":{"' + S + '":1}}\\n' + C)
elif mode == "dup_decided":
    w(D + D + C)
elif mode == "d_garbage":
    w(D + S + "\\n")
elif mode == "after_closed":
    w(D + C + D)
elif mode == "echo":
    data = sys.stdin.buffer.read()
    n = len(data)
    a = 1 if "--apply" in sys.argv else 0
    k = 1 if "--skip-not-found" in sys.argv else 0
    w('{"phase":"decided","committed":false,"outcomes":{"resolved":%d,"not_found":%d,"applied":%d}}\\n' % (k, a, n) + C)
elif mode == "slow_reader":
    time.sleep(1.0)
    n = len(sys.stdin.buffer.read())
    w('{"phase":"decided","committed":true,"outcomes":{"applied":%d}}\\n' % n + C)
"""


def _stub(tmp_path, mode, stdin=EMPTY, args=("--apply",), **kw):
    pkg = tmp_path / "backend" / "scripts"
    pkg.mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "__init__.py").write_text("")
    (pkg / "_import_staff_cards_worker.py").write_text(_STUB)
    extra = {"STUB_MODE": mode, **kw.pop("extra", {})}
    return _run(tmp_path, stdin, args=args, extra=extra, **kw)


A2 = ("applied: 2",)


@pytest.mark.parametrize(
    ("mode", "args", "committed", "codes", "rc", "lines"),
    [
        # the truthfulness table, row by row (apply)
        ("good", ("--apply",), "yes", [], 0, A2),
        ("rejected", ("--apply",), "no", [], 1, ("rejected:invalid_fields: 1",)),
        ("close_false", ("--apply",), "yes", ["close_failed"], 1, A2),
        ("d_kill", ("--apply",), "yes", ["worker_died_after_decision"], 1, A2),
        ("d_exit0", ("--apply",), "yes", ["worker_died_after_decision"], 1, A2),
        ("d_closed_exit3", ("--apply",), "yes", ["worker_exit_nonzero"], 1, A2),
        ("nothing_exit0", ("--apply",), "unknown", ["no_result"], 1, ()),
        ("nothing_exit3", ("--apply",), "unknown", ["no_result"], 1, ()),
        ("kill_first", ("--apply",), "unknown", ["worker_killed"], 1, ()),
        ("failed_only", ("--apply",), "unknown", ["unexpected"], 1, ()),
        # the same rows for a dry-run: it never writes, so the answer is "no"
        ("good", (), "no", [], 0, A2),  # a dry-run never writes: a claim to the contrary is ignored
        ("rejected", (), "no", [], 0, ("rejected:invalid_fields: 1",)),
        ("nothing_exit0", (), "no", ["no_result"], 1, ()),
        ("kill_first", (), "no", ["worker_killed"], 1, ()),
        ("failed_only", (), "no", ["unexpected"], 1, ()),
    ],
)
def test_family2_the_truthfulness_table(tmp_path, mode, args, committed, codes, rc, lines):
    _check(_stub(tmp_path, mode, args=args), committed=committed, codes=codes, rc=rc, lines=lines)


@pytest.mark.parametrize(
    ("mode", "committed", "must_have"),
    [
        ("garbage", "unknown", ["protocol_error"]),
        ("oversized", "unknown", ["protocol_error"]),
        ("oversized_valid", "unknown", ["protocol_error"]),
        ("no_newline", "unknown", ["protocol_error"]),
        ("unknown_key", "unknown", ["protocol_error"]),
        ("unknown_extra_field", "unknown", ["protocol_error"]),
        ("bad_code", "unknown", ["protocol_error"]),
        ("negative", "unknown", ["protocol_error"]),
        ("huge", "unknown", ["protocol_error"]),
        ("over_bound", "unknown", ["protocol_error"]),
        ("sentinel_string", "unknown", ["protocol_error"]),
        ("sentinel_in_phase", "unknown", ["protocol_error"]),
        ("sentinel_in_key", "unknown", ["protocol_error"]),
        ("dup_decided", "yes", ["protocol_error"]),  # the first valid decision stands
        ("d_garbage", "yes", ["protocol_error", "worker_died_after_decision"]),
        ("after_closed", "yes", ["protocol_error"]),
    ],
)
def test_family2_anything_outside_the_closed_grammar_is_discarded_and_counted(
    tmp_path, mode, committed, must_have
):
    run = _stub(tmp_path, mode)
    out, err = run.stdout.decode(), run.stderr.decode()
    assert SENT not in out + err and "Traceback" not in out + err
    assert all(LINE.fullmatch(line) for line in out.splitlines()), out
    assert f"committed: {committed}" in out.splitlines()
    got = [
        line.removeprefix("import_staff_cards: failed (").rstrip(")") for line in err.splitlines()
    ]
    assert all(line.startswith("import_staff_cards: failed (") for line in err.splitlines())
    assert set(must_have) <= set(got) and run.returncode == 1


@pytest.mark.parametrize(
    ("mode", "committed", "codes", "lines"),
    [
        ("hang_first", "unknown", ["timeout"], ()),
        ("d_hang", "yes", ["timeout", "worker_died_after_decision"], A2),
    ],
)
def test_family2_a_hung_worker_times_out_before_and_after_the_decision(
    tmp_path, mode, committed, codes, lines
):
    run = _stub(tmp_path, mode, extra={"IMPORT_STAFF_CARDS_TEST_TIMEOUT_S": "1.5"})
    _check(run, committed=committed, codes=codes, rc=1, lines=lines)


def test_family2_the_worker_gets_the_raw_bytes_and_the_flags_and_no_more(tmp_path):
    raw = b"\xff\xfe\x00 not utf-8, not json " + SENT.encode()
    run = _stub(tmp_path, "echo", raw, args=("--apply", "--skip-not-found"))
    _check(
        run, committed="no", codes=[], rc=1,
        lines=(f"applied: {len(raw)}", "not_found: 1", "resolved: 1"),
    )  # fmt: skip
    run = _stub(tmp_path, "echo", raw, args=())
    assert "would_update: 0" in run.stdout.decode() and "not_found: 0" in run.stdout.decode()


def test_family2_a_large_input_does_not_deadlock_a_worker_that_reads_late(tmp_path):
    raw = b" " * 400_000
    run = _stub(tmp_path, "slow_reader", raw)
    _check(run, committed="yes", codes=[], rc=0, lines=("applied: 400000",))


def test_family2_the_launcher_runs_isolated_and_with_warnings_ignored(tmp_path):
    run = _stub(tmp_path, "good", py_flags=("-I", "-W", "ignore"))
    _check(run, committed="yes", codes=[], rc=0, lines=A2)
    run = _stub(tmp_path, "good", py_flags=("-W", "error"))
    _check(run, committed="yes", codes=[], rc=0, lines=A2)


def test_family2_a_closed_stdout_is_a_fixed_line_and_the_report_moves_to_stderr(tmp_path):
    read_end, write_end = os.pipe()
    os.close(read_end)  # nobody will ever read this stdout: the first write fails
    try:
        run = _stub(tmp_path, "good", stdout=write_end)
    finally:
        os.close(write_end)
    err = run.stderr.decode()
    assert run.returncode == 1 and SENT not in err and "Traceback" not in err
    assert err.splitlines()[-1] == "import_staff_cards: failed (output_closed)"
    assert "committed: yes" in err.splitlines() and "applied: 2" in err.splitlines()


@pytest.mark.parametrize("exc", ["SystemExit(0)", f"SystemExit({SENT!r})", "KeyboardInterrupt()"])
def test_an_exception_or_exit_anywhere_after_the_arguments_is_one_fixed_line(tmp_path, exc):
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('imp_launcher', {str(LAUNCHER)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"def boom(*a, **k): raise {exc}\n"
        "m._supervise = boom\n"
        "m.main(['--apply'])\n"
    )
    pkg = tmp_path / "backend" / "scripts"
    pkg.mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "__init__.py").write_text("")
    (pkg / "_import_staff_cards_worker.py").write_text(_STUB)
    run = subprocess.run(
        [sys.executable, "-I", "-c", code],
        input=EMPTY, capture_output=True, cwd=tmp_path, timeout=60,
        env=_env(tmp_path, {"STUB_MODE": "d_hang"}),
    )  # fmt: skip
    assert run.stdout == b""
    assert run.stderr == b"import_staff_cards: failed (internal_error)\n" and run.returncode == 1


# ─── argv and --help: nothing the operator typed is ever echoed ───


@pytest.mark.parametrize(
    "argv",
    [
        ["--apply", f"--npwp={SENT}"],  # unknown flag with a value
        [f"--apply={SENT}"],  # malformed value for a known flag
        [SENT],  # stray positional
        [f"--skip-not-foun={SENT}"],  # abbreviation attempt
        ["--apply", "--apply", SENT],  # repeated flag
        ["--skip", SENT],
        [f"--result-fd={SENT}"],
    ],
)
def test_every_parse_error_is_the_one_fixed_line_and_exit_2(tmp_path, argv):
    run = _run(tmp_path, EMPTY, args=argv)
    assert run.returncode == 2 and run.stdout == b""
    assert run.stderr.decode() == FIXED_BAD_ARGS + "\n"


def test_help_is_static_text(tmp_path):
    run = _run(tmp_path, b"", args=("--help",))
    out = run.stdout.decode()
    assert run.returncode == 0 and run.stderr == b""
    assert "--apply" in out and "--skip-not-found" in out and SENT not in out
    assert _run(tmp_path, b"", args=("--help", SENT)).stdout.decode().count(SENT) == 0


# ─── in-process unit checks of the launcher's own parts ───


def test_the_launcher_imports_the_standard_library_only():
    tree = ast.parse(LAUNCHER.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module
            roots.add(node.module.split(".")[0])
    assert roots and roots <= set(sys.stdlib_module_names), roots
    assert "print(" not in LAUNCHER.read_text().replace("# no print", "")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 900.0), ("", 900.0), ("0", 900.0), ("-5", 900.0), ("0.1", 900.0), ("nan", 900.0),
        ("inf", 900.0), ("abc", 900.0), ("900", 900.0), ("99999", 900.0), ("1e9", 900.0),
        ("0.2", 0.2), ("1.5", 1.5), ("899", 899.0),
    ],
)  # fmt: skip
def test_the_test_timeout_knob_can_only_shorten_the_deadline(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(launcher.TEST_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(launcher.TEST_TIMEOUT_ENV, value)
    assert launcher._timeout_seconds() == expected


def test_a_spawn_failure_is_committed_no_and_one_fixed_line(monkeypatch, capfd):
    saved = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)

    def boom(*a, **k):
        raise OSError(SENT)

    monkeypatch.setattr(launcher.subprocess, "Popen", boom)
    try:
        code = launcher.run_launcher(["--apply"])
    finally:
        signal.signal(signal.SIGINT, saved[0])
        signal.signal(signal.SIGTERM, saved[1])
    captured = capfd.readouterr()
    assert code == 1 and SENT not in captured.out + captured.err
    assert captured.out.splitlines() == ["mode: apply", "committed: no"]
    assert captured.err == "import_staff_cards: failed (spawn_failed)\n"


def _state(**kw) -> launcher.State:
    st = launcher.State()
    for key, value in kw.items():
        setattr(st, key, value)
    return st


D_YES = (True, {"applied": 2})
D_NO = (False, {"rejected:invalid_fields": 1})
D_NONE = (False, {})

# (apply, state, committed line, failure codes, exit): the table of STAFF_CARD.md C10
TABLE = [
    (True, _state(decided=D_YES, closed=True, returncode=0), "yes", [], 0),
    (True, _state(decided=D_NO, closed=True, returncode=0), "no", [], 1),
    (True, _state(decided=D_NONE, failed="connect_failed", closed=True, returncode=1), "no", ["connect_failed"], 1),
    (True, _state(decided=D_YES, closed=False, returncode=0), "yes", ["close_failed"], 1),
    (True, _state(decided=D_YES, returncode=-9), "yes", ["worker_died_after_decision"], 1),
    (True, _state(decided=D_YES, returncode=0), "yes", ["worker_died_after_decision"], 1),
    (True, _state(decided=D_YES, closed=True, returncode=3), "yes", ["worker_exit_nonzero"], 1),
    (True, _state(decided=D_YES, timed_out=True, returncode=-15), "yes", ["timeout", "worker_died_after_decision"], 1),
    (True, _state(decided=D_YES, interrupted=True, returncode=-15), "yes", ["interrupted", "worker_died_after_decision"], 1),
    (True, _state(decided=D_YES, protocol_error=True, returncode=-15), "yes", ["protocol_error", "worker_died_after_decision"], 1),
    (True, _state(returncode=-9), "unknown", ["worker_killed"], 1),
    (True, _state(returncode=0), "unknown", ["no_result"], 1),
    (True, _state(returncode=1), "unknown", ["no_result"], 1),
    (True, _state(failed="unexpected", closed=True, returncode=1), "unknown", ["unexpected"], 1),
    (True, _state(timed_out=True, returncode=-15), "unknown", ["timeout"], 1),
    (True, _state(interrupted=True, returncode=-15), "unknown", ["interrupted"], 1),
    (True, _state(protocol_error=True, returncode=-15), "unknown", ["protocol_error"], 1),
    (True, _state(nothing_ran=True, spawn_failed=True), "no", ["spawn_failed"], 1),
    (True, _state(nothing_ran=True, failed="bad_input"), "no", ["bad_input"], 1),
    (False, _state(decided=D_NONE, closed=True, returncode=0), "no", [], 0),
    (False, _state(decided=D_NO, closed=True, returncode=0), "no", [], 0),
    (False, _state(decided=D_NONE, closed=True, returncode=3), "no", ["worker_exit_nonzero"], 1),
    (False, _state(returncode=-9), "no", ["worker_killed"], 1),
    (False, _state(returncode=0), "no", ["no_result"], 1),
    (False, _state(timed_out=True, returncode=-15), "no", ["timeout"], 1),
]  # fmt: skip


@pytest.mark.parametrize(("apply", "state", "committed", "codes", "exit_code"), TABLE)
def test_the_truthfulness_table_row_by_row(apply, state, committed, codes, exit_code):
    lines, got_codes, got_exit = launcher.build_report(apply, state)
    assert f"committed: {committed}" in lines
    assert lines[0] == f"mode: {'apply' if apply else 'dry-run'}"
    assert got_codes == codes and got_exit == exit_code
    assert all(LINE.fullmatch(line) for line in lines)
    assert set(got_codes) <= set(launcher.WORKER_CODES) | set(launcher.LAUNCHER_CODES)


def test_exit_zero_needs_a_decision_a_clean_close_a_clean_exit_and_no_rejected_card():
    clean = _state(decided=D_YES, closed=True, returncode=0)
    assert launcher.build_report(True, clean)[2] == 0
    for broken in (
        _state(decided=None, closed=True, returncode=0),
        _state(decided=D_YES, closed=None, returncode=0),
        _state(decided=D_YES, closed=False, returncode=0),
        _state(decided=D_YES, closed=True, returncode=1),
        _state(decided=(True, {"applied": 1, "rejected:db_error": 1}), closed=True, returncode=0),
        _state(decided=D_YES, closed=True, returncode=0, protocol_error=True),
    ):
        assert launcher.build_report(True, broken)[2] == 1


def test_a_result_is_taken_only_when_it_matches_the_closed_grammar():
    st = launcher.State()
    st.feed(b'{"phase":"decided","committed":true,"outcomes":{"applied":2,"not_found":1}}\n')
    assert st.decided == (True, {"applied": 2, "not_found": 1}) and not st.protocol_error
    for bad in (
        b'{"phase":"decided","committed":true,"outcomes":{"applied":1,"applied":1}}\n',
        b'{"phase":"decided", "committed":true,"outcomes":{}}\n',  # whitespace
        b'{"phase":"decided","committed":"true","outcomes":{}}\n',
        b'{"phase":"decided","committed":true,"outcomes":{"applied":01}}\n',
        b'{"phase":"decided","committed":true,"outcomes":{"applied":1.5}}\n',
        b'{"phase":"decided","committed":true,"outcomes":{"applied":1000001}}\n',
        b'{"phase":"closed","close_ok":1}\n',
        b'{"phase":"failed","code":"connect_failed","x":1}\n',
        b"\n",
        b"[]\n",
    ):
        st = launcher.State()
        st.feed(bad)
        assert st.protocol_error and st.decided is None and st.failed is None and st.closed is None
    st = launcher.State()
    st.feed(b'{"phase":"closed","close_ok":tr')
    st.end_of_stream()
    assert st.protocol_error and st.closed is None


def test_every_launcher_literal_is_ascii_and_closed():
    for name in ("OUTCOME_KEYS", "WORKER_CODES", "LAUNCHER_CODES"):
        values = getattr(launcher, name)
        assert len(set(values)) == len(values)
        assert all(re.fullmatch(r"[a-z_:]+", v) for v in values)
