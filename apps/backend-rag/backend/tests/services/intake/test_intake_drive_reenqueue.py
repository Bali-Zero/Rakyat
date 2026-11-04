"""Re-enqueue by Drive file id: files the changes feed never delivered are read again, by id.

Before the cursor discipline (test_intake_drive_drain_cursor.py) the drain moved its cursor past changes
it had not handled, and nothing emits those changes again. ``reenqueue_drive_files`` reads each given file
id with files.get, sends it through the drain's own skips and scope walk (``_ingest_change``) and then only
counts it (dry run) or downloads and enqueues it (apply).

GUILT: a dry run that downloads or enqueues anything, or that cannot tell a file the queue has from one it
lacks; an apply that does not enqueue the file once, with the drain's source_ref and source_path, or that
adds a row the second time; a trashed, native or out-of-scope file, a file Drive no longer serves
(``missing``) and a Drive or DB fault (``errors``) that is not counted or that writes anything; an apply
below the disk floor that downloads; an id that is not a Drive file id, an empty scope or an ``apply`` that
is not a bool, which must stop the run before any Drive call; a queue lookup that answers anything but True
or False and is read as a verdict; a name or a path in a log line.
INNOCENCE: a dry run is not stopped by a full disk (it downloads nothing); a file under nested folders keeps
its relative path and a direct child of the scope is its bare name; every requested id lands in exactly one
counter; the cursor is never touched.
The queue's SQL and the real ``enqueue`` run against the local ``nuzantara_test`` (skipped when it is
unreachable); everything else is a pure unit with a fake Drive. Synthetic ids only; every file and folder
name is a sentinel that may reach ``source_path`` (the queue row) and nothing else.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import logging
import os
import types
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httplib2
import pytest
import pytest_asyncio
from googleapiclient.errors import HttpError

from backend.services.intake import drive_adapter as da
from backend.services.intake.enqueue import EnqueueResult, compute_blob_hash, enqueue

SCOPE = "SCOPE"
NAME = "ZQX-sentinel-6f1c"
PDF = "application/pdf"
NATIVE = "application/vnd.google-apps.document"
GIB = 2**30
DSN = os.environ.get("INTAKE_TEST_DSN", "postgresql://localhost:5432/nuzantara_test")

ZERO = {
    "requested": 0,
    "would_enqueue_new": 0,
    "enqueued_new": 0,
    "already_present": 0,
    "skipped_native": 0,
    "skipped_removed": 0,
    "skipped_out_of_scope": 0,
    "missing": 0,
    "errors": 0,
    "not_attempted": 0,
    "disk_hold": 0,
}
BUCKETS = (
    "would_enqueue_new",
    "enqueued_new",
    "already_present",
    "skipped_native",
    "skipped_removed",
    "skipped_out_of_scope",
    "missing",
    "errors",
    "not_attempted",
)


def expect(**counts: int) -> dict[str, int]:
    return {**ZERO, **counts}


def fid(i: int) -> str:
    return f"file{i:07d}"  # 11 characters: a valid Drive id shape, never a real one


def drive_error(status: int, *reasons: str, message: str = "synthetic") -> HttpError:
    """A real googleapiclient HttpError with Drive's error body: one `errors` entry per reason."""
    error = {
        "code": status,
        "message": message,
        "errors": [{"domain": "global", "reason": reason} for reason in reasons],
    }
    return HttpError(httplib2.Response({"status": status}), json.dumps({"error": error}).encode())


class FakeRequest:
    def __init__(self, run: Any) -> None:
        self._run = run

    def execute(self) -> dict[str, Any]:
        return self._run()


class FakeFiles:
    def __init__(self, drive: FakeDrive) -> None:
        self._drive = drive

    def get(self, *, fileId: str, fields: str, supportsAllDrives: bool = False) -> FakeRequest:
        drive = self._drive
        drive.gets.append((fileId, fields, supportsAllDrives))

        def run() -> dict[str, Any]:
            if fileId in drive.errors:
                raise drive.errors[fileId].with_traceback(None)
            item = drive.items.get(fileId)
            if item is None:
                raise drive_error(404, "notFound")
            wanted = {part.strip() for part in fields.split(",")}
            return {key: value for key, value in item.items() if key in wanted}

        return FakeRequest(run)


