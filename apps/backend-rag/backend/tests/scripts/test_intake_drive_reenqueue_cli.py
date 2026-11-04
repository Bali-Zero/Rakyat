"""scripts/intake_drive_reenqueue.py: the CLI around reenqueue_drive_files.

GUILT: an ids file that is not a TSV of Drive file ids (a header, a name, a path, a blank file, bytes that
are not text, a missing file), more than 500 ids, or no scope folder must exit 2 BEFORE any database or Drive
call, and say a line number, never what the line says; without ``--apply`` nothing is downloaded or enqueued;
a run with errors or stopped at the disk floor must not exit 0; a database or Drive service that cannot
start, or a fault that escapes the run, must exit 3 with a summary and never a message; stdout is ONE JSON
line of counts; no file or folder name, and no path, ever reaches stdout, stderr or a log.
INNOCENCE: only the first TSV column is read (the others may hold anything), blank lines, CRLF and a BOM are
fine, a repeated id counts once and is reported, exactly 500 ids run, ``missing`` alone is exit 0, the pool
is closed on every path.
PURE unit: the pool and the Drive service are fakes, the library is stubbed where the test is about the CLI
and real (over a fake Drive) where it is about what leaves the process. Synthetic ids only; every name is a
sentinel.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from backend.services.intake import drive_adapter as da
from backend.services.intake.enqueue import EnqueueResult

# test file: apps/backend-rag/backend/tests/scripts/<this>; parents[3] = apps/backend-rag
BACKEND_RAG = Path(__file__).resolve().parents[3]
SCRIPT = BACKEND_RAG / "scripts" / "intake_drive_reenqueue.py"
SCOPE = "SCOPE"
NAME = "ZQX-sentinel-6f1c"
SERVICE = "backend.services.integrations.service_account_drive_service.ServiceAccountDriveService"
KEYS = {
    "requested",
    "would_enqueue_new",
    "enqueued_new",
    "already_present",
    "skipped_native",
    "skipped_removed",
    "skipped_out_of_scope",
    "missing",
    "errors",
    "not_attempted",
    "disk_hold",
}


def fid(i: int) -> str:
    return f"file{i:07d}"


def counts(**given: int) -> dict[str, int]:
    return {**dict.fromkeys(KEYS, 0), **given}


class FakePool:
    """An asyncpg pool stand-in: the queue lookup answers from `refs`; close() is recorded."""

    def __init__(self) -> None:
        self.closed = False
        self.refs: set[str] = set()

    def acquire(self) -> Any:
        pool = self

        class _Conn:
            async def fetchval(self, query: str, *args: Any) -> Any:
                return args[0] in pool.refs

        class _Ctx:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()

    async def close(self) -> None:
        self.closed = True


class FakeDrive:
    """files.get over a dict of items (all the fields; a CLI test is not about the projection)."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, Exception] = {}
        self.service = types.SimpleNamespace(files=lambda: self)

    def get(self, *, fileId: str, fields: str, supportsAllDrives: bool = False) -> Any:
        drive = self

        class _Request:
            def execute(self) -> dict[str, Any]:
                if fileId in drive.errors:
                    raise drive.errors[fileId].with_traceback(None)
                if fileId not in drive.items:
                    raise _drive_error(404, "notFound")
                return dict(drive.items[fileId])

        return _Request()

    def add(
        self, item_id: str, name: str, parents: list[str], mime: str = "application/pdf"
    ) -> None:
        self.items[item_id] = {"id": item_id, "name": name, "mimeType": mime, "parents": parents}


def _drive_error(status: int, *reasons: str, message: str = "synthetic") -> HttpError:
    error = {
        "code": status,
        "message": message,
        "errors": [{"domain": "global", "reason": reason} for reason in reasons],
    }
    return HttpError(httplib2.Response({"status": status}), json.dumps({"error": error}).encode())


