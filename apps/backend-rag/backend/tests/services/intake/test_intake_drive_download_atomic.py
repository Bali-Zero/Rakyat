"""A download lands whole or not at all: ``_download_file`` stages the bytes beside the destination and renames them.

Before, ``_download_file`` wrote straight onto the destination. A write that failed part way left a truncated
file there and, when a valid blob was already at that path, truncated it: ``enqueue`` then hashes whatever is on
disk. The drain and the re-enqueue both come through this function.

GUILT: a write that fails part way (disk full, I/O error, an interrupt) that leaves a truncated file at the
destination or a stage behind; a blob that was already there and is truncated, or changed at all, by a failed
rewrite; a final rename that fails and leaves a stage; a stage that is not beside the destination or whose name
holds a part of the Drive name; a file descriptor left open; a direct write of a blob anywhere in the adapter;
the drain or the re-enqueue not going through the same helper; a blob downloaded again that comes out with other
permission bits than an in-place write leaves it (a 0o600 or 0o640 blob made 0o644), a stage more permissive than
0o600 while bytes are written, the umask read through os.umask(), a failed write or chmod that changes the bytes or
the mode of the blob already there.
INNOCENCE: a download that works gives the same bytes at the same path, the same returned size and the mode an
in-place write leaves (a blob already there keeps its permission bits, a new one gets 0o666 minus the umask, under
any umask), so the drain's live behaviour on success is unchanged, whether the file is empty or a megabyte,
whether the directory exists yet or not, with a 255-byte name; a write that takes fewer bytes than it was given is
finished; a reader never sees half a blob while writers replace it.
Synthetic payloads and names only, under tmp_path; the Drive and the queue are fakes.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import errno
import os
import re
import stat
import threading
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.services.intake import drive_adapter as da
from backend.services.intake.enqueue import EnqueueResult

NAME = "ZQX-sentinel-6f1c"
SCOPE = "SCOPE"
ADAPTER = Path(da.__file__)
PAYLOADS = {
    "empty": b"",
    "one-byte": b"x",
    "seven": b"1234567",
    "a-page": bytes(range(256)) * 16,
    "a-megabyte-and-one": (b"synthetic-blob-" * 70000)[: 2**20 + 1],
}


class Downloader:
    """googleapiclient's MediaIoBaseDownload as far as ``_download_file`` uses it: one chunk, then done."""

    def __init__(self, buf: Any, request: Any) -> None:
        self.buf, self.request = buf, request

    def next_chunk(self) -> tuple[None, bool]:
        self.buf.write(self.request.payload)
        return None, True


class Drive:
    """The Drive service a download and a re-enqueue read: files.get for metadata, get_media for bytes."""

    def __init__(
        self, payloads: dict[str, bytes], metas: dict[str, dict[str, Any]] | None = None
    ) -> None:
        self.payloads = payloads
        self.metas = metas or {}
        self.service = types.SimpleNamespace(files=lambda: self)

    def get_media(self, *, fileId: str) -> Any:
        return types.SimpleNamespace(payload=self.payloads[fileId])

    def get(self, *, fileId: str, fields: str, supportsAllDrives: bool = False) -> Any:
        meta = self.metas[fileId]
        return types.SimpleNamespace(execute=lambda: dict(meta))

    async def list_changes_since(
        self, token: str, page_size: int, max_pages: int
    ) -> dict[str, Any]:
        return {"changes": self.changes, "new_page_token": "next", "more_pages": False}

    changes: list[dict[str, Any]] = []