class FakeDrive:
    """files.get over a dict of items; it answers ONLY the fields asked for, as Drive does."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, Exception] = {}  # item id -> what files.get raises for it
        self.gets: list[tuple[str, str, bool]] = []  # (id, fields, supportsAllDrives)
        self.service = types.SimpleNamespace(files=lambda: FakeFiles(self))

    def folder(self, folder_id: str, name: str, parents: list[str]) -> None:
        self.items[folder_id] = {
            "id": folder_id,
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": parents,
        }

    def file(
        self, file_id: str, name: str, parents: list[str], *, mime: str = PDF, trashed: bool = False
    ) -> None:
        self.items[file_id] = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parents": parents,
            "trashed": trashed,
        }

    def gets_of(self, item_id: str) -> list[tuple[str, str, bool]]:
        return [call for call in self.gets if call[0] == item_id]


_UNSET = object()


class Book:
    """intake_queue as the code sees it: the pool answers the EXISTS lookup, the patched enqueue inserts."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}  # source_ref -> the kwargs enqueue got
        self.enqueued: list[str] = []  # every enqueue call, by source_ref
        self.fail: dict[str, Exception] = {}  # source_ref -> what enqueue raises
        self.lookups: list[tuple[str, tuple[Any, ...]]] = []
        self.executed: list[str] = []  # every write the pool was asked for
        self.reply: Any = _UNSET  # fault injection: what the lookup answers
        self.lookup_error: Exception | None = None

    def acquire(self) -> Any:
        book = self

        class _Conn:
            async def fetchval(self, query: str, *args: Any) -> Any:
                book.lookups.append((query, args))
                if book.lookup_error is not None:
                    raise book.lookup_error.with_traceback(None)
                return book.reply if book.reply is not _UNSET else args[0] in book.rows

            async def execute(self, query: str, *args: Any) -> str:
                book.executed.append(query)
                return "INSERT 0 1"

        class _Ctx:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()

    async def enqueue(self, pool: Any, **kwargs: Any) -> EnqueueResult:
        ref = kwargs["source_ref"]
        self.enqueued.append(ref)
        if ref in self.fail:
            raise self.fail[ref].with_traceback(None)
        was_new = ref not in self.rows
        if was_new:
            self.rows[ref] = kwargs
        return EnqueueResult(instance_id=1, queue_id=len(self.rows), was_new=was_new)


