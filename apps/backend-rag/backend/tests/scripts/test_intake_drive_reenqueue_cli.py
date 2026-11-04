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
R1, no Drive name reaches any handler, is held where a record is CREATED and never in a handler: the Drive
client logs ids, counts and class names since #9236, and the tests run its REAL constructor over a fake Drive
whose every name is the sentinel, with a foreign observer at six places. On a base without #9236 they are RED
(the client puts folder names and the raw text of a failed check in its start-up lines, and the raw text
of a failed decode of its credentials in its debug lines).
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import functools
import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import types
from collections.abc import Iterator
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
        self.close_calls = 0
        self.close_error: Exception | None = None
        self.lookup_error: Exception | None = None
        self.refs: set[str] = set()

    def acquire(self) -> Any:
        pool = self

        class _Conn:
            async def fetchval(self, query: str, *args: Any) -> Any:
                if pool.lookup_error is not None:
                    raise pool.lookup_error.with_traceback(None)
                return args[0] in pool.refs

        class _Ctx:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self.close_error is not None:
            raise self.close_error.with_traceback(None)


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
    assert "the OUTCOME keys add up to ``requested``, the number of distinct ids read" in text
    assert "(a repeated id is counted once, in ``duplicate_ids``)" in text
    assert "otherwise stop and look" in text
    assert "``already_present`` is not a reason to stop" in text
    assert "Nor are ``missing``, ``skipped_removed`` and ``skipped_native``" in text
    assert "each one only means one id fewer for ``would_enqueue_new``" in text
    assert wired.built == {"pool": 0, "service": 0}


def _help_keys(cli: Any, capsys: Any) -> tuple[set[str], set[str]]:
    """(the OUTCOME keys, the METADATA keys) as the help names them: the words in double backticks."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    outcome = text[text.index("The OUTCOME keys") : text.index("The METADATA keys")]
    metadata = text[text.index("The METADATA keys") : text.index("Go on to")]

    def named(part: str) -> set[str]:
        return set(re.findall(r"``([a-z_]+)``", part))

    return named(outcome), named(metadata)


def test_the_help_splits_every_json_key_into_outcome_or_metadata_and_none_is_left_out(
    wired: Wired, capsys: Any
) -> None:
    outcome, metadata = _help_keys(wired.cli, capsys)
    counters = set(da._new_reenqueue_counters(0))
    assert outcome == counters - {"requested", "disk_hold"}  # what became of each id
    assert metadata == {"apply", "duplicate_ids", "requested", "disk_hold"}
    assert not outcome & metadata  # no key is both
    assert outcome | metadata == counters | {"apply", "duplicate_ids"}  # and none is unnamed


def _patch_apply_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """What an apply needs besides Drive and the queue: a download that returns a size, an enqueue that records."""

    async def download(drive_service: Any, file_id: str, dest: Path) -> int:
        return 7

    async def enqueue(pool: Any, **kwargs: Any) -> EnqueueResult:
        return EnqueueResult(instance_id=1, queue_id=1, was_new=True)

    async def no_pause(seconds: float) -> None:
        return None

    monkeypatch.setattr(da, "_download_file", download)
    monkeypatch.setattr(da, "enqueue", enqueue)
    monkeypatch.setattr(da, "_pause", no_pause)
    monkeypatch.setattr(da, "_free_bytes", lambda: 10**15)
    monkeypatch.setattr(da, "_BLOB_ROOT", tmp_path / "blobs")


def _real_run(
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
    text: str,
    *,
    apply: bool,
    out_of_scope: bool = False,
    broken: bool = False,
) -> tuple[int, dict[str, int]]:
    """The CLI over the REAL library and a fake Drive: one JSON line of counts, as an operator reads it."""
    _patch_apply_path(monkeypatch, tmp_path)
    drive = wired.drive
    drive.add("DIR", f"{NAME}-dir", [SCOPE], "application/vnd.google-apps.folder")
    drive.add("ELSEWHERE", f"{NAME}-else", ["ROOT"], "application/vnd.google-apps.folder")
    drive.add("ROOT", "root", [], "application/vnd.google-apps.folder")
    drive.add(fid(1), f"{NAME}-1.pdf", ["DIR"])
    drive.add(fid(2), f"{NAME}-2.pdf", ["DIR"])
    drive.add(fid(4), f"{NAME}-4.doc", ["DIR"], "application/vnd.google-apps.document")  # native
    drive.add(fid(5), f"{NAME}-5.pdf", ["ELSEWHERE" if out_of_scope else "DIR"])
    if broken:
        drive.errors[fid(2)] = _drive_error(500, "backendError")
    argv = ["--ids-file", tsv(tmp_path, text), *(["--apply"] if apply else [])]
    code, out, _ = run(wired.cli, capsys, *argv)
    return code, json.loads(out)


def _go_on(line: dict[str, int], outcome: set[str]) -> bool:
    """The rule as the help states it, on the outcome keys only."""
    return (
        line["errors"] == 0
        and line["skipped_out_of_scope"] == 0
        and sum(line[key] for key in outcome) == line["requested"]
    )


REPEATING = {
    "a-clean-id-twice": f"{fid(1)}\n{fid(1)}\n",  # the gate's own example: requested=1, duplicate_ids=1
    "mixed-outcomes-and-a-repeat": f"{fid(1)}\n{fid(2)}\n{fid(1)}\n{fid(3)}\n{fid(4)}\n",
}


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
@pytest.mark.parametrize("kind", list(REPEATING))
def test_the_rule_in_the_help_holds_on_the_json_of_an_input_that_repeats_an_id(
    kind: str,
    apply: bool,
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
) -> None:
    help_outcome, help_metadata = _help_keys(wired.cli, capsys)
    code, line = _real_run(wired, monkeypatch, tmp_path, capsys, REPEATING[kind], apply=apply)
    assert set(line) == help_outcome | help_metadata  # the line has these keys and no others
    assert line["duplicate_ids"] >= 1  # the input did repeat an id
    assert sum(line[key] for key in help_outcome) == line["requested"]  # the rule's sum
    assert sum(line[key] for key in help_outcome | {"duplicate_ids"}) != line["requested"]
    assert sum(line.values()) != line["requested"]  # summing the whole line, literally, is wrong
    assert _go_on(line, help_outcome) is True and code == 0


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_the_rule_in_the_help_says_stop_for_an_error_or_a_file_out_of_scope(
    apply: bool,
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
) -> None:
    help_outcome, _ = _help_keys(wired.cli, capsys)
    text = f"{fid(1)}\n{fid(2)}\n{fid(1)}\n"
    code, errors = _real_run(wired, monkeypatch, tmp_path, capsys, text, apply=apply, broken=True)
    assert (errors["errors"], code) == (1, 3) and not _go_on(errors, help_outcome)
    text = f"{fid(1)}\n{fid(5)}\n"
    _, scope = _real_run(wired, monkeypatch, tmp_path, capsys, text, apply=apply, out_of_scope=True)
    assert scope["skipped_out_of_scope"] == 1 and not _go_on(scope, help_outcome)
    # the other outcomes are findings to read, not reasons to stop
    text = f"{fid(1)}\n{fid(3)}\n{fid(4)}\n"
    _, rest = _real_run(wired, monkeypatch, tmp_path, capsys, text, apply=apply)
    assert (rest["missing"], rest["skipped_native"]) == (1, 1) and _go_on(rest, help_outcome)


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


def test_a_pool_that_fails_to_close_after_a_clean_run_is_exit_3_with_the_counts_and_no_message(
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wired.stub_library(monkeypatch)
    wired.result = counts(would_enqueue_new=1)
    wired.pool.close_error = RuntimeError(f"cannot close the pool of {NAME}")
    with caplog.at_level(logging.DEBUG):
        code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert code == 3 and wired.pool.close_calls == 1
    assert out.count("\n") == 1 and json.loads(out)["would_enqueue_new"] == 1  # the counts are said
    assert "RuntimeError" in caplog.text and "closing the intake database failed" in caplog.text
    assert NAME not in caplog.text + out + err
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)


def test_a_pool_that_fails_to_close_after_a_failed_run_is_exit_3_and_both_faults_are_summaries(
    wired: Wired, tmp_path: Path, capsys: Any, caplog: pytest.LogCaptureFixture
) -> None:
    wired.service_error = ValueError(f"bad credentials for {NAME}")
    wired.pool.close_error = RuntimeError(f"cannot close the pool of {NAME}")
    with caplog.at_level(logging.DEBUG):
        code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert code == 3 and out == "" and wired.pool.close_calls == 1
    assert "ValueError" in caplog.text and "RuntimeError" in caplog.text
    assert NAME not in caplog.text + err
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)


def test_a_pool_that_closes_cleanly_changes_nothing_about_the_exit_code(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    wired.result = counts(would_enqueue_new=1)
    code, out, _ = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert code == 0 and wired.pool.close_calls == 1 and json.loads(out)["requested"] == 1


def test_the_pool_is_closed_even_when_the_run_is_cancelled(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def cancelled(*args: Any, **kwargs: Any) -> dict[str, int]:
        raise asyncio.CancelledError

    monkeypatch.setattr(wired.cli, "reenqueue_drive_files", cancelled)
    with pytest.raises(asyncio.CancelledError):
        wired.cli.main(["--ids-file", tsv(tmp_path, fid(1) + "\n")])
    assert wired.pool.close_calls == 1  # a fault that is not an Exception still closes the pool


# The pool, the Drive service and the library are replaced inside a real process: what the operator would
# see on a close that raises an exception whose message carries a file name.
FAKE_WORLD = f"""
import asyncpg
class _Pool:
    async def close(self):
        raise RuntimeError("cannot close the pool of {NAME}")