class Queue:
    """What the drain's and the re-enqueue's calls to ``enqueue`` and to the cursor store are given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cursor_writes: list[tuple[Any, ...]] = []

    async def enqueue(self, pool: Any, **kwargs: Any) -> EnqueueResult:
        self.calls.append(kwargs)
        return EnqueueResult(instance_id=1, queue_id=len(self.calls), was_new=True)

    def acquire(self) -> Any:
        queue = self

        class _Conn:
            async def execute(self, query: str, *args: Any) -> str:
                queue.cursor_writes.append(args)
                return "INSERT 0 1"

        class _Ctx:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


@pytest.fixture
def blobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The directory the tests write into; the adapter's blob root points at its parent."""
    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", Downloader)
    monkeypatch.setattr(da, "_BLOB_ROOT", tmp_path / "root")
    monkeypatch.setattr(da, "_free_bytes", lambda: 10**15)
    monkeypatch.delenv("INTAKE_DRIVE_MIN_FREE_GIB", raising=False)
    return tmp_path / "blobs"


@pytest.fixture
def umask022() -> Iterator[None]:
    """A plain write gets 0o666 minus the umask: pin it so the mode the tests compare is not the host's."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def break_the_write(
    monkeypatch: pytest.MonkeyPatch, *, after: int = 3, error: BaseException | None = None
) -> list[int]:
    """Make the write take ``after`` bytes and then fail; the list says what the stage held at that moment."""
    held: list[int] = []

    def broken(fd: int, data: bytes) -> None:
        os.write(fd, data[:after])
        held.append(os.fstat(fd).st_size)
        raise error or OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(da, "_write_all", broken)
    return held


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# --------------------------------------------------------------------------- #
# A download that works: the same bytes, size and mode as the plain write it replaces
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(PAYLOADS))
async def test_a_download_that_works_gives_the_bytes_the_size_and_the_mode_of_a_plain_write(
    kind: str, blobs: Path, umask022: None
) -> None:
    payload = PAYLOADS[kind]
    dest = blobs / "deeper" / "still" / "blob.bin"  # directories that do not exist yet
    size = await da._download_file(Drive({"f1": payload}), "f1", dest)
    assert size == len(payload) and dest.read_bytes() == payload
    control = blobs / "control.bin"
    control.write_bytes(payload)  # what the code did before: a plain write
    assert stat.S_IMODE(dest.stat().st_mode) == stat.S_IMODE(control.stat().st_mode) == 0o644
    assert names(dest.parent) == ["blob.bin"]  # and no stage is left beside it


@pytest.mark.asyncio
async def test_a_download_over_an_existing_blob_replaces_it_whole(blobs: Path) -> None:
    dest = blobs / "blob.bin"
    blobs.mkdir(parents=True)
    dest.write_bytes(b"old bytes, longer than the new ones are")
    assert await da._download_file(Drive({"f1": b"new"}), "f1", dest) == 3
    assert dest.read_bytes() == b"new" and names(blobs) == ["blob.bin"]


@pytest.mark.asyncio
async def test_a_destination_with_a_255_byte_name_still_downloads(blobs: Path) -> None:
    dest = blobs / (
        "a" * 255
    )  # the stage must not be named after it: a suffix would pass the limit
    assert await da._download_file(Drive({"f1": b"abc"}), "f1", dest) == 3
    assert dest.read_bytes() == b"abc" and names(blobs) == [dest.name]


# --------------------------------------------------------------------------- #
# A blob downloaded again keeps the permission bits an in-place write leaves it (gate round 2, R2)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _umask(value: int) -> Iterator[None]:
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _blob_at(path: Path, mode: int | None) -> Path:
    """A blob already on disk with exactly ``mode`` whatever the umask says (None: nothing there yet)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        path.write_bytes(b"older bytes, longer than the new ones are")
        os.chmod(path, mode)
    return path


EXISTING = [0o600, 0o640, 0o644, 0o664, None]
EXISTING_IDS = ["existing-0600", "existing-0640", "existing-0644", "existing-0664", "new"]