class World:
    """A fake Drive, a fake queue and recorders for what a dry run must never do."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, fake_queue: bool = True
    ) -> None:
        self.drive = FakeDrive()
        self.book = Book()
        self.downloads: list[str] = []
        self.download_errors: dict[str, Exception] = {}
        self.content: bytes | None = None  # real bytes for the DB tests, else a size of 7
        self.pauses: list[float] = []
        monkeypatch.setattr(da, "_BLOB_ROOT", tmp_path / "blobs")  # nothing lands in the home dir
        monkeypatch.setattr(da, "_download_file", self._download)
        monkeypatch.setattr(da, "_pause", self._pause)
        monkeypatch.setattr(da, "_free_bytes", lambda: 10**15)
        if fake_queue:
            monkeypatch.setattr(da, "enqueue", self.book.enqueue)

    async def _download(self, drive_service: Any, file_id: str, dest: Path) -> int:
        self.downloads.append(file_id)
        if file_id in self.download_errors:
            raise self.download_errors[file_id].with_traceback(None)
        if self.content is None:
            return 7
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.content)
        return len(self.content)

    async def _pause(self, seconds: float) -> None:
        self.pauses.append(seconds)

    async def run(self, ids: list[Any], *, apply: Any, **kwargs: Any) -> dict[str, int]:
        return await da.reenqueue_drive_files(
            self.book, self.drive, SCOPE, ids, apply=apply, **kwargs
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    monkeypatch.delenv("INTAKE_DRIVE_MIN_FREE_GIB", raising=False)
    return World(monkeypatch, tmp_path)


def seed(world: World, i: int = 1, *, parents: list[str] | None = None, **extra: Any) -> str:
    """File i, named with the sentinel, in a folder named with the sentinel under the scope root."""
    world.drive.folder("DIR", f"{NAME}-dir", [SCOPE])
    world.drive.folder("OUTSIDE", f"{NAME}-out", ["ROOT"])
    world.drive.folder("ROOT", "root", [])
    file_id = fid(i)
    world.drive.file(file_id, f"{NAME}-{i}.pdf", parents or ["DIR"], **extra)
    return file_id


def free_space(monkeypatch: pytest.MonkeyPatch, *values: int | Exception) -> list[int]:
    """What the disk probe reports, one value per probe (the last one repeats); an Exception is raised."""
    calls: list[int] = []

    def probe() -> int:
        value = values[min(len(calls), len(values) - 1)]
        calls.append(1)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(da, "_free_bytes", probe)
    return calls


# --------------------------------------------------------------------------- #
# A dry run looks; an apply loads, once
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_dry_run_reports_what_an_apply_would_do_and_writes_nothing(world: World) -> None:
    f = seed(world)
    counters = await world.run([f], apply=False)
    assert counters == expect(requested=1, would_enqueue_new=1)
    assert world.downloads == []  # nothing left Drive
    assert world.book.enqueued == [] and world.book.rows == {}  # the queue is as it was
    assert len(world.book.lookups) == 1  # it did look the file up


@pytest.mark.asyncio
async def test_a_dry_run_tells_a_file_the_queue_has_from_one_it_lacks(world: World) -> None:
    present, absent = seed(world, 1), seed(world, 2)
    world.book.rows[f"drive:{present}"] = {}
    counters = await world.run([present, absent], apply=False)
    assert counters == expect(requested=2, already_present=1, would_enqueue_new=1)
    assert world.downloads == [] and world.book.enqueued == []


@pytest.mark.asyncio
async def test_an_apply_enqueues_the_file_once_with_the_drains_ref_and_path(world: World) -> None:
    f = seed(world)
    first = await world.run([f], apply=True)
    assert first == expect(requested=1, enqueued_new=1)
    assert world.downloads == [f]
    row = world.book.rows[f"drive:{f}"]
    assert row["source"] == "drive" and row["mime_type"] == PDF and row["byte_size"] == 7
    assert (
        row["source_path"] == f"{NAME}-dir/{NAME}-1.pdf"
    )  # the name goes to the row, as in the drain
    second = await world.run([f], apply=True)
    assert second == expect(requested=1, already_present=1)
    assert len(world.book.rows) == 1  # a second apply adds nothing


@pytest.mark.asyncio
async def test_a_direct_child_of_the_scope_is_its_bare_name_and_nested_folders_keep_their_path(
    world: World,
) -> None:
    world.drive.file(fid(1), f"{NAME}-loose.pdf", [SCOPE])
    world.drive.folder("A", f"{NAME}-a", [SCOPE])
    world.drive.folder("B", f"{NAME}-b", ["A"])
    world.drive.file(fid(2), f"{NAME}-deep.pdf", ["B"])
    await world.run([fid(1), fid(2)], apply=True)
    assert world.book.rows[f"drive:{fid(1)}"]["source_path"] == f"{NAME}-loose.pdf"
    assert world.book.rows[f"drive:{fid(2)}"]["source_path"] == f"{NAME}-a/{NAME}-b/{NAME}-deep.pdf"


@pytest.mark.asyncio
async def test_the_ids_are_read_one_at_a_time_in_the_order_given(world: World) -> None:
    ids = [seed(world, i) for i in (3, 1, 2)]
    await world.run(ids, apply=False)
    assert [call[0] for call in world.drive.gets if call[0] in ids] == ids


@pytest.mark.asyncio
async def test_the_file_is_read_with_the_fields_a_change_of_the_feed_carries(world: World) -> None:
    f = seed(world)
    await world.run([f], apply=False)
    [(_, fields, all_drives)] = world.drive.gets_of(f)
    assert {part.strip() for part in fields.split(",")} == {
        "id",
        "name",
        "mimeType",
        "parents",
        "trashed",
    }
    assert all_drives is True  # a file on a shared drive is still found


@pytest.mark.asyncio
async def test_the_queue_is_looked_up_by_the_drain_source_ref(world: World) -> None:
    f = seed(world)
    await world.run([f], apply=False)
    [(query, args)] = world.book.lookups
    assert args == (f"drive:{f}",)
    assert "intake_queue" in query and "source_ref" in query


# --------------------------------------------------------------------------- #
# What is skipped, what is gone, what failed: each counted, nothing written
# --------------------------------------------------------------------------- #
SKIPS = {
    "trashed": ("skipped_removed", {"trashed": True}),
    "native": ("skipped_native", {"mime": NATIVE}),
    "out-of-scope": ("skipped_out_of_scope", {"parents": ["OUTSIDE"]}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
@pytest.mark.parametrize("kind", list(SKIPS))
async def test_a_file_the_drain_would_skip_is_counted_and_nothing_is_written(
    world: World, kind: str, apply: bool
) -> None:
    bucket, extra = SKIPS[kind]
    f = seed(world, **extra)
    counters = await world.run([f], apply=apply)
    assert counters == expect(requested=1, **{bucket: 1})
    assert world.downloads == [] and world.book.enqueued == [] and world.book.rows == {}


FAULTS = {
    "gone-404": (lambda: drive_error(404, "notFound"), "missing"),
    "forbidden-403": (lambda: drive_error(403, "forbidden"), "missing"),
    "not-downloadable-403": (lambda: drive_error(403, "fileNotDownloadable"), "missing"),
    "rate-limit-403": (lambda: drive_error(403, "rateLimitExceeded"), "errors"),
    "quota-429": (lambda: drive_error(429, "rateLimitExceeded"), "errors"),
    "server-500": (lambda: drive_error(500, "backendError"), "errors"),
    "network": (lambda: ConnectionError("network"), "errors"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
@pytest.mark.parametrize("kind", list(FAULTS))
async def test_a_files_get_that_fails_is_missing_or_an_error_with_one_attempt_and_no_write(
    world: World, kind: str, apply: bool
) -> None:
    make, bucket = FAULTS[kind]
    f = seed(world)
    world.drive.errors[f] = make()
    counters = await world.run([f], apply=apply)
    assert counters == expect(requested=1, **{bucket: 1})
    assert len(world.drive.gets_of(f)) == 1  # no retry
    assert world.downloads == [] and world.book.rows == {} and world.book.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
async def test_an_ancestor_lookup_that_fails_is_an_error_and_one_drive_says_is_gone_is_out_of_scope(
    world: World, apply: bool
) -> None:
    f = seed(world)
    world.drive.errors["DIR"] = drive_error(500, "backendError")
    assert await world.run([f], apply=apply) == expect(requested=1, errors=1)
    world.drive.errors["DIR"] = drive_error(404, "notFound")
    assert await world.run([f], apply=apply) == expect(requested=1, skipped_out_of_scope=1)
    assert world.downloads == [] and world.book.rows == {}


@pytest.mark.asyncio
async def test_a_download_or_an_enqueue_that_fails_is_an_error_and_enqueues_nothing(
    world: World,
) -> None:
    down, perm, db = seed(world, 1), seed(world, 2), seed(world, 3)
    world.download_errors[down] = drive_error(500, "backendError")
    world.download_errors[perm] = drive_error(
        403, "forbidden"
    )  # permanent for the drain; here too an error
    world.book.fail[f"drive:{db}"] = RuntimeError("db down")
    counters = await world.run([down, perm, db], apply=True)
    assert counters == expect(requested=3, errors=3)
    assert world.book.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
async def test_every_requested_id_lands_in_exactly_one_counter(world: World, apply: bool) -> None:
    new, present, trashed, native, outside, gone, flaky, new2 = (
        seed(world, i) for i in range(1, 9)
    )
    world.book.rows[f"drive:{present}"] = {}
    world.drive.file(trashed, f"{NAME}-t", ["DIR"], trashed=True)
    world.drive.file(native, f"{NAME}-n", ["DIR"], mime=NATIVE)
    world.drive.file(outside, f"{NAME}-o", ["OUTSIDE"])
    del world.drive.items[gone]
    world.drive.errors[flaky] = drive_error(500, "backendError")
    ids = [new, present, trashed, native, outside, gone, flaky, new2]
    counters = await world.run(ids, apply=apply)
    loaded = "enqueued_new" if apply else "would_enqueue_new"
    assert counters == expect(
        requested=8,
        already_present=1,
        skipped_removed=1,
        skipped_native=1,
        skipped_out_of_scope=1,
        missing=1,
        errors=1,
        **{loaded: 2},
    )
    assert sum(counters[key] for key in BUCKETS) == counters["requested"]


@pytest.mark.asyncio
async def test_no_ids_means_no_drive_call(world: World) -> None:
    assert await world.run([], apply=True) == expect()
    assert world.drive.gets == [] and world.pauses == []


# --------------------------------------------------------------------------- #
# The disk floor guards every id of an apply, never a dry run
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_below_the_disk_floor_an_apply_downloads_nothing_and_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = [seed(world, i) for i in (1, 2, 3)]
    free_space(monkeypatch, GIB)  # 1 GiB free, the floor is 20
    with caplog.at_level(logging.ERROR, logger=da.logger.name):
        counters = await world.run(ids, apply=True)
    assert counters == expect(requested=3, disk_hold=1, not_attempted=3)
    assert world.downloads == [] and world.drive.gets == [] and world.book.rows == {}
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("below the 20.0 GiB floor" in message for message in errors), errors


@pytest.mark.asyncio
async def test_the_disk_is_checked_before_every_id_and_the_run_stops_at_the_first_breach(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [seed(world, i) for i in (1, 2, 3, 4)]
    probes = free_space(monkeypatch, 10**15, 10**15, GIB)  # room for two ids, then the floor
    counters = await world.run(ids, apply=True)
    assert counters == expect(requested=4, enqueued_new=2, disk_hold=1, not_attempted=2)
    assert world.downloads == ids[:2]
    assert len(probes) == 3  # one probe per id reached, none after the stop


@pytest.mark.asyncio
async def test_a_dry_run_is_not_stopped_by_a_full_disk_because_it_downloads_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [seed(world, i) for i in (1, 2, 3)]
    free_space(monkeypatch, GIB)
    counters = await world.run(ids, apply=False)
    assert counters == expect(requested=3, would_enqueue_new=3)


@pytest.mark.asyncio
async def test_a_disk_probe_that_fails_stops_an_apply_like_a_full_disk(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = seed(world)
    free_space(monkeypatch, OSError("no volume"))
    assert await world.run([f], apply=True) == expect(requested=1, disk_hold=1, not_attempted=1)
    assert world.downloads == [] and world.drive.gets == []


@pytest.mark.asyncio
async def test_the_floor_comes_from_the_environment_and_an_argument_beats_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = seed(world)
    monkeypatch.setenv("INTAKE_DRIVE_MIN_FREE_GIB", "1")
    free_space(monkeypatch, 2 * GIB)
    assert (await world.run([f], apply=True))["enqueued_new"] == 1  # 2 GiB free, floor 1
    free_space(monkeypatch, GIB // 2)
    assert (await world.run([f], apply=True))["disk_hold"] == 1  # 0.5 GiB free, floor 1
    free_space(monkeypatch, 50 * GIB)
    assert (await world.run([f], apply=True, min_free_gib=100))[
        "disk_hold"
    ] == 1  # the argument wins


# --------------------------------------------------------------------------- #
# Pace
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_there_is_a_short_pause_between_two_ids_and_none_around_them(world: World) -> None:
    ids = [seed(world, i) for i in (1, 2, 3)]
    await world.run(ids, apply=False)
    assert world.pauses == [da._REENQUEUE_PAUSE_S] * 2
    assert 0 < da._REENQUEUE_PAUSE_S <= 2
    world.pauses.clear()
    await world.run(ids[:1], apply=False)
    assert world.pauses == []
    await world.run(ids, apply=False, pause_s=0.5)
    assert world.pauses == [0.5, 0.5]


# --------------------------------------------------------------------------- #
# Input that must stop the run before any Drive call
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("apply", ["yes", "no", 1, 0, None, "", [], [1]])
async def test_an_apply_that_is_not_a_bool_stops_the_run(world: World, apply: Any) -> None:
    f = seed(world)
    with pytest.raises(TypeError):
        await world.run([f], apply=apply)
    assert world.drive.gets == [] and world.downloads == [] and world.book.rows == {}


def test_apply_has_no_default_so_a_caller_always_says_which() -> None:
    parameter = inspect.signature(da.reenqueue_drive_files).parameters["apply"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


BAD_IDS = [
    "../../etc/passwd",
    "short",
    "",
    "abcdefghij\n",
    "abcdefghij ",
    "id with space 12",
    "a" * 101,
    "file/0000001",
    "file.0000001",
    "ünicodeid1234",
    None,
    12345678901,
    b"file0000001",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_IDS, ids=[repr(b)[:20] for b in BAD_IDS])
async def test_an_entry_that_is_not_a_drive_file_id_stops_the_run_before_any_drive_call(
    world: World, bad: Any
) -> None:
    good = seed(world)
    with pytest.raises(ValueError, match="entry 1 is not a Drive file id"):
        await world.run([good, bad], apply=True)  # the good one first: it must not be read either
    assert world.drive.gets == [] and world.downloads == [] and world.book.rows == {}


@pytest.mark.asyncio
async def test_the_error_for_a_bad_entry_names_its_position_never_its_content(world: World) -> None:
    secret = f"{NAME} passport"
    with pytest.raises(ValueError) as caught:
        await world.run([fid(1), secret], apply=False)
    assert NAME not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "   ", None, 7])
async def test_an_empty_scope_stops_the_run_before_any_drive_call(world: World, scope: Any) -> None:
    f = seed(world)
    with pytest.raises(ValueError):
        await da.reenqueue_drive_files(world.book, world.drive, scope, [f], apply=False)
    assert world.drive.gets == []


# --------------------------------------------------------------------------- #
# The queue lookup is a verdict only when it is a bool
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [None, 0, 1, "", "t", "false", [], [True], 0.0, b"t"])
async def test_a_queue_lookup_that_does_not_answer_with_a_bool_is_an_error_not_a_guess(
    world: World, reply: Any
) -> None:
    f = seed(world)
    world.book.reply = reply
    assert await world.run([f], apply=False) == expect(requested=1, errors=1)


@pytest.mark.asyncio
async def test_a_plain_true_or_false_from_the_queue_is_believed(world: World) -> None:
    f = seed(world)
    world.book.reply = True
    assert await world.run([f], apply=False) == expect(requested=1, already_present=1)
    world.book.reply = False
    assert await world.run([f], apply=False) == expect(requested=1, would_enqueue_new=1)


@pytest.mark.asyncio
async def test_a_queue_lookup_that_raises_is_an_error(world: World) -> None:
    f = seed(world)
    world.book.lookup_error = RuntimeError("db down")
    assert await world.run([f], apply=False) == expect(requested=1, errors=1)


@pytest.mark.asyncio
async def test_a_re_enqueue_never_touches_the_cursor(world: World) -> None:
    ids = [seed(world, i) for i in (1, 2)]
    await world.run(ids, apply=False)
    await world.run(ids, apply=True)
    assert world.book.executed == []
    assert not any("system_settings" in query for query, _ in world.book.lookups)


# --------------------------------------------------------------------------- #
# PII: ids and counts only
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_log_line_and_no_output_carries_a_file_or_folder_name(
    world: World, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    named = f"cannot read {NAME}"
    ok, files_get, ancestor, download, path, enqueue_ = (seed(world, i) for i in range(1, 7))
    world.drive.errors[files_get] = drive_error(500, "backendError", message=named)
    world.drive.folder("BADDIR", f"{NAME}-bad", [SCOPE])
    world.drive.file(ancestor, f"{NAME}-x", ["BADDIR"])
    world.drive.errors["BADDIR"] = drive_error(500, "backendError", message=named)
    world.download_errors[download] = drive_error(500, "backendError", message=named)
    world.download_errors[path] = PermissionError(13, "Permission denied", f"/blobs/{NAME}.pdf")
    world.book.fail[f"drive:{enqueue_}"] = RuntimeError(named)
    ids = [ok, files_get, ancestor, download, path, enqueue_]
    with caplog.at_level(logging.DEBUG):
        counters = await world.run(ids, apply=True)
    assert counters == expect(requested=6, enqueued_new=1, errors=5)  # every fault really happened
    assert (
        NAME in world.book.rows[f"drive:{ok}"]["source_path"]
    )  # and the name IS in play: the row only
    assert NAME not in caplog.text and not any(NAME in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)  # no traceback
    out = capsys.readouterr()
    assert NAME not in out.out + out.err and out.out == ""
    world.book.lookup_error = RuntimeError(named)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await world.run([ok], apply=False)
    assert NAME not in caplog.text


NAMEY = {"file_name", "source_path", "dest", "rel_dir", "file_meta", "meta", "segments"}


def _names_in_logs(source: str) -> list[str]:
    """Logger calls whose arguments mention a variable that holds a file name, a folder name or a path."""
    bad = []
    for node in ast.walk(ast.parse(source)):
        is_log = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        )
        if not is_log:
            continue
        used = {
            n.id
            for arg in (*node.args, *(k.value for k in node.keywords))
            for n in ast.walk(arg)
            if isinstance(n, ast.Name)
        }
        if used & NAMEY:
            bad.append(f"line {node.lineno}: logs {sorted(used & NAMEY)}")
    return bad


def test_the_adapter_never_logs_a_variable_that_holds_a_name_or_a_path() -> None:
    assert _names_in_logs(Path(da.__file__).read_text()) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "logger.info('x %s', file_name)",
        "logger.warning(f'x {source_path}')",
        "logger.error('x %s', dest)",
        "logger.error('x %s', file_meta.get('name'))",
        "logger.debug('x %s', '/'.join(segments))",
    ],
)
def test_the_name_checker_catches_every_shape_it_forbids(snippet: str) -> None:
    assert _names_in_logs(snippet)


def test_the_name_checker_lets_an_id_and_a_summary_through() -> None:
    assert _names_in_logs("logger.info('x %s (%s)', file_id, why)") == []


# --------------------------------------------------------------------------- #
# Real SQL and the real enqueue (local nuzantara_test; skipped when it is unreachable)
# --------------------------------------------------------------------------- #
class Real:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self.refs: list[str] = []
        self.hashes: list[str] = []

    async def rows(self, ref: str) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT source, source_path, status, source_ref FROM intake_queue"
                " WHERE source_ref = $1",
                ref,
            )


@pytest_asyncio.fixture
async def real() -> Any:
    try:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    except Exception:
        pytest.skip(f"local intake DB not reachable at {DSN}")
    handle = Real(pool)
    try:
        async with pool.acquire() as conn:
            there = await conn.fetchval(
                "SELECT to_regclass('public.intake_queue') IS NOT NULL"
                " AND to_regclass('public.document_instances') IS NOT NULL"
            )
        if not there:
            pytest.skip("the intake tables are not in the test database")
        yield handle
    finally:
        async with pool.acquire() as conn:
            if handle.refs:
                await conn.execute(
                    "DELETE FROM intake_queue WHERE source_ref = ANY($1::text[])", handle.refs
                )
            if handle.hashes:
                await conn.execute(
                    "DELETE FROM document_instances WHERE blob_hash = ANY($1::char(64)[])",
                    handle.hashes,
                )
        await pool.close()


@pytest.mark.asyncio
async def test_the_queue_lookup_finds_a_real_row_by_its_source_ref(
    real: Real, tmp_path: Path
) -> None:
    f = f"reenq{uuid.uuid4().hex[:12]}"
    assert await da._already_queued(real.pool, f) is False
    blob = tmp_path / "blob.bin"
    blob.write_bytes(uuid.uuid4().bytes * 8)
    real.refs.append(f"drive:{f}")
    real.hashes.append(compute_blob_hash(blob))
    await enqueue(real.pool, source="drive", source_ref=f"drive:{f}", blob_path=blob)
    assert await da._already_queued(real.pool, f) is True
    assert await da._already_queued(real.pool, f + "x") is False
    assert await da._already_queued(real.pool, f[:-1]) is False


@pytest.mark.asyncio
async def test_against_the_real_queue_an_apply_enqueues_once_and_a_second_apply_adds_nothing(
    real: Real, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = World(
        monkeypatch, tmp_path, fake_queue=False
    )  # the REAL enqueue; only the download is replaced
    f = f"reenq{uuid.uuid4().hex[:12]}"
    world.content = uuid.uuid4().bytes * 8
    real.refs.append(f"drive:{f}")
    real.hashes.append(hashlib.sha256(world.content).hexdigest())
    world.drive.folder("DIR", f"{NAME}-dir", [SCOPE])
    world.drive.file(f, f"{NAME}.pdf", ["DIR"])

    def go(apply: bool) -> Any:
        return da.reenqueue_drive_files(real.pool, world.drive, SCOPE, [f], apply=apply)

    assert (await go(False)) == expect(requested=1, would_enqueue_new=1)
    assert await real.rows(f"drive:{f}") == [] and world.downloads == []  # a dry run left no row
    assert (await go(True)) == expect(requested=1, enqueued_new=1)
    [row] = await real.rows(f"drive:{f}")
    assert (row["source"], row["status"]) == ("drive", "pending")
    assert row["source_path"] == f"{NAME}-dir/{NAME}.pdf"
    assert (await go(True)) == expect(requested=1, already_present=1)
    assert len(await real.rows(f"drive:{f}")) == 1  # idempotent through intake_key
    assert (await go(False)) == expect(requested=1, already_present=1)


@pytest.mark.asyncio
async def test_against_the_real_queue_a_changed_file_reads_present_in_a_dry_run_and_new_on_apply(
    real: Real, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What the CLI help promises: the dry run asks by source_ref, the apply decides by intake_key."""
    world = World(monkeypatch, tmp_path, fake_queue=False)
    f = f"reenq{uuid.uuid4().hex[:12]}"
    real.refs.append(f"drive:{f}")
    world.drive.folder("DIR", f"{NAME}-dir", [SCOPE])
    world.drive.file(f, f"{NAME}.pdf", ["DIR"])

    def go(apply: bool) -> Any:
        return da.reenqueue_drive_files(real.pool, world.drive, SCOPE, [f], apply=apply)

    world.content = uuid.uuid4().bytes * 8  # the file as it was when it was queued
    real.hashes.append(hashlib.sha256(world.content).hexdigest())
    assert (await go(True)) == expect(requested=1, enqueued_new=1)
    world.content = uuid.uuid4().bytes * 8  # the same Drive id, changed since
    real.hashes.append(hashlib.sha256(world.content).hexdigest())
    assert (await go(False)) == expect(requested=1, already_present=1)  # the estimate
    assert (await go(True)) == expect(requested=1, enqueued_new=1)  # the apply: a new row
    assert len(await real.rows(f"drive:{f}")) == 2
    assert (await go(True)) == expect(requested=1, already_present=1)  # and then nothing more
    assert len(await real.rows(f"drive:{f}")) == 2