async def _create_pool(**kwargs):
    return _Pool()
asyncpg.create_pool = _create_pool
import backend.services.integrations.service_account_drive_service as svc
svc.ServiceAccountDriveService = lambda *a, **k: object()
import backend.services.intake.drive_adapter as da
async def _reenqueue(pool, service, scope, ids, *, apply):
    keys = {sorted(KEYS)!r}
    return {{**dict.fromkeys(keys, 0), "requested": len(ids), "would_enqueue_new": len(ids)}}
da.reenqueue_drive_files = _reenqueue
"""


def test_a_close_that_raises_ends_the_real_process_with_exit_3_no_traceback_and_no_name(
    tmp_path: Path,
) -> None:
    child = _spawn_driven(
        tmp_path,
        FAKE_WORLD,
        "--ids-file",
        tsv(tmp_path, fid(1) + "\n"),
        env_extra={"INTAKE_DRIVE_SCOPE_FOLDER_ID": SCOPE},
    )
    assert child.returncode == 3, child.stderr
    assert "Traceback" not in child.stderr and 'File "' not in child.stderr
    assert NAME not in child.stdout + child.stderr
    assert "RuntimeError" in child.stderr  # said by class, with the place it was raised
    line = json.loads(child.stdout)
    assert (line["requested"], line["would_enqueue_new"], child.stdout.count("\n")) == (1, 1, 1)


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
def _child_env(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(BACKEND_RAG),
        "PYTHONDONTWRITEBYTECODE": "1",  # a test process leaves no __pycache__ behind it
        # Settings() will not load without these two and reads .env from the working directory only: made-up
        # values (conftest's), so that no child depends on a .env file being there. A checkout has none.
        "JWT_SECRET_KEY": "test_jwt_secret_key_for_testing_only_min_32_chars_long",
        "API_KEYS": "test_api_key_1,test_api_key_2",
    }
    env.update(env_extra or {})
    return env


def _spawn_driven(
    tmp_path: Path, patches: str, *argv: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """The real script, run as __main__ in a real process, after `patches` ran in that process."""
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(BACKEND_RAG)!r})\n"
        + textwrap.dedent(patches)
        + f"\nsys.argv = [{str(SCRIPT)!r}] + sys.argv[1:]\n"
        + f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(driver), *argv],
        cwd=BACKEND_RAG,
        env=_child_env(env_extra),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _spawn(*argv: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = _child_env(env_extra)
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
# The log route. Two speakers (this CLI and the Drive adapter) log Drive ids, counts and exception class
# names; one handler keeps every other logger's noise off the terminal. That handler is hygiene: what keeps
# a name out of a log is that it never enters a record (the R1 section below).
# --------------------------------------------------------------------------- #
SAFE_HANDLER = "intake_drive_reenqueue.safe_stderr"
SAFE_LOGGERS = ("intake_drive_reenqueue", "backend.services.intake.drive_adapter")
SERVICE_LOGGER = "backend.services.integrations.service_account_drive_service"
ROOT_ID, INDIVIDUALS_ID, COMPANIES_ID = "rootfolder0001", "indivfolder001", "compfolder0001"


@pytest.fixture(autouse=True)
def _restore_logging() -> Any:
    """main() puts a handler on the root logger and raises two loggers to INFO: take both back."""
    levels = {name: logging.getLogger(name).level for name in SAFE_LOGGERS}
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == SAFE_HANDLER:
            root.removeHandler(handler)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def _record(name: str, level: int, message: str, *args: Any) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, args, None)


def _record_with_exception(name: str, exc: BaseException) -> logging.LogRecord:
    try:
        raise exc
    except BaseException:
        return logging.LogRecord(name, logging.ERROR, __file__, 1, "failed", (), sys.exc_info())


def test_the_handler_lets_this_cli_and_the_drive_adapter_speak(cli: Any, capsys: Any) -> None:
    handler = cli._SafeStderr()
    for name in SAFE_LOGGERS:
        handler.emit(_record(name, logging.INFO, "file_id=%s counted %d", "abc", 3))
    err = capsys.readouterr().err
    assert err.count("\n") == 2 and err.count("file_id=abc counted 3") == 2
    assert all(name in err for name in SAFE_LOGGERS) and "INFO" in err


def test_the_handler_never_prints_a_traceback_not_even_for_a_speaker_it_lets_through(
    cli: Any, capsys: Any
) -> None:
    cli._SafeStderr().emit(
        _record_with_exception(SAFE_LOGGERS[1], RuntimeError(f"cannot read {NAME}"))
    )
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "failed" in err
    assert "Traceback" not in err and "RuntimeError" not in err and NAME not in err


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
def test_the_handler_drops_the_low_records_of_every_other_logger(
    cli: Any, capsys: Any, level: int
) -> None:
    cli._SafeStderr().emit(_record(SERVICE_LOGGER, level, "folder %r is fine", NAME))
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("level", [logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_the_handler_replaces_a_warning_or_worse_of_another_logger_by_a_constant_line(
    cli: Any, capsys: Any, level: int
) -> None:
    cli._SafeStderr().emit(_record(SERVICE_LOGGER, level, "folder %r: %s", NAME, f"error {NAME}"))
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and SERVICE_LOGGER in err and logging.getLevelName(level) in err
    assert "withheld" in err and NAME not in err and "folder" not in err


def test_the_withheld_line_names_the_exception_class_and_never_its_text(
    cli: Any, capsys: Any
) -> None:
    exc = PermissionError(13, f"denied for {NAME}")
    cli._SafeStderr().emit(_record_with_exception(SERVICE_LOGGER, exc))
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "(PermissionError)" in err
    assert NAME not in err and "denied" not in err and "Traceback" not in err


def test_a_logger_or_an_exception_whose_name_is_not_plain_is_not_echoed(
    cli: Any, capsys: Any
) -> None:
    odd = type(f"Error-{NAME}", (Exception,), {})
    cli._SafeStderr().emit(_record_with_exception(f"lib.{NAME}", odd("x")))
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "withheld" in err and NAME not in err and "?" in err


def test_the_handler_never_raises_whatever_the_record_or_the_stream(
    cli: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    handler = cli._SafeStderr()
    handler.emit(_record(SAFE_LOGGERS[0], logging.INFO, "%d", "not a number"))  # getMessage raises
    assert capsys.readouterr() == ("", "")  # and nothing half-written was printed

    attempts: list[str] = []

    class Closed:
        def write(self, text: str) -> int:
            attempts.append(text)
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stderr", Closed())
    handler.emit(_record(SAFE_LOGGERS[0], logging.INFO, "fine"))
    handler.emit(_record(SERVICE_LOGGER, logging.ERROR, "fine"))
    assert len(attempts) == 2  # both lines were tried; the closed stream's error stopped neither


def test_the_installed_handler_is_one_and_main_run_twice_leaves_one(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    wired.stub_library(monkeypatch)
    for _ in range(2):
        run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    named = [h for h in logging.getLogger().handlers if h.get_name() == SAFE_HANDLER]
    assert len(named) == 1


def test_the_two_speakers_reach_info_in_a_process_whose_root_logger_is_quiet(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", logging.WARNING)
    for name in SAFE_LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    wired.stub_library(monkeypatch)
    run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert all(logging.getLogger(name).isEnabledFor(logging.INFO) for name in SAFE_LOGGERS)
    assert not logging.getLogger(SERVICE_LOGGER).isEnabledFor(
        logging.INFO
    )  # nobody else was raised


# ---- main() lets no exception escape ---------------------------------------------------------------------
def test_a_fault_that_nothing_else_catches_is_exit_3_with_a_summary_and_no_message(
    wired: Wired,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unreadable(path: Path) -> Any:
        raise RuntimeError(f"unreadable {NAME}")

    monkeypatch.setattr(wired.cli, "read_ids", unreadable)
    with caplog.at_level(logging.DEBUG):
        code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
    assert code == 3 and out == "" and wired.built == {"pool": 0, "service": 0}
    assert "unexpected fault" in err and "RuntimeError" in err  # said, by class and place
    assert NAME not in err + caplog.text and "Traceback" not in err
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit(7)], ids=["interrupt", "exit"])
def test_main_does_not_swallow_an_interrupt_or_an_exit(
    raised: BaseException, wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def leave(path: Path) -> Any:
        raise raised

    monkeypatch.setattr(wired.cli, "read_ids", leave)
    with pytest.raises(type(raised)):
        wired.cli.main(["--ids-file", tsv(tmp_path, fid(1) + "\n")])


FAULT_WORLD = f"""
import pathlib
_read_text = pathlib.Path.read_text
def _unreadable(self, *args, **kwargs):
    if self.name == "ids.tsv":
        raise RuntimeError("unreadable {NAME}")
    return _read_text(self, *args, **kwargs)