@pytest.mark.asyncio
@pytest.mark.parametrize("umask", [0o022, 0o077], ids=["umask-022", "umask-077"])
@pytest.mark.parametrize("existing", EXISTING, ids=EXISTING_IDS)
async def test_a_download_leaves_the_mode_an_in_place_write_leaves(
    existing: int | None, umask: int, blobs: Path
) -> None:
    """The base wrote with ``dest.write_bytes``: a blob already there kept its mode, a new one got 0o666 minus the umask."""
    theirs = _blob_at(blobs / "in-place" / "blob.bin", existing)
    ours = _blob_at(blobs / "staged" / "blob.bin", existing)
    with _umask(umask):
        theirs.write_bytes(b"new bytes")  # the base's routine, on a twin file
        assert await da._download_file(Drive({"f1": b"new bytes"}), "f1", ours) == 9
    assert ours.read_bytes() == b"new bytes" and names(ours.parent) == ["blob.bin"]
    assert _mode(ours) == _mode(theirs) == (0o666 & ~umask if existing is None else existing)


@pytest.mark.parametrize(
    "umask", [0o022, 0o077, 0o002], ids=["umask-022", "umask-077", "umask-002"]
)
@pytest.mark.parametrize("existing", EXISTING, ids=EXISTING_IDS)
def test_the_stage_is_never_more_permissive_than_0600_while_bytes_are_written(
    existing: int | None, umask: int, blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = _blob_at(blobs / "blob.bin", existing)
    seen: list[int] = []

    def watched(fd: int, data: bytes) -> None:
        for start in range(0, len(data), 3):  # a look at the stage before each chunk
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
            os.write(fd, data[start : start + 3])

    monkeypatch.setattr(da, "_write_all", watched)
    with _umask(umask):
        da._write_atomically(dest, b"0123456789")
    assert len(seen) == 4
    assert all(mode & ~0o600 == 0 for mode in seen), [oct(mode) for mode in seen]


@pytest.mark.parametrize("existing", [0o600, 0o640, 0o644], ids=["0600", "0640", "0644"])
def test_a_failed_write_leaves_the_bytes_and_the_mode_of_the_blob_already_there(
    existing: int, blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = _blob_at(blobs / "blob.bin", existing)
    before = (dest.read_bytes(), _mode(dest), dest.stat().st_ino)
    break_the_write(monkeypatch)
    with pytest.raises(OSError):
        da._write_atomically(dest, b"the new bytes")
    assert (dest.read_bytes(), _mode(dest), dest.stat().st_ino) == before
    assert names(blobs) == ["blob.bin"]


@pytest.mark.parametrize("fails_on", [1, 2], ids=["narrowing", "final-mode"])
def test_a_chmod_that_fails_leaves_the_blob_untouched_and_no_stage(
    fails_on: int, blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = _blob_at(blobs / "blob.bin", 0o640)
    real, calls = os.fchmod, []

    def flaky(fd: int, mode: int) -> None:
        calls.append(mode)
        if len(calls) == fails_on:
            raise OSError(errno.EPERM, "Operation not permitted")
        real(fd, mode)

    monkeypatch.setattr(os, "fchmod", flaky)
    with pytest.raises(OSError) as raised:
        da._write_atomically(dest, b"the new bytes")
    assert raised.value.errno == errno.EPERM and len(calls) == fails_on
    assert dest.read_bytes().startswith(b"older") and _mode(dest) == 0o640
    assert names(blobs) == ["blob.bin"]


def test_a_destination_whose_mode_cannot_be_read_is_an_error_and_not_a_new_blob(
    blobs: Path,
) -> None:
    blobs.mkdir(parents=True)
    dest = blobs / "loop.bin"
    dest.symlink_to(dest.name)  # a link to itself: stat() says ELOOP, as the base's open() does
    with pytest.raises(OSError) as raised:
        da._write_atomically(dest, b"abc")
    assert raised.value.errno == errno.ELOOP
    assert dest.is_symlink() and names(blobs) == ["loop.bin"]


def test_the_umask_is_never_read_through_os_umask(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    there = _blob_at(blobs / "there.bin", 0o640)

    def forbidden(mask: int) -> int:
        raise AssertionError("os.umask is process-global: reading it changes it for every thread")

    with monkeypatch.context() as patched:
        patched.setattr(os, "umask", forbidden)
        da._write_atomically(blobs / "new.bin", b"abc")
        da._write_atomically(there, b"abc")
    assert _mode(there) == 0o640


# --------------------------------------------------------------------------- #
# A write that fails: nothing at the destination, nothing beside it, an old blob untouched
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_write_that_fails_part_way_leaves_no_file_and_no_stage(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = blobs / "blob.bin"
    held = break_the_write(monkeypatch)
    with pytest.raises(OSError) as raised:
        await da._download_file(Drive({"f1": b"0123456789"}), "f1", dest)
    assert raised.value.errno == errno.ENOSPC
    assert held == [3]  # the stage really held 3 bytes when the write failed
    assert not dest.exists() and names(blobs) == []


@pytest.mark.asyncio
async def test_an_existing_valid_blob_survives_a_failed_rewrite_byte_identical(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = blobs / "blob.bin"
    blobs.mkdir(parents=True)
    dest.write_bytes(b"a valid blob that was already here")
    before = dest.stat()
    break_the_write(monkeypatch)
    with pytest.raises(OSError):
        await da._download_file(Drive({"f1": b"the new bytes"}), "f1", dest)
    after = dest.stat()
    assert dest.read_bytes() == b"a valid blob that was already here"
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert names(blobs) == ["blob.bin"]


@pytest.mark.asyncio
async def test_a_final_rename_that_fails_leaves_no_stage_and_touches_nothing(blobs: Path) -> None:
    dest = blobs / "blob.bin"
    dest.mkdir(
        parents=True
    )  # a directory where the file should go: os.replace cannot put a file on it
    (dest / "keep.txt").write_bytes(b"keep")
    with pytest.raises(OSError):
        await da._download_file(Drive({"f1": b"abc"}), "f1", dest)
    assert (dest / "keep.txt").read_bytes() == b"keep" and names(blobs) == ["blob.bin"]


@pytest.mark.parametrize(
    "raised",
    [KeyboardInterrupt, asyncio.CancelledError, SystemExit],
    ids=["interrupt", "cancel", "exit"],
)
def test_an_interrupt_during_the_write_leaves_no_stage_either(
    raised: type[BaseException], blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs.mkdir(parents=True)
    break_the_write(monkeypatch, error=raised())
    with pytest.raises(raised):
        da._write_atomically(blobs / "blob.bin", b"0123456789")
    assert names(blobs) == []


def test_a_failed_write_is_not_swallowed(blobs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blobs.mkdir(parents=True)
    break_the_write(monkeypatch, error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        da._write_atomically(blobs / "blob.bin", b"abc")


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_no_file_descriptor_is_left_open_by_a_good_or_a_failed_write(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs.mkdir(parents=True)
    before = _open_fds()
    for i in range(20):
        da._write_atomically(blobs / f"ok{i}.bin", b"abc")
    with monkeypatch.context() as broken:
        broken.setattr(da, "_write_all", lambda fd, data: (_ for _ in ()).throw(OSError("full")))
        for _ in range(20):
            with pytest.raises(OSError):
                da._write_atomically(blobs / "bad.bin", b"abc")
    assert _open_fds() == before and not (blobs / "bad.bin").exists()


@pytest.mark.asyncio
async def test_a_stage_that_cannot_be_created_raises_and_leaves_nothing(blobs: Path) -> None:
    with pytest.raises(FileNotFoundError):
        da._write_atomically(blobs / "blob.bin", b"abc")  # blobs itself does not exist
    assert not blobs.exists()


def test_a_stage_name_that_is_already_taken_is_never_overwritten_or_removed(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs.mkdir(parents=True)
    taken = blobs / ".stage-0101010101010101.part"
    taken.write_bytes(b"somebody else's file")
    with monkeypatch.context() as fixed:
        fixed.setattr(da.os, "urandom", lambda n: b"\x01" * n)
        with pytest.raises(FileExistsError):
            da._write_atomically(blobs / "blob.bin", b"abc")
    assert taken.read_bytes() == b"somebody else's file" and names(blobs) == [taken.name]


def test_the_error_the_caller_sees_is_the_write_error_even_when_the_stage_is_already_gone(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs.mkdir(parents=True)

    def vanishing(fd: int, data: bytes) -> None:
        for stage in blobs.iterdir():  # somebody removes the stage under us
            stage.unlink()
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(da, "_write_all", vanishing)
    with pytest.raises(OSError) as raised:
        da._write_atomically(blobs / "blob.bin", b"abc")
    assert raised.value.errno == errno.ENOSPC and names(blobs) == []


# --------------------------------------------------------------------------- #
# The stage: beside the destination, named by nobody's name
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_stage_is_beside_the_destination_and_its_name_holds_no_part_of_the_drive_name(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    real_write_all = da._write_all

    def watching(fd: int, data: bytes) -> None:
        seen.append(names(blobs))
        real_write_all(fd, data)

    monkeypatch.setattr(da, "_write_all", watching)
    dest = blobs / f"{NAME}-passport.pdf"
    await da._download_file(Drive({"f1": b"abc"}), "f1", dest)
    [during] = seen
    assert len(during) == 1 and re.fullmatch(r"\.stage-[0-9a-f]{16}\.part", during[0])
    assert NAME not in during[0] and "passport" not in during[0]
    assert names(blobs) == [dest.name]


# --------------------------------------------------------------------------- #
# The write itself: every byte, and a loop that cannot spin
# --------------------------------------------------------------------------- #
def test_a_write_that_takes_fewer_bytes_than_it_was_given_is_finished(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs.mkdir(parents=True)
    real_write = os.write
    chunks: list[int] = []

    def two_at_a_time(fd: int, data: Any) -> int:
        chunks.append(len(data))
        return real_write(fd, bytes(data[:2]))

    dest = blobs / "blob.bin"
    with monkeypatch.context() as short:
        short.setattr(os, "write", two_at_a_time)
        da._write_atomically(dest, b"0123456789")
    assert dest.read_bytes() == b"0123456789" and chunks == [10, 8, 6, 4, 2]


def test_a_write_that_makes_no_progress_is_an_error_not_a_loop(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs.mkdir(parents=True)
    calls: list[int] = []

    def stuck(fd: int, data: Any) -> int:
        calls.append(fd)
        assert len(calls) < 4, "the loop kept calling a write that wrote nothing"
        return 0

    with monkeypatch.context() as stalled:
        stalled.setattr(os, "write", stuck)
        with pytest.raises(OSError, match="no progress"):
            da._write_atomically(blobs / "blob.bin", b"abc")
    assert len(calls) == 1 and names(blobs) == []


def test_a_reader_never_sees_a_partial_blob_while_writers_replace_it(blobs: Path) -> None:
    blobs.mkdir(parents=True)
    dest = blobs / "blob.bin"
    payloads = [bytes([i]) * (256 * 1024) for i in range(1, 7)]
    allowed = set(payloads)
    dest.write_bytes(payloads[0])
    finished = threading.Event()
    torn: list[int] = []
    faults: list[str] = []

    def reader() -> None:
        try:
            while not finished.is_set():
                data = dest.read_bytes()
                if data not in allowed:
                    torn.append(len(data))
        except Exception as exc:
            faults.append(type(exc).__name__)

    def writer(payload: bytes) -> None:
        try:
            for _ in range(25):
                da._write_atomically(dest, payload)
        except Exception as exc:
            faults.append(type(exc).__name__)

    watcher = threading.Thread(target=reader)
    writers = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    watcher.start()
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    finished.set()
    watcher.join()
    assert faults == [] and torn == []
    assert dest.read_bytes() in allowed and names(blobs) == ["blob.bin"]


# --------------------------------------------------------------------------- #
# The drain and the re-enqueue come through the same helper
# --------------------------------------------------------------------------- #
FILE_ID = "file0000001"
PAYLOAD = b"synthetic-bytes-of-a-file"


def _change() -> dict[str, Any]:
    meta = {
        "id": FILE_ID,
        "name": f"{NAME}.pdf",
        "mimeType": "application/pdf",
        "parents": [SCOPE],
        "trashed": False,
    }
    return {"fileId": FILE_ID, "removed": False, "file": meta}


def _world(monkeypatch: pytest.MonkeyPatch) -> tuple[Drive, Queue, Path]:
    drive = Drive({FILE_ID: PAYLOAD}, {FILE_ID: _change()["file"]})
    drive.changes = [_change()]
    queue = Queue()
    monkeypatch.setattr(da, "enqueue", queue.enqueue)
    return drive, queue, da._local_blob_path(FILE_ID, f"{NAME}.pdf")


@pytest.mark.asyncio
async def test_the_drains_change_lands_whole_through_the_helper(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, queue, dest = _world(monkeypatch)
    counters, failures = da._new_counters(), []
    await da._ingest_change(queue, drive, SCOPE, _change(), {}, counters, failures)
    assert (counters["enqueued_new"], counters["errors"], failures) == (1, 0, [])
    assert dest.read_bytes() == PAYLOAD and names(dest.parent) == [dest.name]
    [call] = queue.calls
    assert (call["blob_path"], call["byte_size"]) == (dest, len(PAYLOAD))  # what enqueue hashes


@pytest.mark.asyncio
async def test_the_drains_change_with_a_failing_write_is_an_error_and_leaves_nothing(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, queue, dest = _world(monkeypatch)
    break_the_write(monkeypatch)
    counters, failures = da._new_counters(), []
    await da._ingest_change(queue, drive, SCOPE, _change(), {}, counters, failures)
    assert (counters["enqueued_new"], counters["errors"]) == (0, 1)
    assert failures == [(FILE_ID, False)] and queue.calls == []  # nothing was enqueued
    assert not dest.exists() and names(dest.parent) == []


@pytest.mark.asyncio
async def test_a_drain_page_holds_its_cursor_on_a_failing_write_and_leaves_nothing(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, queue, dest = _world(monkeypatch)
    break_the_write(monkeypatch)
    page = await da._drain_page(queue, drive, SCOPE, "token", 0, 50)
    assert (page.held, page.advanced, page.cursor) == (True, False, "token")
    assert page.counters["errors"] == 1 and not dest.exists() and names(dest.parent) == []


@pytest.mark.asyncio
async def test_a_drain_page_with_a_good_write_advances_and_the_blob_is_whole(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, queue, dest = _world(monkeypatch)
    page = await da._drain_page(queue, drive, SCOPE, "token", 0, 50)
    assert (page.held, page.advanced, page.cursor) == (False, True, "next")
    assert page.counters["enqueued_new"] == 1 and dest.read_bytes() == PAYLOAD


@pytest.mark.asyncio
async def test_the_reenqueue_lands_whole_and_a_failing_write_is_an_error_that_leaves_nothing(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, queue, dest = _world(monkeypatch)
    ok = await da.reenqueue_drive_files(queue, drive, SCOPE, [FILE_ID], apply=True)
    assert (ok["enqueued_new"], ok["errors"]) == (1, 0) and dest.read_bytes() == PAYLOAD
    dest.unlink()
    break_the_write(monkeypatch)
    bad = await da.reenqueue_drive_files(queue, drive, SCOPE, [FILE_ID], apply=True)
    assert (bad["enqueued_new"], bad["errors"]) == (0, 1)
    assert not dest.exists() and names(dest.parent) == [] and len(queue.calls) == 1


@pytest.mark.asyncio
async def test_the_drain_and_the_reenqueue_write_through_one_helper(
    blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, queue, dest = _world(monkeypatch)
    written: list[tuple[Path, bytes]] = []
    real = da._write_atomically

    def recording(path: Path, data: bytes) -> None:
        written.append((path, data))
        real(path, data)

    monkeypatch.setattr(da, "_write_atomically", recording)
    await da._ingest_change(queue, drive, SCOPE, _change(), {}, da._new_counters(), [])
    await da.reenqueue_drive_files(queue, drive, SCOPE, [FILE_ID], apply=True)
    assert written == [(dest, PAYLOAD), (dest, PAYLOAD)]


class _Shapes(ast.NodeVisitor):
    """Every way the adapter could put bytes on disk, and every call of the two write helpers, by enclosing function."""

    METHODS = {
        "write",
        "write_bytes",
        "write_text",
        "writelines",
        "open",
        "touch",
        "symlink_to",
        "hardlink_to",
    }
    OS_CALLS = {"replace", "rename", "link", "symlink", "truncate", "ftruncate"}
    SHUTIL_CALLS = {"copy", "copy2", "copyfile", "copyfileobj", "move"}

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.writes: list[tuple[str, str]] = []
        self.calls: dict[str, list[str]] = {"_write_all": [], "_write_atomically": []}

    def _scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _scope
    visit_AsyncFunctionDef = _scope

    def visit_Call(self, node: ast.Call) -> None:
        where = self.stack[-1] if self.stack else "<module>"
        func = node.func
        if isinstance(func, ast.Attribute):
            receiver = func.value.id if isinstance(func.value, ast.Name) else None
            if (
                func.attr in self.METHODS
                or (receiver == "os" and func.attr in self.OS_CALLS)
                or (receiver == "shutil" and func.attr in self.SHUTIL_CALLS)
            ):
                self.writes.append((where, ast.unparse(func)))
        if isinstance(func, ast.Name) and func.id == "open":
            self.writes.append((where, "open"))
        if isinstance(func, ast.Name) and func.id in self.calls:
            self.calls[func.id].append(where)
        self.generic_visit(node)


def _shapes_of(source: str) -> _Shapes:
    shapes = _Shapes()
    shapes.visit(ast.parse(source))
    return shapes


def test_the_adapter_puts_no_bytes_on_disk_by_any_other_way() -> None:
    """The adapter's only file writes are the stage, its write loop and its rename, in the two helpers.

    A direct ``write_bytes``/``open(..., "w")``/``shutil`` copy anywhere else would bring the truncated blob
    back; ``_write_all`` is called only by ``_write_atomically`` and that only by ``_download_file``.
    """
    shapes = _shapes_of(ADAPTER.read_text())
    assert shapes.writes == [
        ("_write_all", "os.write"),
        ("_write_atomically", "os.open"),
        ("_write_atomically", "os.replace"),
    ]
    assert shapes.calls == {
        "_write_all": ["_write_atomically"],
        "_write_atomically": ["_download_file"],
    }


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("def f(dest, data):\n    dest.write_bytes(data)\n", [("f", "dest.write_bytes")]),
        (
            "def f(p, data):\n    with open(p, 'wb') as fh:\n        fh.write(data)\n",
            [("f", "open"), ("f", "fh.write")],
        ),
        ("def f(a, b):\n    shutil.copyfile(a, b)\n", [("f", "shutil.copyfile")]),
        ("def f(a, b):\n    os.rename(a, b)\n", [("f", "os.rename")]),
        ("async def f(a, b):\n    a.write_text(b)\n", [("f", "a.write_text")]),
        ("def f(name):\n    return name.replace('/', '_')\n", []),
        ("def f(p):\n    return shutil.disk_usage(p).free\n", []),
        ("def f(p):\n    return p.read_bytes()\n", []),
    ],
    ids=[
        "write_bytes",
        "open-w",
        "copyfile",
        "rename",
        "write_text",
        "str-replace",
        "disk-usage",
        "read",
    ],
)
def test_the_shape_scan_sees_a_direct_write_and_lets_an_innocent_call_pass(
    source: str, found: list[tuple[str, str]]
) -> None:
    """Guilt and innocence for the scan above: the old way of writing a blob is caught, a string replace is not."""
    assert _shapes_of(source).writes == found