class Wired:
    """The CLI with a fake database pool and a fake Drive service; counts what it was asked to build."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, cli: Any) -> None:
        self.cli = cli
        self.pool = FakePool()
        self.drive = FakeDrive()
        self.built = {"pool": 0, "service": 0}
        self.pool_error: Exception | None = None
        self.pool_kwargs: dict[str, Any] = {}
        self.service_error: Exception | None = None
        self.calls: list[dict[str, Any]] = []  # what the stubbed library was asked
        self.result: dict[str, int] = counts()
        self.run_error: Exception | None = None
        wired = self

        async def create_pool(**kwargs: Any) -> FakePool:
            wired.built["pool"] += 1
            wired.pool_kwargs = kwargs
            if wired.pool_error is not None:
                raise wired.pool_error.with_traceback(None)
            return wired.pool

        def service(*args: Any, **kwargs: Any) -> FakeDrive:
            wired.built["service"] += 1
            if wired.service_error is not None:
                raise wired.service_error.with_traceback(None)
            return wired.drive

        monkeypatch.setattr(cli.asyncpg, "create_pool", create_pool)
        monkeypatch.setattr(SERVICE, service)

    def stub_library(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wired = self

        async def stub(
            pool: Any, drive_service: Any, scope_id: str, ids: list[str], *, apply: bool
        ) -> dict:
            wired.calls.append({"scope": scope_id, "ids": list(ids), "apply": apply})
            if wired.run_error is not None:
                raise wired.run_error.with_traceback(None)
            return {**wired.result, "requested": len(ids)}

        monkeypatch.setattr(self.cli, "reenqueue_drive_files", stub)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("intake_drive_reenqueue_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "path", list(sys.path))  # the script prepends the backend dir
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wired(cli: Any, monkeypatch: pytest.MonkeyPatch) -> Wired:
    monkeypatch.setenv("INTAKE_DRIVE_SCOPE_FOLDER_ID", SCOPE)
    monkeypatch.delenv("INTAKE_DRIVE_MIN_FREE_GIB", raising=False)
    return Wired(monkeypatch, cli)


def tsv(tmp_path: Path, content: str | bytes) -> str:
    path = tmp_path / "ids.tsv"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return str(path)


def run(cli: Any, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------- #
# Bad input: exit 2, nothing touched, a line number and never the line
# --------------------------------------------------------------------------- #
BAD = {
    "header": "file_id\tclass\n",
    "too-short": "short\n",
    "path": "../../etc/passwd\n",
    "a-name": f"{NAME} passport\n",
    "good-then-a-name": f"{fid(1)}\n{NAME} passport\n",
    "space-inside": "abcde fghij\n",
    "empty": "",
    "only-blank": "\n  \n\t\n",
    "not-text": b"\xff\xfe\x00 not utf-8 \x80",
}


@pytest.mark.parametrize("kind", list(BAD))
def test_a_bad_ids_file_exits_2_before_any_database_or_drive_call(
    kind: str,
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wired.stub_library(monkeypatch)
    code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, BAD[kind]), "--apply")
    assert code == 2
    assert wired.built == {"pool": 0, "service": 0} and wired.calls == []
    assert out == "" and NAME not in err and "passwd" not in err


# An id is checked exactly as written. Each text below is one valid id, or good lines around it, made wrong
# by padding or by a separator that is not a tab: the run must stop at that line, touching nothing.
PADDED = {
    "leading-space": (f" {fid(1)}\n", 1),
    "trailing-space": (f"{fid(1)} \n", 1),
    "trailing-space-then-a-tab": (f"{fid(1)} \t{NAME}\n", 1),
    "leading-tab": (f"\t{fid(1)}\n", 1),
    "both-sides": (f"  {fid(1)}  \n", 1),
    "no-break-space": (f"{fid(1)}\u00a0\n", 1),
    "padded-after-good-lines-and-blanks": (f"{fid(1)}\n\n   \n {fid(2)}\n", 4),
    "padded-last-line-without-a-newline": (f"{fid(1)}\n{fid(2)} ", 2),
    "space-separated-columns": (f"{fid(1)} {NAME}\n", 1),
}


@pytest.mark.parametrize("kind", list(PADDED))
def test_a_padded_id_is_bad_input_and_the_message_names_its_line(
    kind: str,
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wired.stub_library(monkeypatch)
    text, line_number = PADDED[kind]
    code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, text), "--apply")
    assert code == 2 and out == ""
    assert f"line {line_number} " in err  # "line 1 does not start..." and not "line 10 ..."
    assert wired.built == {"pool": 0, "service": 0} and wired.calls == []  # no Drive, no database
    assert NAME not in err and fid(1) not in err and fid(2) not in err  # a number, never the line


def test_read_ids_hands_back_the_ids_exactly_as_written(wired: Wired, tmp_path: Path) -> None:
    path = Path(tsv(tmp_path, f"{fid(1)}\t x \n\n \t\n{fid(2)}\n{fid(1)}\n"))
    assert wired.cli.read_ids(path) == ([fid(1), fid(2)], 1, None)
    padded_path = Path(tsv(tmp_path, f"{fid(1)}\n{fid(2)} \n"))
    ids, repeated, problem = wired.cli.read_ids(padded_path)
    assert (ids, repeated) == ([], 0) and problem == "line 2 does not start with a Drive file id"


def test_the_message_names_the_line_number(wired: Wired, tmp_path: Path, capsys: Any) -> None:
    code, _, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, BAD["good-then-a-name"]))
    assert code == 2 and "line 2" in err


def test_a_missing_file_or_a_directory_is_bad_input(
    wired: Wired, tmp_path: Path, capsys: Any
) -> None:
    assert run(wired.cli, capsys, "--ids-file", str(tmp_path / "nope.tsv"))[0] == 2
    assert run(wired.cli, capsys, "--ids-file", str(tmp_path))[0] == 2
    assert wired.built == {"pool": 0, "service": 0}


def test_a_pipe_is_not_an_ids_file_and_the_run_does_not_wait_on_it(tmp_path: Path) -> None:
    fifo = tmp_path / "ids.fifo"
    os.mkfifo(fifo)
    refused = _spawn("--ids-file", str(fifo), env_extra={"INTAKE_DRIVE_SCOPE_FOLDER_ID": SCOPE})
    assert refused.returncode == 2 and refused.stdout == ""


def test_an_ids_file_beyond_the_size_bound_is_bad_input(
    wired: Wired, tmp_path: Path, capsys: Any
) -> None:
    path = tmp_path / "big.tsv"  # one good id, then blank lines: only the size can refuse it
    path.write_bytes(fid(1).encode() + b"\n" + b"\n" * wired.cli.MAX_FILE_BYTES)
    assert run(wired.cli, capsys, "--ids-file", str(path))[0] == 2
    assert wired.built == {"pool": 0, "service": 0}


def test_more_than_500_ids_is_bad_input_and_exactly_500_runs(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    over = "\n".join(fid(i) for i in range(501)) + "\n"
    assert run(wired.cli, capsys, "--ids-file", tsv(tmp_path, over))[0] == 2
    assert wired.built == {"pool": 0, "service": 0} and wired.calls == []
    exact = "\n".join(fid(i) for i in range(500)) + "\n"
    assert run(wired.cli, capsys, "--ids-file", tsv(tmp_path, exact))[0] == 0
    assert len(wired.calls[0]["ids"]) == 500


@pytest.mark.parametrize("value", [None, "", "   "])
def test_without_a_scope_folder_nothing_runs(
    value: str | None, wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    if value is None:
        monkeypatch.delenv("INTAKE_DRIVE_SCOPE_FOLDER_ID")
    else:
        monkeypatch.setenv("INTAKE_DRIVE_SCOPE_FOLDER_ID", value)
    code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert code == 2 and out == "" and "INTAKE_DRIVE_SCOPE_FOLDER_ID" in err
    assert wired.built == {"pool": 0, "service": 0}


def test_an_unknown_flag_or_a_missing_ids_file_argument_exits_2(wired: Wired) -> None:
    for argv in (["--apply"], ["--ids-file"], ["--ids-file", "x", "--force"], []):
        with pytest.raises(SystemExit) as stopped:
            wired.cli.main(argv)
        assert stopped.value.code == 2
    assert wired.built == {"pool": 0, "service": 0}


def test_the_help_says_how_a_dry_run_can_differ_from_an_apply(wired: Wired, capsys: Any) -> None:
    with pytest.raises(SystemExit) as stopped:
        wired.cli.main(["--help"])
    assert stopped.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "asks the queue by ``source_ref``" in text
    assert "decides by ``intake_key``" in text
    assert "reads ``already_present`` in a dry run and still gets a new row on apply" in text
    assert wired.built == {"pool": 0, "service": 0}


def test_the_help_says_when_a_dry_run_may_be_followed_by_an_apply(
    wired: Wired, capsys: Any
) -> None:
    with pytest.raises(SystemExit) as stopped:
        wired.cli.main(["--help"])
    assert stopped.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert (
        "Go on to ``--apply`` only when ``errors`` and ``skipped_out_of_scope`` are both 0" in text
    )
    assert "(every count but ``requested`` and ``disk_hold``) add up to ``requested``" in text
    assert "otherwise stop and look" in text
    assert "``already_present`` is not a reason to stop" in text
    assert "Nor are ``missing``, ``skipped_removed`` and ``skipped_native``" in text
    assert "each one only means one id fewer for ``would_enqueue_new``" in text
    assert wired.built == {"pool": 0, "service": 0}


def test_every_count_the_help_names_in_its_rule_is_a_count_the_library_returns(
    wired: Wired, capsys: Any
) -> None:
    with pytest.raises(SystemExit):
        wired.cli.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    rule = text[text.index("Before an apply") : text.index("Usage")]
    named = set(re.findall(r"``([a-z_]+)``", rule)) - {"intake_key"}
    assert {"errors", "skipped_out_of_scope", "requested"} <= named
    assert named <= set(da._new_reenqueue_counters(0))


# --------------------------------------------------------------------------- #
# What the CLI hands to the library
# --------------------------------------------------------------------------- #
def test_a_dry_run_is_the_default_and_an_apply_must_be_asked_for(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    path = tsv(tmp_path, fid(1) + "\n")
    run(wired.cli, capsys, "--ids-file", path)
    run(wired.cli, capsys, "--ids-file", path, "--apply")
    assert [call["apply"] for call in wired.calls] == [False, True]
    assert all(call["scope"] == SCOPE for call in wired.calls)


def test_only_the_first_column_is_read_and_blank_lines_crlf_and_a_bom_are_fine(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    text = (
        f"\ufeff{fid(1)}\t{NAME}\tbirthday\r\n"
        "\r\n   \r\n\t\t\r\n \t \r\n"  # blank, spaces only, tabs only, both: all skipped
        f"{fid(2)}\t  {NAME}.pdf \t \r\n"  # spaces after the first tab are the other columns' business
        f"{fid(3)}"  # no tab, no final newline
    )
    code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, text))
    assert code == 0 and wired.calls[0]["ids"] == [fid(1), fid(2), fid(3)]
    assert NAME not in out + err


def test_a_repeated_id_counts_once_and_is_reported(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    text = "\n".join([fid(1), fid(2), fid(1), fid(1)]) + "\n"
    code, out, _ = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, text))
    assert code == 0 and wired.calls[0]["ids"] == [fid(1), fid(2)]
    assert json.loads(out)["duplicate_ids"] == 2


# --------------------------------------------------------------------------- #
# The output and the exit code
# --------------------------------------------------------------------------- #
def test_stdout_is_one_json_line_of_counts(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    wired.result = counts(would_enqueue_new=2)
    code, out, _ = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, f"{fid(1)}\n{fid(2)}\n"))
    assert code == 0 and out.count("\n") == 1 and out.endswith("\n")
    line = json.loads(out)
    assert set(line) == KEYS | {"apply", "duplicate_ids"}
    assert all(type(value) is int for value in line.values())
    assert (line["apply"], line["requested"], line["would_enqueue_new"]) == (0, 2, 2)


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (counts(would_enqueue_new=1), 0),
        (counts(enqueued_new=1, already_present=1), 0),
        (counts(missing=3), 0),  # a file that is gone is a finding, not a failure of the run
        (counts(skipped_native=1, skipped_removed=1, skipped_out_of_scope=1), 0),
        (counts(errors=1), 3),
        (counts(disk_hold=1, not_attempted=4), 3),
        (counts(enqueued_new=5, errors=1), 3),
    ],
)
def test_the_exit_code_says_whether_the_run_was_clean(
    result: dict[str, int],
    code: int,
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
) -> None:
    wired.stub_library(monkeypatch)
    wired.result = result
    got, out, _ = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"), "--apply")
    assert got == code
    assert json.loads(out)["apply"] == 1  # the line is printed whatever the code


def test_the_database_is_the_drains_unless_the_environment_says_otherwise(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    path = tsv(tmp_path, fid(1) + "\n")
    monkeypatch.delenv("LOCAL_DATABASE_URL", raising=False)
    run(wired.cli, capsys, "--ids-file", path)
    assert wired.pool_kwargs["dsn"] == "postgresql://nuzantara@127.0.0.1:5432/nuzantara_dev"
    monkeypatch.setenv("LOCAL_DATABASE_URL", "postgresql://example.invalid/intake_other")
    run(wired.cli, capsys, "--ids-file", path)
    assert wired.pool_kwargs["dsn"] == "postgresql://example.invalid/intake_other"


def test_the_pool_is_closed_after_a_run(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert wired.pool.closed is True


# --------------------------------------------------------------------------- #
# A run that cannot start, or a fault that escapes: exit 3, a summary, never a message
# --------------------------------------------------------------------------- #
def test_a_database_that_cannot_be_opened_is_exit_3_with_no_json_and_no_message(
    wired: Wired, tmp_path: Path, capsys: Any, caplog: pytest.LogCaptureFixture
) -> None:
    wired.pool_error = ConnectionRefusedError(f"cannot connect for {NAME}")
    with caplog.at_level(logging.DEBUG):
        code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert code == 3 and out == "" and wired.built["service"] == 0
    assert "ConnectionRefusedError" in caplog.text
    assert NAME not in caplog.text + err


def test_a_drive_service_that_cannot_be_built_is_exit_3_and_the_pool_is_closed(
    wired: Wired, tmp_path: Path, capsys: Any, caplog: pytest.LogCaptureFixture
) -> None:
    wired.service_error = ValueError(f"bad credentials for {NAME}")
    with caplog.at_level(logging.DEBUG):
        code, out, _ = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert code == 3 and out == "" and wired.pool.closed is True
    assert "ValueError" in caplog.text and NAME not in caplog.text


def test_a_fault_that_escapes_the_run_is_exit_3_and_logs_a_summary(
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wired.stub_library(monkeypatch)
    wired.run_error = PermissionError(13, "Permission denied", f"/blobs/{NAME}.pdf")
    with caplog.at_level(logging.DEBUG):
        code, out, err = run(
            wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"), "--apply"
        )
    assert code == 3 and out == "" and wired.pool.closed is True
    assert "PermissionError" in caplog.text
    assert NAME not in caplog.text + err
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)


# --------------------------------------------------------------------------- #
# PII: the whole path, the real library over a fake Drive, every fault carrying the sentinel
# --------------------------------------------------------------------------- #
def test_no_file_or_folder_name_and_no_path_reaches_stdout_stderr_or_a_log(
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    named = f"cannot read {NAME}"
    enqueued: list[dict[str, Any]] = []
    downloads: list[str] = []

    async def download(drive_service: Any, file_id: str, dest: Path) -> int:
        downloads.append(file_id)
        if file_id == fid(4):
            raise _drive_error(500, "backendError", message=named)
        if file_id == fid(5):
            raise PermissionError(13, "Permission denied", f"/blobs/{NAME}.pdf")
        return 7

    async def enqueue(pool: Any, **kwargs: Any) -> EnqueueResult:
        if kwargs["source_ref"] == f"drive:{fid(6)}":
            raise RuntimeError(named)
        enqueued.append(kwargs)
        return EnqueueResult(instance_id=1, queue_id=len(enqueued), was_new=True)

    async def no_pause(seconds: float) -> None:
        return None

    monkeypatch.setattr(da, "_download_file", download)
    monkeypatch.setattr(da, "enqueue", enqueue)
    monkeypatch.setattr(da, "_pause", no_pause)
    monkeypatch.setattr(da, "_free_bytes", lambda: 10**15)
    monkeypatch.setattr(da, "_BLOB_ROOT", tmp_path / "blobs")
    drive = wired.drive
    drive.add("DIR", f"{NAME}-dir", [SCOPE], "application/vnd.google-apps.folder")
    for i in range(1, 7):
        drive.add(fid(i), f"{NAME}-{i}.pdf", ["DIR"])
    drive.errors[fid(2)] = _drive_error(500, "backendError", message=named)
    drive.items.pop(fid(3))  # gone
    ids = "\n".join(f"{fid(i)}\t{NAME}" for i in range(1, 7)) + "\n"

    with caplog.at_level(logging.DEBUG):
        code_dry, out_dry, err_dry = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, ids))
        code_apply, out_apply, err_apply = run(
            wired.cli, capsys, "--ids-file", tsv(tmp_path, ids), "--apply"
        )

    assert code_dry == 3 and code_apply == 3  # the faults really happened
    dry, applied = json.loads(out_dry), json.loads(out_apply)
    assert (dry["would_enqueue_new"], dry["missing"], dry["errors"]) == (4, 1, 1)
    assert (applied["enqueued_new"], applied["missing"], applied["errors"]) == (1, 1, 4)
    assert downloads == [fid(1), fid(4), fid(5), fid(6)]
    assert NAME in enqueued[0]["source_path"]  # the name IS in play: the queue row only
    for text in (out_dry, out_apply, err_dry, err_apply, caplog.text):
        assert NAME not in text
    for text in (out_dry, out_apply):
        assert text.count("\n") == 1 and text.endswith("\n")
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)


# --------------------------------------------------------------------------- #
# The real entry point, in a real process
# --------------------------------------------------------------------------- #
def _spawn(*argv: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(BACKEND_RAG),
        "PYTHONDONTWRITEBYTECODE": "1",  # a test process leaves no __pycache__ behind it
    }
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        cwd=BACKEND_RAG,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_the_script_runs_as_a_program(tmp_path: Path) -> None:
    helped = _spawn("--help")
    assert helped.returncode == 0, helped.stderr
    assert "--ids-file" in helped.stdout and "--apply" in helped.stdout
    bad = _spawn("--ids-file", tsv(tmp_path, f"{NAME} passport\n"))
    assert bad.returncode == 2 and bad.stdout == "" and NAME not in bad.stderr
    no_scope = _spawn("--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert no_scope.returncode == 2 and no_scope.stdout == ""
    assert "INTAKE_DRIVE_SCOPE_FOLDER_ID" in no_scope.stderr
    missing = _spawn()
    assert missing.returncode == 2 and "--ids-file" in missing.stderr


# --------------------------------------------------------------------------- #
# Static: the script never logs or prints an exception, a traceback, a name
# --------------------------------------------------------------------------- #
def _violations(source: str) -> list[str]:
    """Logger or print calls that could carry an exception's text, a traceback or a name."""
    tree = ast.parse(source)
    caught = {n.name for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name}
    holders = {"file_name", "source_path", "dest", "line", "first"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "traceback":
                bad.append(f"line {node.lineno}: traceback.{node.attr}")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_log = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "logger"
        )
        if not (is_log or (isinstance(func, ast.Name) and func.id == "print")):
            continue
        if is_log and func.attr == "exception":
            bad.append(f"line {node.lineno}: logger.exception")
        if any(k.arg in ("exc_info", "stack_info") for k in node.keywords):
            bad.append(f"line {node.lineno}: exc_info or stack_info")
        used = {
            n.id
            for arg in (*node.args, *(k.value for k in node.keywords))
            for n in ast.walk(arg)
            if isinstance(n, ast.Name)
        }
        if used & (caught | holders):
            bad.append(f"line {node.lineno}: logs {sorted(used & (caught | holders))}")
    return bad


def test_the_script_never_logs_or_prints_an_exception_a_traceback_or_a_line_of_the_file() -> None:
    assert _violations(SCRIPT.read_text()) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "try:\n    pass\nexcept Exception as exc:\n    logger.exception('x')\n",
        "try:\n    pass\nexcept Exception as exc:\n    logger.error('x %s', exc)\n",
        "try:\n    pass\nexcept Exception as e:\n    print(f'x {e}')\n",
        "try:\n    pass\nexcept Exception as e:\n    logger.error('x %s', str(e))\n",
        "try:\n    pass\nexcept Exception:\n    logger.error('x', exc_info=True)\n",
        "import traceback\ntraceback.format_exc()\n",
        "logger.info('x %s', line)\n",
        "print(first)\n",
    ],
)
def test_the_checker_catches_every_shape_it_forbids(snippet: str) -> None:
    assert _violations(snippet)


def test_the_checker_lets_a_summary_and_a_number_through() -> None:
    ok = "try:\n    pass\nexcept Exception as exc:\n    why = summary(exc)\n    logger.error('x %s', why)\n"
    assert _violations(ok + "print(f'line {number} is bad')\n") == []