pathlib.Path.read_text = _unreadable
"""


def test_an_unexpected_fault_ends_the_real_process_with_exit_3_no_traceback_and_no_name(
    tmp_path: Path,
) -> None:
    child = _spawn_driven(
        tmp_path,
        FAULT_WORLD,
        "--ids-file",
        tsv(tmp_path, fid(1) + "\n"),
        env_extra={"INTAKE_DRIVE_SCOPE_FOLDER_ID": SCOPE},
    )
    assert child.returncode == 3, child.stderr
    assert "Traceback" not in child.stderr and 'File "' not in child.stderr
    assert NAME not in child.stdout + child.stderr and child.stdout == ""
    assert "unexpected fault" in child.stderr and "RuntimeError" in child.stderr


# ---- R1: no Drive name reaches any handler ---------------------------------------------------------------
# Held where a record is CREATED, never in a handler: code anywhere can attach a handler to any logger, so a
# handler cannot hold it. The Drive client (ServiceAccountDriveService) logs folder ids, counts and exception
# class names only since #9236, and so do this CLI and the Drive adapter. What follows runs the REAL client
# constructor over a fake Drive API whose every folder and file name is the sentinel, with every logger at
# DEBUG, and looks at what a foreign observer is handed at six places.
ADAPTER_LOGGER, CLI_LOGGER = SAFE_LOGGERS[1], SAFE_LOGGERS[0]
DELEGATE = (
    "delegate-6f1c@example.invalid"  # a Workspace address made up for the tests: never logged
)
KEY = "ZQX-key-6f1c"  # stands for the private key of the service account: never logged
FORBIDDEN = (NAME, DELEGATE, KEY)  # what no record of a run may hold, whatever the run does


def _account() -> str:
    """The service account the fake settings hold: its key is a canary."""
    return json.dumps(
        {"type": "service_account", "client_email": "synthetic@example.invalid", "private_key": KEY}
    )


def _sentinel_http_error(status: int, reason: str, where: str) -> HttpError:
    """A Drive error whose uri AND body repeat the sentinel: the worst a failed call can hand a log line."""
    message = f"File not found: {NAME}"
    body = {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"domain": "global", "reason": reason, "message": message}],
        }
    }
    uri = f"https://www.googleapis.com/drive/v3/files/{where}?fields=name&q={NAME}"
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode(), uri=uri)


def _undecodable() -> str:
    """Base64 whose bytes are not UTF-8: the error of decoding them holds them, sentinel included, in .object."""
    return base64.b64encode(b"\xff\xfe" + NAME.encode()).decode()


def _not_json() -> str:
    """Base64 of a text that is not JSON: the error of parsing it holds the text, sentinel included, in .doc."""
    return base64.b64encode(f"not json {NAME}".encode()).decode()


SHAPES = {  # the credentials the client is given, by shape
    "creds-base64": base64.b64encode(_account().encode()).decode(),  # parses at the second try
    "creds-undecodable": _undecodable(),
    "creds-not-json": _not_json(),
}
# the client refuses its credentials: no API client is built and the run has no counts to print
REFUSED = {"creds-undecodable", "creds-not-json"}


def _decode_errors(payload: str) -> list[str]:
    """The text of the error that each of the client's two parses of ``payload`` raises (raw JSON, then
    base64): what its log lines must never say. The stdlib's errors do not carry the payload in their text,
    so the sentinel in the payload is for the routes that do: repr, exc_info and the payload itself."""
    texts = []
    for parse in (
        lambda: json.loads(payload),
        lambda: json.loads(base64.b64decode(payload).decode("utf-8")),
    ):
        try:
            parse()
        except Exception as exc:
            texts.append(str(exc))
    return texts


class RealCtor:
    """The CLI around the REAL ServiceAccountDriveService constructor, a fake Drive API and a fake pool.

    Nothing real can be reached: the settings the constructor reads, its credentials factory and the
    discovery build are replaced. Every folder it checks at start-up and every file the run reads carries the
    sentinel in its name. ``companies`` is what the check of the last folder does: "ok" finds it, "runtime"
    raises a RuntimeError whose text holds the sentinel, "http" an HttpError whose uri and body hold it,
    "trashed" finds it in the bin. ``delegated`` makes the client impersonate that (made-up) Workspace
    address. ``credentials`` replaces the service account it reads by one of SHAPES. ``forbidden`` is every
    string that no record of a run may hold: the sentinel, the address, the key, and for ``credentials``
    the payload and the text of each error that parsing it raises.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cli: Any,
        *,
        companies: str = "runtime",
        delegated: str | None = None,
        credentials: str | None = None,
    ) -> None:
        import backend.services.integrations.service_account_drive_service as svc

        self.cli = cli
        self.svc = svc
        self.pool = FakePool()
        self.drive = FakeDrive()
        self.discovery: list[tuple[str, str]] = []
        real = self

        async def create_pool(**kwargs: Any) -> FakePool:
            return real.pool

        def build(api: str, version: str, credentials: Any = None) -> Any:
            real.discovery.append((api, version))
            return real.drive.service

        account = _account()
        self.forbidden: tuple[str, ...] = FORBIDDEN
        if credentials is not None:
            errors = _decode_errors(credentials)
            assert all(errors)  # an error without a text would be found in every record
            account = credentials
            self.forbidden += (credentials, *errors)
        settings = types.SimpleNamespace(
            google_credentials_json=account,
            google_drive_root_folder_id=ROOT_ID,
            gdrive_individuals_folder_id=INDIVIDUALS_ID,
            gdrive_companies_folder_id=COMPANIES_ID,
        )
        creds = types.SimpleNamespace(with_subject=lambda subject: object())
        accounts = types.SimpleNamespace(
            Credentials=types.SimpleNamespace(
                from_service_account_info=lambda info, scopes=None: creds
            )
        )
        monkeypatch.setattr(cli.asyncpg, "create_pool", create_pool)
        monkeypatch.setattr(svc, "settings", settings)
        monkeypatch.setattr(svc, "service_account", accounts)
        monkeypatch.setattr(svc, "build", build)
        if delegated is not None:
            client = functools.partial(svc.ServiceAccountDriveService, delegated_user=delegated)
            monkeypatch.setattr(svc, "ServiceAccountDriveService", client)
        self.drive.add(ROOT_ID, f"{NAME}-root-folder", [])
        self.drive.add(INDIVIDUALS_ID, f"{NAME}-individuals", [])
        if companies == "runtime":
            self.drive.errors[COMPANIES_ID] = RuntimeError(f"folder {NAME} is unreachable")
        elif companies == "http":
            self.drive.errors[COMPANIES_ID] = _sentinel_http_error(404, "notFound", COMPANIES_ID)
        else:
            self.drive.add(COMPANIES_ID, f"{NAME}-companies", [])
            self.drive.items[COMPANIES_ID]["trashed"] = companies == "trashed"
        self.drive.add("DIR", f"{NAME}-dir", [SCOPE], "application/vnd.google-apps.folder")
        self.drive.add(fid(1), f"{NAME}-1.pdf", ["DIR"])

    def ingest_faults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Eight ids that end in eight different ways, every fault holding the sentinel in its text.

        1 is fine; 2 fails at files.get; 3 is gone; 4 and 5 fail to download; 6 fails to enqueue; 7 has an
        ancestor whose lookup fails; 8 has an ancestor that is gone. A dry run cannot ask the queue.
        """
        named = f"cannot read {NAME}"
        _patch_apply_path(monkeypatch, tmp_path)

        async def download(drive_service: Any, file_id: str, dest: Path) -> int:
            if file_id == fid(4):
                raise _drive_error(500, "backendError", message=named)
            if file_id == fid(5):
                raise PermissionError(13, "Permission denied", f"/blobs/{NAME}.pdf")
            return 7

        async def enqueue(pool: Any, **kwargs: Any) -> EnqueueResult:
            if kwargs["source_ref"] == f"drive:{fid(6)}":
                raise RuntimeError(named)
            return EnqueueResult(instance_id=1, queue_id=1, was_new=True)

        monkeypatch.setattr(da, "_download_file", download)
        monkeypatch.setattr(da, "enqueue", enqueue)
        for i in range(2, 9):
            self.drive.add(fid(i), f"{NAME}-{i}.pdf", ["DIR"])
        self.drive.errors[fid(2)] = _sentinel_http_error(500, "backendError", fid(2))
        self.drive.errors[fid(3)] = _sentinel_http_error(404, "notFound", fid(3))
        self.drive.items[fid(7)]["parents"] = ["LOST"]
        self.drive.errors["LOST"] = RuntimeError(named)
        self.drive.items[fid(8)]["parents"] = ["GONE"]
        self.drive.errors["GONE"] = _sentinel_http_error(404, "notFound", "GONE")
        self.pool.lookup_error = RuntimeError(named)


CASES = {
    "dry-run": [0],
    "apply": [0],
    "folder-errors": [0],
    "ingest-errors": [3, 3],
    "creds-undecodable": [3],
    "creds-not-json": [3],
}  # case: exit codes
PLACES = [
    "terminal-only",  # the CLI's own handler and nothing else
    "root-buffer",  # a StreamHandler on the root, into a buffer
    "root-file",  # a FileHandler on the root
    "service-stderr",  # a StreamHandler(stderr) on the Drive client's own logger
    "adapter-handler",  # a handler on the drive adapter's logger
    "last-resort",  # no handler at all: logging.lastResort, which writes to stderr
]
ADAPTER_FAULTS = [
    "queue lookup failed",
    "files.get failed",
    "is gone or unreadable",
    "ancestor walk failed",
    "no longer exists",
    "enqueue failed",
]


def _live(place: str, case: str) -> list[str] | None:
    """What a handler at ``place`` must have been handed in a run of ``case`` (proof that it was
    listening), or None when that run gives it nothing to hear."""
    faults = ADAPTER_FAULTS if case == "ingest-errors" else []
    if case in REFUSED:  # both parses fail, the client is refused and the CLI says so
        parsed = ["SA creds not raw JSON", "SA creds base64 decode failed"]
        refused = ["Drive service"]
        return {
            "terminal-only": refused,  # the CLI's own line: the client's debug lines are not printed
            "last-resort": refused,
            "service-stderr": parsed,
            "root-buffer": [*parsed, *refused],
            "root-file": [*parsed, *refused],
        }.get(place)
    if place == "last-resort":  # WARNING and above, from every logger
        return [ROOT_ID, COMPANIES_ID] if case == "folder-errors" else faults or None
    if place == "terminal-only":  # the CLI's own lines; the Drive client's, as a constant line
        return ["reenqueue_drive done", *(["withheld"] if case == "folder-errors" else []), *faults]
    if place == "adapter-handler":
        return ["reenqueue_drive done", *faults]
    service = [ROOT_ID, COMPANIES_ID] if case == "folder-errors" else [ROOT_ID]
    return service if place == "service-stderr" else [*service, "reenqueue_drive done", *faults]


PAIRS = [(place, case) for place in PLACES for case in CASES if _live(place, case) is not None]


class _Tap(logging.Handler):
    """Keeps, for each record it is handed, every field a formatter, a serializer or a log shipper could print."""

    def __init__(self, words: tuple[str, ...] = FORBIDDEN) -> None:
        super().__init__(logging.NOTSET)
        self.words = words
        self.fields: list[str] = []
        self.exc_texts: list[str] = []
        self.records = 0

    def emit(self, record: logging.LogRecord) -> None:
        shown = self.format(record)  # the message and, when the record has one, its traceback
        self.records += 1
        self.exc_texts.append(str(record.exc_text))
        self.fields += [
            str(record.msg),
            repr(record.args),
            record.getMessage(),
            shown,
            repr(record.exc_info),
            str(record.exc_text),
            str(record.stack_info),
        ]

    def leaks(self) -> int:
        return sum(field.count(word) for field in self.fields for word in self.words)


class _Placed:
    """One place where a foreign observer of the log sits, and what it writes."""

    def __init__(self, place: str, tmp_path: Path, words: tuple[str, ...]) -> None:
        self.place = place
        self.words = words
        self.buffer = io.StringIO()
        self.path = tmp_path / "foreign.log"
        self.handlers: list[logging.Handler] = []
        self.taps: list[_Tap] = []

    def watch(self, logger: logging.Logger, handler: logging.Handler) -> None:
        # at the same place, the field-level view of what the handler is handed
        tap = _Tap(self.words)
        logger.addHandler(handler)
        logger.addHandler(tap)
        self.handlers.append(handler)
        self.taps.append(tap)

    def written(self, stderr: str) -> str:
        """The text the observer wrote: stderr for the terminal, the stderr handler and the last resort."""
        if self.place == "root-file":
            return self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        return (
            self.buffer.getvalue() if self.place in ("root-buffer", "adapter-handler") else stderr
        )


@contextlib.contextmanager
def _placed(
    place: str,
    cli: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    words: tuple[str, ...] = FORBIDDEN,
) -> Iterator[_Placed]:
    """Every logger at DEBUG and a foreign observer at ``place``; the loggers are put back on the way out."""
    root = logging.getLogger()
    service, adapter = logging.getLogger(SERVICE_LOGGER), logging.getLogger(ADAPTER_LOGGER)
    loggers = [root, service, adapter, logging.getLogger(CLI_LOGGER)]
    saved = [(each, list(each.handlers), each.level) for each in loggers]
    placed = _Placed(place, tmp_path, words)
    try:
        for each in loggers:
            each.setLevel(logging.DEBUG)
        if place == "root-buffer":
            placed.watch(root, logging.StreamHandler(placed.buffer))
        elif place == "root-file":
            placed.watch(root, logging.FileHandler(placed.path, encoding="utf-8"))
        elif place == "service-stderr":
            placed.watch(service, logging.StreamHandler(sys.stderr))
        elif place == "adapter-handler":
            placed.watch(adapter, logging.StreamHandler(placed.buffer))
        elif place == "last-resort":
            root.handlers[
                :
            ] = []  # pytest's own included: nobody listens, so the last resort speaks
            monkeypatch.setattr(cli, "_install_safe_logging", lambda: None)
        yield placed
    finally:
        for each, handlers, level in saved:
            each.handlers[:] = handlers
            each.setLevel(level)
        for handler in placed.handlers:
            handler.close()


def _runs_of(
    case: str, real: RealCtor, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[list[str]]:
    """The CLI runs of one case, set up on the fakes of ``real``, as argv lists."""
    one = tmp_path / "one.tsv"
    one.write_text(fid(1) + "\n", encoding="utf-8")
    if case == "apply":
        _patch_apply_path(monkeypatch, tmp_path)
        return [["--ids-file", str(one), "--apply"]]
    if case == "ingest-errors":
        real.ingest_faults(monkeypatch, tmp_path)
        every = tmp_path / "every.tsv"
        every.write_text("".join(f"{fid(i)}\n" for i in range(1, 9)), encoding="utf-8")
        return [["--ids-file", str(one)], ["--ids-file", str(every), "--apply"]]
    if case == "folder-errors":
        real.drive.items[ROOT_ID]["trashed"] = True
        real.drive.errors[INDIVIDUALS_ID] = RuntimeError(f"folder {NAME} is unreachable")
    return [["--ids-file", str(one)]]


@pytest.mark.parametrize(("place", "case"), PAIRS, ids=[f"{p}-{c}" for p, c in PAIRS])
def test_no_name_reaches_a_handler_wherever_it_sits_and_whatever_the_run_does(
    place: str,
    case: str,
    cli: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
) -> None:
    monkeypatch.setenv("INTAKE_DRIVE_SCOPE_FOLDER_ID", SCOPE)
    monkeypatch.delenv("INTAKE_DRIVE_MIN_FREE_GIB", raising=False)
    companies = "http" if case == "folder-errors" else "ok"
    real = RealCtor(
        monkeypatch, cli, companies=companies, delegated=DELEGATE, credentials=SHAPES.get(case)
    )
    runs = _runs_of(case, real, monkeypatch, tmp_path)
    codes: list[int] = []
    outs: list[str] = []
    err = ""
    with _placed(place, cli, monkeypatch, tmp_path, real.forbidden) as placed:
        for argv in runs:
            code, out, run_err = run(cli, capsys, *argv)
            codes.append(code)
            outs.append(out)
            err += run_err
        for handler in placed.handlers:
            handler.flush()
        heard = placed.written(err)
    refused = case in REFUSED
    assert codes == CASES[case]  # the run did what the case says it does
    # the real constructor ran once per run, and built its API client unless it refused the credentials
    assert real.discovery == ([] if refused else [("drive", "v3")] * len(runs))
    assert all(marker in heard for marker in _live(place, case) or []), heard  # it was listening
    assert all(tap.records > 0 for tap in placed.taps)
    # stdout is one JSON line per run, and none for a run that stopped before it had counts
    assert all(out.count("\n") == (0 if refused else 1) for out in outs)
    deliveries = sum(text.count(word) for text in (*outs, err, heard) for word in real.forbidden)
    assert deliveries + sum(tap.leaks() for tap in placed.taps) == 0
    assert not any(
        word in text for tap in placed.taps for text in tap.exc_texts for word in real.forbidden
    )


@pytest.mark.parametrize("place", ["terminal-only", "named-stderr", "root-buffer", "root-file"])
def test_the_gates_round_2_probe_finds_no_sentinel_at_any_output_handler(
    place: str, cli: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The round-2 gate's own independent test, kept as it was written (its four placements, its
    capture, its fake): the real constructor over fake Drive metadata, the root and the service at DEBUG."""
    monkeypatch.setenv("INTAKE_DRIVE_SCOPE_FOLDER_ID", SCOPE)
    real = RealCtor(monkeypatch, cli, delegated=DELEGATE)
    ids = tmp_path / "ids.tsv"
    ids.write_text(fid(1) + "\n", encoding="utf-8")
    root, service = logging.getLogger(), logging.getLogger(SERVICE_LOGGER)
    roots, services = list(root.handlers), list(service.handlers)
    levels = (root.level, service.level)
    out, err, buffer = io.StringIO(), io.StringIO(), io.StringIO()
    target = tmp_path / "captured.log"
    foreign: logging.Handler | None = None
    try:
        root.setLevel(logging.DEBUG)
        service.setLevel(logging.DEBUG)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if place == "named-stderr":
                foreign = logging.StreamHandler(sys.stderr)
                service.addHandler(foreign)
            elif place == "root-buffer":
                foreign = logging.StreamHandler(buffer)
                root.addHandler(foreign)
            elif place == "root-file":
                foreign = logging.FileHandler(target)
                root.addHandler(foreign)
            code = cli.main(["--ids-file", str(ids)])
        if foreign is not None:
            foreign.flush()
        captured = out.getvalue() + err.getvalue() + buffer.getvalue()
        if target.exists():
            captured += target.read_text(encoding="utf-8")
    finally:
        root.handlers[:] = roots
        service.handlers[:] = services
        root.setLevel(levels[0])
        service.setLevel(levels[1])
        if foreign is not None:
            foreign.close()
    assert code == 0 and real.discovery == [("drive", "v3")]
    assert captured.count(NAME) + captured.count(DELEGATE) == 0


@pytest.mark.parametrize("delegated", [DELEGATE, None], ids=["made-up-address", "default-address"])
@pytest.mark.parametrize("companies", ["ok", "runtime", "http", "trashed"])
def test_the_real_client_constructor_creates_no_record_that_holds_a_name_or_an_address(
    companies: str,
    delegated: str | None,
    cli: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source of R1: no handler is involved, the records are looked at where they are created."""
    real = RealCtor(monkeypatch, cli, companies=companies, delegated=delegated)
    tap, service = _Tap(), logging.getLogger(SERVICE_LOGGER)
    level = service.level
    service.setLevel(logging.DEBUG)
    service.addHandler(tap)
    try:
        real.svc.ServiceAccountDriveService()
    finally:
        service.removeHandler(tap)
        service.setLevel(level)
    said = "\n".join(tap.fields)
    assert tap.records >= 6  # the credentials, the address and the four folders were all said
    assert ROOT_ID in said and COMPANIES_ID in said  # ids are what it says
    assert tap.leaks() == 0
    assert delegated is not None or "@" not in said  # the address it falls back to is no more said


@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_credentials_parser_says_a_class_name_and_never_the_text_of_its_errors(
    shape: str, cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 at the credentials, where the records are created: a payload the raw parse cannot read is tried as
    base64; what is said of each failure is neither the failure's text, nor the payload, nor anything the
    error holds. Two payloads fail both parses and the client refuses to start; one is readable."""
    real = RealCtor(monkeypatch, cli, credentials=SHAPES[shape])
    refused = shape in REFUSED
    assert len(_decode_errors(SHAPES[shape])) == (2 if refused else 1)  # the shape is what it says
    tap, service = _Tap(real.forbidden), logging.getLogger(SERVICE_LOGGER)
    level = service.level
    service.setLevel(logging.DEBUG)
    service.addHandler(tap)
    try:
        if refused:
            with pytest.raises(ValueError, match="Invalid Service Account credentials"):
                real.svc.ServiceAccountDriveService()
        else:
            real.svc.ServiceAccountDriveService()
    finally:
        service.removeHandler(tap)
        service.setLevel(level)
    said = "\n".join(tap.fields)
    assert "SA creds not raw JSON" in said  # the raw parse failed in all three
    assert ("SA creds base64 decode failed" in said) is refused
    assert ("Loaded Google credentials from base64-encoded JSON" in said) is not refused
    assert tap.leaks() == 0
    assert real.discovery == ([] if refused else [("drive", "v3")])


def test_a_third_party_warning_reaches_the_terminal_as_a_constant_line_and_a_foreign_root_handler_goes(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """Terminal hygiene, proved with a synthetic emitter and not with the Drive client: a record of a
    logger the CLI does not speak for is printed whole by a handler somebody left on the root, and as one
    constant line once the CLI has taken that handler off."""
    noisy, root = logging.getLogger("thirdparty.synthetic"), logging.getLogger()
    foreign = logging.StreamHandler(sys.stderr)
    root.addHandler(foreign)

    async def speak(pool: Any, drive_service: Any, scope_id: str, ids: list[str], *, apply: bool):
        noisy.warning("noise about %s", NAME)
        return {**counts(), "requested": len(ids)}

    try:
        noisy.warning("noise about %s", NAME)
        left_alone = capsys.readouterr().err
        monkeypatch.setattr(wired.cli, "reenqueue_drive_files", speak)
        code, out, err = run(wired.cli, capsys, "--ids-file", tsv(tmp_path, fid(1) + "\n"))
        taken_off = foreign not in root.handlers
    finally:
        root.removeHandler(foreign)
    assert NAME in left_alone  # the emitter is guilty: nobody stopped it
    assert code == 0 and taken_off
    assert NAME not in out + err and "withheld" in err and "thirdparty.synthetic" in err


def _drive_calls(source: str, functions: set[str] | None) -> list[tuple[str, str, tuple[str, ...]]]:
    """(function, method, keywords) of every ``<anything>.files().<method>(...)`` call in ``functions``
    (all functions when None); a call inside a nested function belongs to the outermost one."""
    found: list[tuple[str, str, tuple[str, ...]]] = []

    def visit(node: ast.AST, outer: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            name = outer
            if name is None and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if (
                name is not None
                and (functions is None or name in functions)
                and isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Call)
                and getattr(child.func.value.func, "attr", "") == "files"
            ):
                found.append((name, child.func.attr, tuple(k.arg or "**" for k in child.keywords)))
            visit(child, name)

    visit(ast.parse(source), None)
    return found


def _by_id(calls: list[tuple[str, str, tuple[str, ...]]]) -> bool:
    """True when every call reads one file named by its id: get or get_media, with fileId and no query."""
    return bool(calls) and all(
        method in ("get", "get_media") and "fileId" in keywords and "q" not in keywords
        for _, method, keywords in calls
    )


def test_every_drive_call_on_the_reenqueue_path_addresses_a_file_by_id() -> None:
    """The assumption behind the third-party DEBUG lines ("URL being requested" in googleapiclient): a URL
    that holds an id and no query cannot hold a name."""
    import googleapiclient.model as model

    adapter = (BACKEND_RAG / "backend/services/intake/drive_adapter.py").read_text(encoding="utf-8")
    client = BACKEND_RAG / "backend/services/integrations/service_account_drive_service.py"
    in_adapter = _drive_calls(adapter, None)
    at_start_up = _drive_calls(client.read_text(encoding="utf-8"), {"_validate_configured_folders"})
    assert sorted((fn, method) for fn, method, _ in in_adapter) == [
        ("_download_file", "get_media"),
        ("_file_meta", "get"),
        ("_folder_meta", "get"),
    ]
    assert [(fn, method) for fn, method, _ in at_start_up] == [
        ("_validate_configured_folders", "get")
    ]
    assert _by_id(in_adapter) and _by_id(at_start_up)
    assert (
        model.dump_request_response is False
    )  # googleapiclient would log whole requests and responses


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(d):\n    return d.files().list(q=\"name = 'x'\").execute()\n",
        "def f(d):\n    return d.files().get(fileId=1, q='x').execute()\n",
        "def f(d):\n    return d.files().get(supportsAllDrives=True).execute()\n",
        "def f(d):\n    return d.files().update(fileId=1).execute()\n",
        "def f(d):\n    return 1\n",
    ],
)
def test_the_by_id_checker_refuses_a_query_a_missing_id_and_a_call_that_is_not_a_read(
    snippet: str,
) -> None:
    assert not _by_id(_drive_calls(snippet, None))


def test_the_by_id_checker_lets_a_read_by_id_through() -> None:
    ok = "def f(d):\n    return d.files().get(fileId=1, fields='id').execute()\n"
    assert _by_id(_drive_calls(ok, None))


def _real_ctor_driver(*, apply: bool, verbose_root: bool, control: Path) -> str:
    items = {
        ROOT_ID: {"id": ROOT_ID, "name": f"{NAME}-root-folder"},
        INDIVIDUALS_ID: {"id": INDIVIDUALS_ID, "name": f"{NAME}-individuals"},
        "DIR": {
            "id": "DIR",
            "name": f"{NAME}-dir",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [SCOPE],
        },
        fid(1): {
            "id": fid(1),
            "name": f"{NAME}-1.pdf",
            "mimeType": "application/pdf",
            "parents": ["DIR"],
        },
    }
    errors = {COMPANIES_ID: f"folder {NAME} is unreachable"}
    applying = (
        """
async def _download(drive_service, file_id, dest):
    return 7
async def _enqueue(pool, **kwargs):
    return types.SimpleNamespace(was_new=True)
async def _no_pause(seconds):
    return None
da._download_file = _download
da.enqueue = _enqueue
da._pause = _no_pause
da._free_bytes = lambda: 10**15
"""
        if apply
        else ""
    )
    return f"""
import asyncpg, atexit, functools, json, logging, types

class _Conn:
    async def fetchval(self, query, *args):
        return False
class _Ctx:
    async def __aenter__(self):
        return _Conn()
    async def __aexit__(self, *exc):
        return False
class _Pool:
    def acquire(self):
        return _Ctx()
    async def close(self):
        return None
async def _create_pool(**kwargs):
    return _Pool()
asyncpg.create_pool = _create_pool

ITEMS = {items!r}
ERRORS = {errors!r}
class _Request:
    def __init__(self, run):
        self._run = run
    def execute(self):
        return self._run()
class _Files:
    def get(self, *, fileId, fields, supportsAllDrives=False):
        def run():
            if fileId in ERRORS:
                raise RuntimeError(ERRORS[fileId])
            if fileId not in ITEMS:
                raise RuntimeError("not found")
            return dict(ITEMS[fileId])
        return _Request(run)
class _Api:
    def files(self):
        return _Files()

import backend.services.integrations.service_account_drive_service as svc
svc.settings = types.SimpleNamespace(
    google_credentials_json='{{"type": "service_account", "client_email": "synthetic@example.invalid"}}',
    google_drive_root_folder_id={ROOT_ID!r},
    gdrive_individuals_folder_id={INDIVIDUALS_ID!r},
    gdrive_companies_folder_id={COMPANIES_ID!r},
)
svc.service_account = types.SimpleNamespace(
    Credentials=types.SimpleNamespace(
        from_service_account_info=lambda info, scopes=None: types.SimpleNamespace(
            with_subject=lambda subject: object()
        )
    )
)
svc.build = lambda api, version, credentials=None: _Api()
svc.ServiceAccountDriveService = functools.partial(
    svc.ServiceAccountDriveService, delegated_user={DELEGATE!r}
)
import backend.services.intake.drive_adapter as da
{applying}
class _Tap(logging.Handler):
    def __init__(self):
        super().__init__(logging.NOTSET)
        self.seen = 0
        self.leaks = 0
    def emit(self, record):
        try:
            fields = [str(record.msg), repr(record.args), record.getMessage(), self.format(record),
                      repr(record.exc_info), str(record.exc_text)]
            self.seen += 1
            self.leaks += sum(f.count(word) for f in fields for word in ({NAME!r}, {DELEGATE!r}))
        except Exception:
            self.leaks += 1  # a record the tap cannot read is a record it cannot vouch for
TAPS = {{"service": _Tap(), "adapter": _Tap(), "root": _Tap()}}
logging.getLogger({SERVICE_LOGGER!r}).addHandler(TAPS["service"])
logging.getLogger({ADAPTER_LOGGER!r}).addHandler(TAPS["adapter"])
logging.getLogger().addHandler(TAPS["root"])
if {verbose_root!r}:
    logging.getLogger().setLevel(logging.DEBUG)
atexit.register(
    lambda: open({str(control)!r}, "w").write(json.dumps({{k: [t.seen, t.leaks] for k, t in TAPS.items()}}))
)
"""


@pytest.mark.parametrize("verbose_root", [False, True], ids=["quiet-root", "verbose-root"])
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_the_real_constructor_in_a_real_process_leaks_no_name_to_any_handler_or_the_terminal(
    apply: bool, verbose_root: bool, tmp_path: Path
) -> None:
    control = tmp_path / "control.json"
    child = _spawn_driven(
        tmp_path,
        _real_ctor_driver(apply=apply, verbose_root=verbose_root, control=control),
        "--ids-file",
        tsv(tmp_path, fid(1) + "\n"),
        *(["--apply"] if apply else []),
        env_extra={"INTAKE_DRIVE_SCOPE_FOLDER_ID": SCOPE},
    )
    assert child.returncode == 0, child.stderr
    heard = json.loads(control.read_text())  # [records, deliveries of the sentinel] per observer
    assert (
        heard["service"][0] >= 1 and heard["adapter"][0] >= 1
    )  # they were listening, in that process
    assert [deliveries for _, deliveries in heard.values()] == [0, 0, 0]
    assert NAME not in child.stdout + child.stderr and DELEGATE not in child.stdout + child.stderr
    assert "unreachable" not in child.stderr
    assert "withheld" in child.stderr and SERVICE_LOGGER in child.stderr
    assert (
        "reenqueue_drive done" in child.stderr
    )  # the INFO line of a speaker, even on a quiet root
    assert json.loads(child.stdout)["requested"] == 1 and child.stdout.count("\n") == 1


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
