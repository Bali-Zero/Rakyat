"""The Drive drain never reads a change and drops it (cursor discipline).

Before: drain_drive walked EVERY page of the changes feed, kept the first ``limit`` changes and
moved the cursor to the end of the feed, so everything past ``limit`` was lost (281 of 4883 live
ticks were saturated). Now a tick takes ONE bounded page (page_size=limit, max_pages=1), processes
all of it and stores the page's own continuation; a tick with a failing change holds the cursor,
with no tick limit for anything that can heal by waiting, and lets go only of files whose Drive
error is permanent and their own, after _MAX_HELD_TICKS failing ticks and only in a tick where
another change was downloaded and queued (a tick where nothing succeeds holds, however
permanent its errors look: a service account that lost its access costs no page).

GUILT: 450 changes on a 200-per-page feed are consumed 200 + 200 + 50 with nothing lost; a
failing change holds the cursor; a system fault never costs the page; a Drive error leaves the
cursor; an ancestor lookup that fails (5xx, 429, network) holds the cursor instead of calling the
file out of scope. INNOCENCE: only a permanent Drive error of its own file is ever let go; an
ancestor Drive reports as gone (404 notFound) is out of scope as before; below the limit, today's
counters and today's single cursor write; exactly ``limit`` is not saturated; today's cursor
shape is resumed. GATE r9165: a malformed cursor row never shortens the hold; a rate-limit
reason hidden behind a permanent one holds; a walk that cannot finish (128 levels, a cycle)
fails the change; no log line carries an exception's text, a traceback or a name, and an
exception that escapes drain_drive stops at the runner as a summary and exit 1.
PACE (budget PR): a tick walks pages, each with its own cursor write, until the feed ends, a page is
HELD (the walk stops there and never skips it), the page cap is reached, the time budget is spent
(checked between pages; the first page always runs) or the blob volume is below its free-space
floor (the tick downloads nothing, keeps the cursor and says so on its done line). One done line per
page, chained by from_token = the previous next_token; the runner reports the sum.
GATE r9178 round 1: a page is never started on a spent budget, whatever ran since the last check (the
disk probe, the done line), and the time before the first page counts; a floor below zero is the
default floor and one above zero never rounds down to off; every done line, the disk hold and the
budget stop included, carries its own counters and they add up to what the tick returns.
GATE r9178 round 2: the disk floor has no off switch and is an exact decimal, never float() (float("1e-324")
is 0.0): not a positive number is the default with a WARNING, a positive one is at least one byte, proved
for a seeded table of 600+ strings and for float, Decimal and bool arguments against an exact rational
oracle. The budget paces the walk, it is no wall-clock limit; a failed listing has no done line.
PURE unit, synthetic ids only (no file or folder names).
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import json
import logging
import math
import plistlib
import random
import re
import sys
import traceback
import types
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from backend.services.intake import drive_adapter as da
from backend.services.intake.enqueue import EnqueueResult

SCOPE = "SCOPE"
LIMIT = 200
RUNNER = Path(da.__file__).resolve().parents[5] / "scripts" / "drive_intake_drain.py"
PLIST = RUNNER.parents[1] / "infra" / "launchagents" / "com.balizero.drive-intake-drain.plist"
PACE_ENV = (
    "INTAKE_DRIVE_DRAIN_BUDGET_S",
    "INTAKE_DRIVE_DRAIN_MAX_PAGES",
    "INTAKE_DRIVE_MIN_FREE_GIB",
)
LEGACY_COUNTER_KEYS = {
    "changes",
    "enqueued_new",
    "already_present",
    "skipped_native",
    "skipped_removed",
    "skipped_out_of_scope",
    "errors",
}


class RunawayWalk(BaseException):
    """A walk that lists or probes the disk more than 100 times in one test is a loop."""


class CursorPool:
    """A system_settings stand-in that REMEMBERS the cursor between ticks, as the real row does."""

    def __init__(self, stored: str | None = None) -> None:
        self.value = stored
        self.writes: list[str] = []
        self.fail_write: Exception | None = None  # what a cursor write raises
        self.fail_after = 0  # ...once this many writes have gone through
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def acquire(self) -> Any:
        pool = self

        class _Conn:
            async def fetchval(self, query: str, *args: Any) -> Any:
                return pool.value

            async def execute(self, query: str, *args: Any) -> str:
                assert "system_settings" in query
                if pool.fail_write is not None and len(pool.writes) >= pool.fail_after:
                    raise pool.fail_write.with_traceback(None)
                pool.value = args[1]
                pool.writes.append(args[1])
                return "INSERT 0 1"

        class _Ctx:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()

    def stored(self) -> dict[str, Any]:
        return json.loads(self.value) if self.value else {}


class FeedDrive:
    """A changes feed served the way ServiceAccountDriveService.list_changes_since serves it.

    A token is an offset ("c200"). A page holds at most ``page_size`` changes; while more
    remain the result carries the NEXT page's token (``more_pages``), at the end the feed's
    start token. Called without ``max_pages`` it walks the whole feed.
    """

    START = "c0"
    END = "cEND"

    def __init__(self, changes: list[dict[str, Any]]) -> None:
        self.changes = changes
        self.calls: list[tuple[str, int, int | None]] = []
        self.fail_calls: set[int] = set()
        self.fail_exc: Exception = RuntimeError("drive unavailable")

    async def get_start_page_token(self) -> str:
        return self.START

    async def list_changes_since(
        self, page_token: str, *, max_pages: int | None = None, page_size: int = 100
    ) -> dict[str, Any]:
        self.calls.append((page_token, page_size, max_pages))
        if len(self.calls) > 100:
            raise RunawayWalk("more than 100 listings")
        if len(self.calls) in self.fail_calls:
            raise self.fail_exc.with_traceback(None)
        size = max(1, min(page_size, 1000))
        offset = len(self.changes) if page_token == self.END else int(page_token[1:])
        taken: list[dict[str, Any]] = []
        pages = 0
        while True:
            page = self.changes[offset : offset + size]
            taken.extend(page)
            offset += len(page)
            pages += 1
            if offset >= len(self.changes):
                return {"changes": taken, "new_page_token": self.END, "more_pages": False}
            if max_pages is not None and pages >= max_pages:
                return {"changes": taken, "new_page_token": f"c{offset}", "more_pages": True}


class Io:
    """Patches download + folder lookup + enqueue; a re-processed change reads as already_present."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fail: set[str] | None = None,
        download_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.seen: list[str] = []
        self.fail = fail or set()  # refs whose enqueue raises a plain (transient) error
        self.fail_all: Exception | None = None  # a system fault: every enqueue raises it
        self.fail_exc: Exception = RuntimeError("enqueue failed")  # what `fail` raises
        self.folder_calls: list[str] = []  # folder lookups, in order
        self.download_errors = download_errors or {}  # file ids whose download raises
        self.folders: dict[str, dict[str, Any]] = {}  # folder id -> {id, name, parents}
        self.folder_errors: dict[str, Exception] = {}  # folder ids whose lookup raises
        self.paths: dict[str, str] = {}  # source_ref -> source_path it was enqueued with
        monkeypatch.setattr(da, "_download_file", self._download)
        monkeypatch.setattr(da, "_folder_meta", self._folder_meta)
        monkeypatch.setattr(da, "enqueue", self._enqueue)

    async def _download(self, drive_service: Any, file_id: str, dest: Any) -> int:
        if file_id in self.download_errors:
            raise self.download_errors[file_id].with_traceback(None)
        return 1

    async def _folder_meta(self, drive_service: Any, folder_id: str) -> dict[str, Any]:
        self.folder_calls.append(folder_id)
        if folder_id in self.folder_errors:
            raise self.folder_errors[folder_id].with_traceback(None)
        return self.folders.get(folder_id) or {"id": folder_id, "name": "outside", "parents": []}

    async def _enqueue(self, pool: Any, **kwargs: Any) -> EnqueueResult:
        ref = kwargs["source_ref"]
        if self.fail_all is not None:
            raise self.fail_all.with_traceback(None)
        if ref in self.fail:
            raise self.fail_exc.with_traceback(None)
        self.paths[ref] = kwargs["source_path"]
        was_new = ref not in self.seen
        if was_new:
            self.seen.append(ref)
        return EnqueueResult(instance_id=1, queue_id=len(self.seen), was_new=was_new)


def _ref(i: int) -> str:
    return f"drive:f{i:04d}"


def _change(i: int, parents: list[str] | None = None, **file_extra: Any) -> dict[str, Any]:
    fid = f"f{i:04d}"
    file = {
        "id": fid,
        "name": f"n{i:04d}",
        "mimeType": "application/pdf",
        "parents": parents or [SCOPE],
    }
    file.update(file_extra)
    return {"fileId": fid, "removed": False, "file": file}


def _feed(total: int) -> FeedDrive:
    return FeedDrive([_change(i) for i in range(total)])


def _nested_feed(total: int, nested: set[int], parent: str = "F1") -> FeedDrive:
    """Direct children of the scope root, except `nested`, which sit under the folder `parent`."""
    return FeedDrive([_change(i, parents=[parent] if i in nested else None) for i in range(total)])


def _deep_chain(io: Io, levels: int, top: str = SCOPE) -> None:
    """D1 (d1) sits under D2 (d2) ... under D<levels>, which sits under `top`."""
    io.folders = {
        f"D{i}": {"id": f"D{i}", "name": f"d{i}", "parents": [f"D{i + 1}" if i < levels else top]}
        for i in range(1, levels + 1)
    }


def _chain(io: Io) -> None:
    """F1 (a1) sits under F2 (a2), which sits under the scope root."""
    io.folders = {
        "F1": {"id": "F1", "name": "a1", "parents": ["F2"]},
        "F2": {"id": "F2", "name": "a2", "parents": [SCOPE]},
    }


def _http_error(status: int, error: Any) -> HttpError:
    """A real googleapiclient HttpError whose body is {"error": <error>}."""
    return HttpError(httplib2.Response({"status": status}), json.dumps({"error": error}).encode())


def _drive_error(
    status: int, *reasons: str, quota: bool = False, message: str = "synthetic"
) -> HttpError:
    """An HttpError with Drive's error body: one `errors` entry per reason."""
    error: dict[str, Any] = {
        "code": status,
        "message": message,
        "errors": [{"domain": "global", "reason": reason} for reason in reasons],
    }
    if quota:
        error["status"] = "RESOURCE_EXHAUSTED"
    return _http_error(status, error)


async def _tick(pool: CursorPool, feed: FeedDrive, limit: int = LIMIT) -> dict[str, int]:
    return await da.drain_drive(pool, drive_service=feed, limit=limit, max_pages=1)


@pytest.fixture(autouse=True)
def _scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTAKE_DRIVE_SCOPE_FOLDER_ID", SCOPE)
    for name in PACE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(da, "_free_bytes", lambda: 10**15)  # the disk guard has its own tests


# --------------------------------------------------------------------------- #
# GUILT: nothing past `limit` is lost
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_450_changes_over_three_pages_are_consumed_200_200_50_and_none_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _feed(450), CursorPool()
    io = Io(monkeypatch)

    taken, cursors = [], []
    for _ in range(3):
        counters = await _tick(pool, feed)
        taken.append((counters["changes"], counters["enqueued_new"]))
        cursors.append(pool.stored()["token"])

    assert taken == [(200, 200), (200, 200), (50, 50)]
    assert cursors == [
        "c200",
        "c400",
        FeedDrive.END,
    ]  # page boundaries, then the feed's start token
    assert io.seen == [_ref(i) for i in range(450)]  # every change, once, in feed order
    # each tick asked for ONE page of `limit`, resuming exactly where the previous tick stopped
    assert feed.calls == [("c0", LIMIT, 1), ("c200", LIMIT, 1), ("c400", LIMIT, 1)]

    drained = await _tick(pool, feed)  # a fourth tick finds the feed empty and stays at its end
    assert drained["changes"] == 0
    assert pool.stored() == {"token": FeedDrive.END}


@pytest.mark.parametrize(
    ("limit", "total", "per_tick"),
    [
        (LIMIT, 199, [199]),
        (LIMIT, 200, [200]),  # exactly `limit` is a complete page, not a saturated one
        (LIMIT, 201, [200, 1]),
        (7, 20, [7, 7, 6]),  # INTAKE_DRIVE_DRAIN_LIMIT keeps meaning "changes per tick"
    ],
)
@pytest.mark.asyncio
async def test_the_limit_is_the_page_size_and_every_boundary_is_exact(
    monkeypatch: pytest.MonkeyPatch, limit: int, total: int, per_tick: list[int]
) -> None:
    feed, pool = _feed(total), CursorPool()
    io = Io(monkeypatch)

    seen_per_tick = []
    while len(seen_per_tick) < len(per_tick):
        seen_per_tick.append((await _tick(pool, feed, limit))["changes"])

    assert seen_per_tick == per_tick
    assert pool.stored() == {"token": FeedDrive.END}
    assert io.seen == [_ref(i) for i in range(total)]
    assert (await _tick(pool, feed, limit))["changes"] == 0


@pytest.mark.asyncio
async def test_the_done_line_shows_a_saturated_tick_and_the_next_one_resuming_from_its_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(450), CursorPool()
    Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        for _ in range(3):
            await _tick(pool, feed)

    done = [r.getMessage() for r in caplog.records if r.getMessage().startswith("drain_drive done")]
    assert len(done) == 3
    assert "'changes': 200" in done[0]
    assert done[0].endswith("from_token=c0 next_token=c200 more_pages=True gave_up=0")
    assert done[1].endswith("from_token=c200 next_token=c400 more_pages=True gave_up=0")  # resumes
    assert done[2].endswith("from_token=c400 next_token=cEND more_pages=False gave_up=0")


# --------------------------------------------------------------------------- #
# Errors: a failing change holds the cursor, a Drive error leaves it untouched
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_failing_change_mid_page_holds_the_cursor_and_the_retry_recovers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _feed(5), CursorPool()
    io = Io(monkeypatch, fail={_ref(2)})

    first = await _tick(pool, feed)
    assert (first["errors"], first["enqueued_new"]) == (1, 4)
    assert pool.stored() == {"token": FeedDrive.START, "held": 1}  # NOT moved to the feed's end

    io.fail.clear()  # the failure heals
    second = await _tick(pool, feed)
    assert (second["errors"], second["enqueued_new"], second["already_present"]) == (0, 1, 4)
    assert pool.stored() == {"token": FeedDrive.END}  # advanced, `held` cleared
    assert sorted(io.seen) == [_ref(i) for i in range(5)]  # the failed change was not lost


@pytest.mark.asyncio
async def test_a_file_with_a_permanent_drive_error_is_let_go_after_three_ticks_and_the_rest_flows(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(250), CursorPool()  # pages of 200 + 50; the flagged file is on page one
    io = Io(monkeypatch, download_errors={"f0002": _drive_error(403, "cannotDownloadAbusiveFile")})

    stored = []
    with caplog.at_level(logging.INFO, logger=da.logger.name):
        for _ in range(4):
            await _tick(pool, feed)
            stored.append(pool.stored())

    assert stored == [
        {"token": "c0", "held": 1},
        {"token": "c0", "held": 2},
        {"token": "c200"},  # third failing tick: that file alone is let go, the page advances
        {"token": FeedDrive.END},  # and the next page flows
    ]
    page_one = [ref for ref in io.seen if int(ref[-4:]) < LIMIT]
    assert len(page_one) == 199 and _ref(2) not in io.seen  # the other 199 of the page were queued
    assert sorted(io.seen) == [_ref(i) for i in range(250) if i != 2]
    gave_up = [r for r in caplog.records if "giving up" in r.getMessage()]
    assert [r.levelno for r in gave_up] == [logging.ERROR]  # said once, at ERROR
    assert (
        "1 file(s)" in gave_up[0].getMessage() and "file_ids=['f0002']" in gave_up[0].getMessage()
    )
    done = [r.getMessage() for r in caplog.records if r.getMessage().startswith("drain_drive done")]
    assert [m.rsplit(" ", 1)[1] for m in done] == [
        "gave_up=0",
        "gave_up=0",
        "gave_up=1",
        "gave_up=0",
    ]


@pytest.mark.asyncio
async def test_a_system_fault_never_costs_the_page_however_long_it_lasts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(5), CursorPool()
    io = Io(monkeypatch)
    io.fail_all = ConnectionError("db down")  # zero successes, only non-permanent errors

    with caplog.at_level(logging.WARNING, logger=da.logger.name):
        for tick in range(1, 6):
            counters = await _tick(pool, feed)
            assert (counters["enqueued_new"], counters["errors"]) == (0, 5)
            assert pool.stored() == {"token": FeedDrive.START, "held": tick}  # still on the page
    assert io.seen == [] and not any("giving up" in r.getMessage() for r in caplog.records)

    io.fail_all = None  # the database is back
    healed = await _tick(pool, feed)
    assert (healed["enqueued_new"], healed["errors"]) == (5, 0)
    assert pool.stored() == {"token": FeedDrive.END}
    assert io.seen == [_ref(i) for i in range(5)]  # nothing was lost


@pytest.mark.asyncio
async def test_a_permanent_error_alone_never_lets_go_of_a_page_that_still_has_a_transient_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _feed(5), CursorPool()
    io = Io(
        monkeypatch,
        fail={_ref(1), _ref(3)},  # plain errors that heal
        download_errors={"f0002": _drive_error(404, "notFound")},  # one that never will
    )

    for tick in range(1, 6):  # well past three ticks, a transient error is still on the page
        counters = await _tick(pool, feed)
        assert counters["errors"] == 3
        assert pool.stored() == {"token": FeedDrive.START, "held": tick}

    io.fail.clear()  # the transient fault heals: only the permanent one is left, and it is let go
    last = await _tick(pool, feed)
    assert last["errors"] == 1 and pool.stored() == {"token": FeedDrive.END}
    assert sorted(io.seen) == [_ref(i) for i in range(5) if i != 2]


@pytest.mark.parametrize(
    ("exc", "permanent"),
    [
        (_drive_error(403, "cannotDownloadAbusiveFile"), True),
        (_drive_error(403, "forbidden"), True),
        (_drive_error(404, "notFound"), True),
        (_drive_error(403, "fileNotDownloadable"), True),
        # innocence: whatever can heal by waiting, or is not about this file, is never permanent
        (_drive_error(403, "rateLimitExceeded"), False),
        (_drive_error(403, "userRateLimitExceeded"), False),
        (_drive_error(403, "downloadQuotaExceeded"), False),
        (_drive_error(429, "rateLimitExceeded"), False),
        (_drive_error(500, "backendError"), False),
        (_drive_error(503, "backendError"), False),
        (_drive_error(404, "somethingElse"), False),
        (_drive_error(500, "notFound"), False),  # the status matters too
        (_drive_error(429, "forbidden"), False),
        (HttpError(httplib2.Response({"status": 404}), b"<html>not json</html>"), False),
        (HttpError(httplib2.Response({"status": 403}), b""), False),
        (TimeoutError("timed out"), False),
        (ConnectionError("reset"), False),
        (OSError("disk full"), False),
        (RuntimeError("enqueue failed"), False),
        # gate r9165: EVERY reason counts, in any position, against the reasons of that status
        (_drive_error(403, "forbidden", "fileNotDownloadable"), True),
        (_drive_error(403, "forbidden", "forbidden"), True),
        (_drive_error(403, "forbidden", "rateLimitExceeded"), False),
        (_drive_error(403, "rateLimitExceeded", "forbidden"), False),
        (_drive_error(404, "notFound", "forbidden"), False),
        (_drive_error(404, "forbidden"), False),  # forbidden is permanent for 403 only
        (_drive_error(403, "notFound"), False),  # notFound is permanent for 404 only
        (_drive_error(403, "forbidden", quota=True), False),  # RESOURCE_EXHAUSTED
        (_http_error(403, {"code": 403, "status": "PERMISSION_DENIED"}), False),  # no reason
        (_http_error(403, {"code": 403, "errors": []}), False),
        (_http_error(403, {"code": 403, "errors": "forbidden"}), False),  # malformed
        (_http_error(403, {"code": 403, "errors": ["forbidden"]}), False),
        (_http_error(403, {"code": 403, "errors": [{"domain": "global"}]}), False),
        (_http_error(403, {"code": 403, "errors": [{"reason": 5}]}), False),
        (_http_error(403, "forbidden"), False),
        (
            _http_error(403, {"code": 403, "errors": [{"reason": "forbidden"}, {"reason": 5}]}),
            False,
        ),
        (_http_error(403, {"code": 403, "errors": [{"reason": "forbidden"}, "forbidden"]}), False),
    ],
)
def test_only_a_drive_error_of_its_own_file_is_permanent(exc: Exception, permanent: bool) -> None:
    assert da._is_permanent_file_error(exc) is permanent


@pytest.mark.asyncio
async def test_a_drive_error_while_listing_leaves_the_cursor_where_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _feed(450), CursorPool()
    feed.fail_calls = {2}
    io = Io(monkeypatch)

    await _tick(pool, feed)
    assert pool.stored() == {"token": "c200"}
    writes = len(pool.writes)

    failed = await _tick(pool, feed)  # Drive raises while listing: nothing processed, nothing moved
    assert set(failed) == LEGACY_COUNTER_KEYS and not any(failed.values())
    assert pool.stored() == {"token": "c200"} and len(pool.writes) == writes

    await _tick(pool, feed)  # healed: resumes at the same place
    assert pool.stored() == {"token": "c400"}
    assert io.seen == [_ref(i) for i in range(400)]


# --------------------------------------------------------------------------- #
# A tick where nothing succeeds never lets go, however permanent its errors look
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "error"),
    [
        pytest.param("download", _drive_error(403, "forbidden"), id="download-403-forbidden"),
        pytest.param("download", _drive_error(404, "notFound"), id="download-404-notFound"),
        pytest.param(
            "download", _drive_error(403, "cannotDownloadAbusiveFile"), id="download-403-abusive"
        ),
        pytest.param("ancestor", _drive_error(403, "forbidden"), id="ancestor-403-forbidden"),
    ],
)
@pytest.mark.asyncio
async def test_a_tick_where_nothing_succeeds_never_lets_go_however_permanent_its_errors_look(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, path: str, error: Exception
) -> None:
    """The service account loses its access: every file of the page answers 403 or 404."""
    nested = {0, 1, 2, 3, 4} if path == "ancestor" else set()
    feed, pool = _nested_feed(5, nested=nested), CursorPool()
    io = Io(monkeypatch)
    if path == "download":
        io.download_errors = {f"f{i:04d}": error for i in range(5)}
    else:
        io.folders = {"F1": {"id": "F1", "name": "a1", "parents": [SCOPE]}}
        io.folder_errors = {"F1": error}

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        for tick in range(1, 6):
            counters = await _tick(pool, feed)
            assert (counters["enqueued_new"], counters["already_present"]) == (0, 0)
            assert counters["errors"] == 5
            assert pool.stored() == {"token": FeedDrive.START, "held": tick}  # still on the page
    messages = [r.getMessage() for r in caplog.records]
    assert io.seen == [] and not any("giving up" in m for m in messages)
    assert any("(5 permanent), 0 succeeded" in m for m in messages)  # the log says why it holds
    assert all(m.endswith("gave_up=0") for m in messages if m.startswith("drain_drive done"))

    io.download_errors.clear()  # the access is back
    io.folder_errors.clear()
    healed = await _tick(pool, feed)
    assert (healed["enqueued_new"], healed["errors"]) == (5, 0)
    assert pool.stored() == {"token": FeedDrive.END}
    assert io.seen == [_ref(i) for i in range(5)]  # nothing was lost


@pytest.mark.asyncio
async def test_one_success_in_the_tick_is_enough_and_a_relisted_change_counts_as_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(2), CursorPool()  # f0000 downloads, f0001 never will
    io = Io(monkeypatch, download_errors={"f0001": _drive_error(404, "notFound")})

    stored = []
    with caplog.at_level(logging.ERROR, logger=da.logger.name):
        for _ in range(3):
            counters = await _tick(pool, feed)
            stored.append(pool.stored())

    assert stored == [
        {"token": "c0", "held": 1},
        {"token": "c0", "held": 2},
        {"token": FeedDrive.END},  # third failing tick, one success: that file alone is let go
    ]
    # the success of the last tick is the RE-LISTED change: already_present, not a new enqueue
    assert (counters["enqueued_new"], counters["already_present"], counters["errors"]) == (0, 1, 1)
    gave_up = [r.getMessage() for r in caplog.records if "giving up" in r.getMessage()]
    assert len(gave_up) == 1 and "file_ids=['f0001']" in gave_up[0]
    assert io.seen == [_ref(0)]


@pytest.mark.asyncio
async def test_the_first_success_after_a_long_hold_lets_the_permanent_file_go_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(5), CursorPool()
    io = Io(
        monkeypatch, download_errors={f"f{i:04d}": _drive_error(403, "forbidden") for i in range(5)}
    )

    for tick in range(1, 5):  # four ticks with nothing succeeding: held, never let go
        await _tick(pool, feed)
        assert pool.stored() == {"token": FeedDrive.START, "held": tick}

    for i in (0, 1, 3, 4):  # the access comes back for every file but one
        del io.download_errors[f"f{i:04d}"]
    with caplog.at_level(logging.ERROR, logger=da.logger.name):
        last = await _tick(pool, feed)

    assert (last["enqueued_new"], last["errors"]) == (4, 1)  # a NEW enqueue is a success too
    assert pool.stored() == {"token": FeedDrive.END}  # the four failing ticks counted: no wait
    gave_up = [r.getMessage() for r in caplog.records if "giving up" in r.getMessage()]
    assert len(gave_up) == 1 and "file_ids=['f0002']" in gave_up[0]
    assert io.seen == [_ref(i) for i in (0, 1, 3, 4)]


@pytest.mark.asyncio
async def test_a_skipped_change_is_not_a_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    changes = (
        [_change(0)]  # the file that never downloads
        + [_change(1 + i, mimeType="application/vnd.google-apps.document") for i in range(3)]
        + [{"fileId": "r0", "removed": True}]
        + [_change(9, parents=["OUTSIDE"])]
    )
    feed, pool = FeedDrive(changes), CursorPool()
    io = Io(monkeypatch, download_errors={"f0000": _drive_error(404, "notFound")})

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        for tick in range(1, 6):
            counters = await _tick(pool, feed)
            skipped = (
                counters["skipped_native"],
                counters["skipped_removed"],
                counters["skipped_out_of_scope"],
            )
            assert (skipped, counters["errors"]) == ((3, 1, 1), 1)  # five skips, nothing queued
            assert pool.stored() == {"token": FeedDrive.START, "held": tick}
    assert io.seen == [] and not any("giving up" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Scope check: a FAILED ancestor lookup is a failed change, never "out of scope"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(_drive_error(500, "backendError"), id="500"),
        pytest.param(_drive_error(503, "backendError"), id="503"),
        pytest.param(_drive_error(429, "rateLimitExceeded"), id="429"),
        pytest.param(_drive_error(403, "userRateLimitExceeded"), id="403-rate-limit"),
        pytest.param(ConnectionError("reset"), id="connection"),
        pytest.param(TimeoutError("timed out"), id="timeout"),
        # a 404 that is not Drive saying "this folder does not exist" is not an answer either
        pytest.param(
            HttpError(httplib2.Response({"status": 404}), b"<html>gateway</html>"), id="404-html"
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_ancestor_lookup_that_fails_holds_the_cursor_and_the_retry_recovers_the_file(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    feed, pool = _nested_feed(5, nested={2}), CursorPool()
    io = Io(monkeypatch)
    _chain(io)
    io.folder_errors = {"F1": exc}

    first = await _tick(pool, feed)
    assert (first["errors"], first["skipped_out_of_scope"], first["enqueued_new"]) == (1, 0, 4)
    assert pool.stored() == {"token": FeedDrive.START, "held": 1}  # NOT moved: the file is not lost
    assert _ref(2) not in io.seen

    io.folder_errors.clear()  # the lookup heals
    second = await _tick(pool, feed)
    assert (second["errors"], second["enqueued_new"], second["already_present"]) == (0, 1, 4)
    assert pool.stored() == {"token": FeedDrive.END}
    assert sorted(io.seen) == [_ref(i) for i in range(5)]
    assert io.paths[_ref(2)] == "a2/a1/n0002"  # resolved through both hops once Drive answered


@pytest.mark.asyncio
async def test_a_failure_further_up_the_chain_holds_the_cursor_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _nested_feed(5, nested={2}), CursorPool()
    io = Io(monkeypatch)
    _chain(io)
    io.folder_errors = {"F2": _drive_error(503, "backendError")}  # F1 answers, F2 does not

    first = await _tick(pool, feed)
    assert (first["errors"], first["skipped_out_of_scope"], first["enqueued_new"]) == (1, 0, 4)
    assert pool.stored() == {"token": FeedDrive.START, "held": 1}

    io.folder_errors.clear()
    await _tick(pool, feed)
    assert pool.stored() == {"token": FeedDrive.END} and io.paths[_ref(2)] == "a2/a1/n0002"


@pytest.mark.parametrize(
    "outage",
    [
        pytest.param(_drive_error(503, "backendError"), id="503"),
        pytest.param(_drive_error(429, "rateLimitExceeded"), id="429"),
    ],
)
@pytest.mark.asyncio
async def test_an_ancestor_outage_never_costs_the_page_however_long_it_lasts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, outage: Exception
) -> None:
    feed, pool = _nested_feed(5, nested={1, 3}), CursorPool()  # three flow, two sit under F1
    io = Io(monkeypatch)
    io.folders = {"F1": {"id": "F1", "name": "a1", "parents": [SCOPE]}}
    io.folder_errors = {"F1": outage}  # a non-permanent error, while the rest of the page succeeds

    with caplog.at_level(logging.WARNING, logger=da.logger.name):
        for tick in range(1, 6):
            counters = await _tick(pool, feed)
            assert (counters["errors"], counters["skipped_out_of_scope"]) == (2, 0)
            assert counters["enqueued_new"] + counters["already_present"] == 3  # others flow
            assert pool.stored() == {"token": FeedDrive.START, "held": tick}  # still on the page
    assert io.seen == [_ref(0), _ref(2), _ref(4)]
    assert not any("giving up" in r.getMessage() for r in caplog.records)

    io.folder_errors.clear()  # Drive is back
    healed = await _tick(pool, feed)
    assert (healed["enqueued_new"], healed["already_present"], healed["errors"]) == (2, 3, 0)
    assert pool.stored() == {"token": FeedDrive.END}
    assert sorted(io.seen) == [_ref(i) for i in range(5)]
    assert [io.paths[_ref(i)] for i in (1, 3)] == ["a1/n0001", "a1/n0003"]


@pytest.mark.asyncio
async def test_a_forbidden_ancestor_is_a_permanent_error_of_its_file_and_is_let_go_like_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _nested_feed(5, nested={2}), CursorPool()
    io = Io(monkeypatch)
    io.folder_errors = {"F1": _drive_error(403, "forbidden")}

    stored = []
    with caplog.at_level(logging.INFO, logger=da.logger.name):
        for _ in range(3):
            await _tick(pool, feed)
            stored.append(pool.stored())

    assert stored == [{"token": "c0", "held": 1}, {"token": "c0", "held": 2}, {"token": "cEND"}]
    assert sorted(io.seen) == [_ref(i) for i in range(5) if i != 2]  # the other four were queued
    gave_up = [r.getMessage() for r in caplog.records if "giving up" in r.getMessage()]
    assert len(gave_up) == 1 and "file_ids=['f0002']" in gave_up[0]


@pytest.mark.parametrize("gone", ["F1", "F2"])
@pytest.mark.asyncio
async def test_an_ancestor_drive_reports_as_gone_is_out_of_scope_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    feed, pool = _nested_feed(5, nested={2}), CursorPool()
    io = Io(monkeypatch)
    _chain(io)
    io.folder_errors = {gone: _drive_error(404, "notFound")}

    counters = await _tick(pool, feed)

    assert counters == {
        "changes": 5,
        "enqueued_new": 4,
        "already_present": 0,
        "skipped_native": 0,
        "skipped_removed": 0,
        "skipped_out_of_scope": 1,
        "errors": 0,
    }
    assert pool.writes == ['{"token": "cEND"}']  # nothing held, today's single write
    assert _ref(2) not in io.seen


@pytest.mark.parametrize(
    ("exc", "gone"),
    [
        (_drive_error(404, "notFound"), True),
        # innocence: only Drive's own "this folder does not exist" makes a file unreachable
        (_drive_error(404, "somethingElse"), False),
        (_drive_error(403, "notFound"), False),  # the status matters too
        (_drive_error(403, "forbidden"), False),
        (_drive_error(500, "backendError"), False),
        (_drive_error(503, "backendError"), False),
        (_drive_error(429, "rateLimitExceeded"), False),
        (HttpError(httplib2.Response({"status": 404}), b"<html>gateway</html>"), False),
        (HttpError(httplib2.Response({"status": 404}), b""), False),
        (FileNotFoundError("F1"), False),
        (TimeoutError("timed out"), False),
        (ConnectionError("reset"), False),
        (RuntimeError("boom"), False),
        (_drive_error(404, "notFound", "notFound"), True),
        (_drive_error(404, "notFound", "rateLimitExceeded"), False),  # gate r9165
        (_drive_error(404, "rateLimitExceeded", "notFound"), False),
        (_drive_error(404, "notFound", quota=True), False),
        (_http_error(404, {"code": 404, "errors": []}), False),
    ],
)
def test_only_drives_404_not_found_makes_an_ancestor_unreachable(
    exc: Exception, gone: bool
) -> None:
    assert da._is_ancestor_gone(exc) is gone


# --------------------------------------------------------------------------- #
# GATE r9165 (1): a malformed cursor row never shortens the hold
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stored",
    [
        '{"token": "c0", "held": true}',
        '{"token": "c0", "held": "3"}',
        '{"token": "c0", "held": -1}',
        '{"token": "c0", "held": 2.0}',
        '{"token": null, "held": 99}',
        '{"held": 5}',
        '{"token": "", "held": 7}',
        '{"token": 5, "held": 7}',
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_cursor_row_never_shortens_the_hold(
    monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    feed, pool = _feed(2), CursorPool(stored)  # f0000 downloads, f0001 never will
    Io(monkeypatch, download_errors={"f0001": _drive_error(404, "notFound")})

    seen = []
    for _ in range(3):
        await _tick(pool, feed)
        seen.append(pool.stored())

    assert feed.calls[0][0] == FeedDrive.START  # no usable token: reseeded, with no hold count
    assert seen == [{"token": "c0", "held": 1}, {"token": "c0", "held": 2}, {"token": "cEND"}]


@pytest.mark.parametrize(
    ("held", "first"), [(2, {"token": "cEND"}), (0, {"token": "c0", "held": 1})]
)
@pytest.mark.asyncio
async def test_a_valid_hold_count_is_honoured(
    monkeypatch: pytest.MonkeyPatch, held: int, first: dict[str, Any]
) -> None:
    feed, pool = _feed(2), CursorPool(json.dumps({"token": "c0", "held": held}))
    Io(monkeypatch, download_errors={"f0001": _drive_error(404, "notFound")})

    await _tick(pool, feed)

    assert pool.stored() == first  # held 2 + this failing tick = 3: let go at once


# --------------------------------------------------------------------------- #
# GATE r9165 (2): a transient reason hidden behind a permanent one is transient
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["download", "ancestor"])
@pytest.mark.parametrize(
    "reasons", [("forbidden", "rateLimitExceeded"), ("rateLimitExceeded", "forbidden")]
)
@pytest.mark.asyncio
async def test_a_transient_reason_hidden_behind_a_permanent_one_holds_the_page_for_ever(
    monkeypatch: pytest.MonkeyPatch, path: str, reasons: tuple[str, str]
) -> None:
    feed, pool = _nested_feed(3, nested={2} if path == "ancestor" else set()), CursorPool()
    io = Io(monkeypatch)
    error = _drive_error(403, *reasons)
    if path == "download":
        io.download_errors = {"f0002": error}
    else:
        io.folders = {"F1": {"id": "F1", "name": "a1", "parents": [SCOPE]}}
        io.folder_errors = {"F1": error}

    for tick in range(1, 7):  # far past the third failing tick, with two other changes succeeding
        counters = await _tick(pool, feed)
        assert counters["errors"] == 1 and pool.stored() == {"token": "c0", "held": tick}
    assert sorted(io.seen) == [_ref(0), _ref(1)]

    io.download_errors.clear()
    io.folder_errors.clear()
    await _tick(pool, feed)
    assert pool.stored() == {"token": "cEND"} and _ref(2) in io.seen


# --------------------------------------------------------------------------- #
# GATE r9165 (3): reaching the depth cap, or a cycle, fails the change
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("levels", [12, 128])
@pytest.mark.asyncio
async def test_a_valid_deep_chain_is_in_scope_with_its_relative_path(
    monkeypatch: pytest.MonkeyPatch, levels: int
) -> None:
    feed, pool = _nested_feed(3, nested={1}, parent="D1"), CursorPool()
    io = Io(monkeypatch)
    _deep_chain(io, levels)

    counters = await _tick(pool, feed)

    assert (counters["skipped_out_of_scope"], counters["errors"], counters["enqueued_new"]) == (
        0,
        0,
        3,
    )
    assert io.paths[_ref(1)] == "/".join(f"d{i}" for i in range(levels, 0, -1)) + "/n0001"
    assert pool.stored() == {"token": "cEND"}


@pytest.mark.parametrize(("kind", "said"), [("too-deep", "depth=128"), ("cycle", "depth=2 cycle")])
@pytest.mark.asyncio
async def test_a_walk_that_cannot_finish_fails_the_change_instead_of_dropping_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, kind: str, said: str
) -> None:
    feed, pool = _nested_feed(3, nested={1}, parent="D1"), CursorPool()
    io = Io(monkeypatch)
    if kind == "too-deep":
        _deep_chain(io, 129)  # one level more than the cap
    else:
        io.folders = {
            "D1": {"id": "D1", "name": "d1", "parents": ["D2"]},
            "D2": {"id": "D2", "name": "d2", "parents": ["D1"]},
        }

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        for tick in range(1, 5):
            counters = await _tick(pool, feed)
            assert (counters["errors"], counters["skipped_out_of_scope"]) == (1, 0)
            assert pool.stored() == {"token": "c0", "held": tick}  # held, however long it lasts
    walk = [r.getMessage() for r in caplog.records if "ancestor walk failed" in r.getMessage()]
    assert len(walk) == 4  # the log says the file id and the depth, nothing else
    assert all(
        re.fullmatch(
            rf"drain_drive: ancestor walk failed for file_id=f0001 \(_AncestorChainError {said} "
            r"at \w+\.py:\d+\)",
            m,
        )
        for m in walk
    )
    assert not any("giving up" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_chain_that_ends_at_the_root_without_the_scope_is_out_of_scope_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _nested_feed(3, nested={1}, parent="D1"), CursorPool()
    io = Io(monkeypatch)
    _deep_chain(io, 20, top="ROOT")  # ROOT has no parents and is not the scope

    counters = await _tick(pool, feed)

    assert (counters["skipped_out_of_scope"], counters["errors"], counters["enqueued_new"]) == (
        1,
        0,
        2,
    )
    assert pool.writes == ['{"token": "cEND"}']  # nothing held, today's single write


# --------------------------------------------------------------------------- #
# GATE r9165 (4): no log line carries an exception's text, a traceback or a name
# --------------------------------------------------------------------------- #
NAME = "ZZ-synthetic-name"  # stands for a client file or folder name


@pytest.mark.parametrize(
    ("where", "said"),
    [
        ("download-http", "file_id=f0001 (HttpError status=500 reason=backendError at "),
        ("download-fs", "file_id=f0001 (PermissionError at "),
        ("enqueue", "file_id=f0001 (RuntimeError at "),
        ("ancestor", "ancestor walk failed for file_id=f0001 (ConnectionError at "),
        (
            "ancestor-gone",
            "ancestor F1 no longer exists, out of scope (HttpError status=404 reason=notFound ",
        ),
        ("enumeration", "Drive enumeration failed, skipping (RuntimeError at "),
        ("service", "Drive service unavailable, skipping (RuntimeError at "),
        ("odd-reason", "file_id=f0001 (HttpError status=403 reason=other at "),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_log_names_the_failure_but_never_a_file_or_a_folder(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, where: str, said: str
) -> None:
    feed, pool = (
        _nested_feed(3, nested={1} if where.startswith("ancestor") else set()),
        CursorPool(),
    )
    io = Io(monkeypatch)
    named = f"cannot read {NAME}"
    if where == "download-http":
        io.download_errors = {"f0001": _drive_error(500, "backendError", message=named)}
    elif where == "download-fs":
        io.download_errors = {
            "f0001": PermissionError(13, "Permission denied", f"/blobs/{NAME}.pdf")
        }
    elif where == "enqueue":
        io.fail, io.fail_exc = {_ref(1)}, RuntimeError(named)
    elif where == "ancestor":
        io.folder_errors = {"F1": ConnectionError(named)}
    elif where == "ancestor-gone":
        io.folder_errors = {"F1": _drive_error(404, "notFound", message=named)}
    elif where == "enumeration":
        feed.fail_calls, feed.fail_exc = {1}, RuntimeError(named)
    elif where == "odd-reason":
        io.download_errors = {"f0001": _drive_error(403, NAME, message=named)}

    with caplog.at_level(logging.DEBUG, logger=da.logger.name):
        if where == "service":

            class _Boom:
                def __init__(self) -> None:
                    raise RuntimeError(named)

            monkeypatch.setattr(
                "backend.services.integrations.service_account_drive_service.ServiceAccountDriveService",
                _Boom,
            )
            await da.drain_drive(pool, drive_service=None)
        else:
            await _tick(pool, feed)

    messages = [r.getMessage() for r in caplog.records]
    assert any(said in m for m in messages), messages
    assert NAME not in caplog.text and not any(NAME in m for m in messages)
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)  # no traceback


def _log_violations(source: str) -> list[str]:
    """Logger calls that could carry an exception's text or a traceback."""
    tree = ast.parse(source)
    caught = {n.name for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "traceback" and node.attr != "extract_tb":
                bad.append(f"line {node.lineno}: traceback.{node.attr}")
        is_log = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        )
        if not is_log:
            continue
        if node.func.attr == "exception":
            bad.append(f"line {node.lineno}: logger.exception")
        if any(k.arg in ("exc_info", "stack_info") for k in node.keywords):
            bad.append(f"line {node.lineno}: exc_info or stack_info")
        used = {
            n.id
            for arg in (*node.args, *(k.value for k in node.keywords))
            for n in ast.walk(arg)
            if isinstance(n, ast.Name)
        }
        if used & caught:
            bad.append(f"line {node.lineno}: logs {sorted(used & caught)}")
    return bad


@pytest.mark.parametrize("module", [Path(da.__file__), RUNNER], ids=["adapter", "runner"])
def test_the_module_never_logs_an_exception_or_a_traceback(module: Path) -> None:
    if not (module.exists()):
        pytest.skip('scripts/drive_intake_drain.py not in Rakyat')
    assert _log_violations(module.read_text()) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "try:\n    pass\nexcept Exception as exc:\n    logger.exception('x')\n",
        "try:\n    pass\nexcept Exception as exc:\n    logger.warning('x %s', exc)\n",
        "try:\n    pass\nexcept Exception as exc:\n    logger.warning(f'x {exc}')\n",
        "try:\n    pass\nexcept Exception as e:\n    logger.error('x %s', str(e))\n",
        "try:\n    pass\nexcept Exception:\n    logger.error('x', exc_info=True)\n",
        "import traceback\ntraceback.format_exc()\n",
    ],
)
def test_the_log_checker_catches_every_shape_it_forbids(snippet: str) -> None:
    assert _log_violations(snippet)


def test_the_log_checker_lets_a_summary_through() -> None:
    ok = "try:\n    pass\nexcept Exception as exc:\n    why = summary(exc)\n    logger.warning('x %s', why)\n"
    assert _log_violations(ok + "import traceback\ntraceback.extract_tb(None)\n") == []


# --------------------------------------------------------------------------- #
# GATE r9165 (residual b): what escapes drain_drive stops at the runner, as a summary
# --------------------------------------------------------------------------- #
ESCAPES = {  # a fault past drain_drive's own try blocks -> (what it raises, what the runner logs)
    "cursor-write": (RuntimeError, "drain tick failed (RuntimeError at "),
    "blob-path": (PermissionError, "drain tick failed (PermissionError at "),
    "drive-error": (HttpError, "drain tick failed (HttpError status=403 reason=forbidden at "),
}


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """scripts/drive_intake_drain.py as a module, its state file in tmp."""
    if not (RUNNER.exists()):
        pytest.skip('scripts/drive_intake_drain.py not in Rakyat')
    spec = importlib.util.spec_from_file_location("drive_intake_drain_under_test", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "path", list(sys.path))  # the runner prepends the backend dir
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE_FILE", tmp_path / "state.json")
    return module


def _wire(
    monkeypatch: pytest.MonkeyPatch, runner: Any, pool: CursorPool, feed: FeedDrive | None = None
) -> None:
    """The runner gets `pool` for its database and `feed` for Drive."""

    async def _create_pool(**kwargs: Any) -> CursorPool:
        return pool

    monkeypatch.setattr(runner.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(
        "backend.services.integrations.service_account_drive_service.ServiceAccountDriveService",
        lambda: feed,
    )


def _escaping_fault(monkeypatch: pytest.MonkeyPatch, pool: CursorPool, where: str) -> None:
    """Arm a fault past drain_drive's try blocks (cursor write, blob path) whose text names a file."""
    named = f"cannot touch {NAME}"
    if where == "cursor-write":
        pool.fail_write = RuntimeError(named)
        return
    fault = (
        PermissionError(13, "Permission denied", f"/blobs/{NAME}.pdf")
        if where == "blob-path"
        else _drive_error(403, "forbidden", message=named)
    )

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise fault

    monkeypatch.setattr(da, "_local_blob_path", _raise)


@pytest.mark.parametrize("where", list(ESCAPES))
@pytest.mark.asyncio
async def test_without_the_boundary_the_name_reaches_the_interpreters_traceback(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    pool = CursorPool()
    Io(monkeypatch)
    _escaping_fault(monkeypatch, pool, where)

    with pytest.raises(ESCAPES[where][0]) as escaped:
        await _tick(pool, _feed(3))

    assert NAME in "".join(traceback.format_exception(escaped.value))


@pytest.mark.parametrize("where", list(ESCAPES))
@pytest.mark.asyncio
async def test_an_exception_that_escapes_the_drain_is_logged_as_a_summary_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    runner: Any,
    where: str,
) -> None:
    pool = CursorPool('{"token": "c0"}')
    Io(monkeypatch)
    _escaping_fault(monkeypatch, pool, where)
    _wire(monkeypatch, runner, pool, _feed(3))

    with caplog.at_level(logging.DEBUG):
        code = await runner.main()

    shown = capsys.readouterr()
    own = [r for r in caplog.records if r.name == "drive_intake_drain"]
    said = ESCAPES[where][1]
    assert code == 1
    assert [(r.levelno, said in r.getMessage()) for r in own] == [(logging.ERROR, True)]
    assert NAME not in caplog.text and NAME not in shown.out + shown.err
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)
    assert pool.writes == [] and pool.stored() == {"token": "c0"}  # the cursor did not move
    assert pool.closed
    assert not runner.STATE_FILE.exists()  # a failed tick never claims "ok" to the sentinel


@pytest.mark.parametrize("stop", [KeyboardInterrupt(), SystemExit(3)], ids=["interrupt", "exit"])
@pytest.mark.asyncio
async def test_an_interrupt_or_an_exit_passes_through_the_boundary_unchanged(
    monkeypatch: pytest.MonkeyPatch, runner: Any, stop: BaseException
) -> None:
    pool = CursorPool()

    async def _drain(*args: Any, **kwargs: Any) -> dict[str, int]:
        raise stop

    monkeypatch.setattr(runner, "drain_drive", _drain)
    _wire(monkeypatch, runner, pool)

    with pytest.raises(type(stop)) as raised:
        await runner.main()

    assert raised.value is stop and pool.closed
    assert not runner.STATE_FILE.exists()


@pytest.mark.asyncio
async def test_a_drain_that_succeeds_keeps_its_exit_state_and_done_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, runner: Any
) -> None:
    pool = CursorPool()
    Io(monkeypatch)
    _wire(monkeypatch, runner, pool, _feed(3))

    with caplog.at_level(logging.INFO, logger="drive_intake_drain"):
        code = await runner.main()

    state = json.loads(runner.STATE_FILE.read_text())
    own = [r.getMessage() for r in caplog.records if r.name == "drive_intake_drain"]
    assert code == 0 and pool.closed
    assert state["status"] == "ok" and state["enqueued_new"] == 3
    assert set(state) == {"job", "ts", "status", "elapsed", *LEGACY_COUNTER_KEYS}
    assert own == [f"drain tick done: {state}"]  # no failure line, the done line as before


# --------------------------------------------------------------------------- #
# INNOCENCE: below the limit, today's behaviour; today's cursor shape is honoured
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_feed_below_the_limit_gives_todays_counters_and_one_cursor_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changes = (
        [_change(i) for i in range(100)]  # in scope
        + [_change(100 + i, mimeType="application/vnd.google-apps.folder") for i in range(20)]
        + [{"fileId": f"r{i}", "removed": True} for i in range(15)]
        + [_change(200 + i, trashed=True) for i in range(5)]
        + [_change(300 + i, parents=["OUTSIDE"]) for i in range(10)]
    )
    feed, pool = FeedDrive(changes), CursorPool()
    Io(monkeypatch)

    counters = await _tick(pool, feed)

    assert counters == {
        "changes": 150,
        "enqueued_new": 100,
        "already_present": 0,
        "skipped_native": 20,
        "skipped_removed": 20,
        "skipped_out_of_scope": 10,
        "errors": 0,
    }
    assert set(counters) == LEGACY_COUNTER_KEYS  # no key added, none renamed
    assert pool.writes == ['{"token": "cEND"}']  # today's single write, byte for byte


@pytest.mark.parametrize("stored", ['{"token": "c200"}', '{"token": "c200", "held": "x"}'])
@pytest.mark.asyncio
async def test_a_stored_cursor_is_resumed_and_a_garbled_held_never_costs_the_token(
    monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    feed, pool = _feed(450), CursorPool(stored)
    io = Io(monkeypatch)

    counters = await _tick(pool, feed)

    assert feed.calls == [("c200", LIMIT, 1)]
    assert counters["changes"] == 200
    assert pool.stored() == {"token": "c400"}
    assert io.seen[0] == _ref(200)


# --------------------------------------------------------------------------- #
# PACE: a tick walks pages under a budget, a page cap and a disk floor
# --------------------------------------------------------------------------- #
GIB = 2**30
REAL_FREE_BYTES = da._free_bytes  # the autouse fixture replaces it in every test


class Clock:
    """The adapter's monotonic clock, driven by the test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TimedFeed(FeedDrive):
    """A feed whose every page costs `page_cost` seconds on `clock`."""

    def __init__(self, changes: list[dict[str, Any]], clock: Clock, page_cost: float) -> None:
        super().__init__(changes)
        self.clock, self.page_cost = clock, page_cost

    async def list_changes_since(
        self, page_token: str, *, max_pages: int | None = None, page_size: int = 100
    ) -> dict[str, Any]:
        self.clock.now += self.page_cost
        return await super().list_changes_since(
            page_token, max_pages=max_pages, page_size=page_size
        )


class NoTokenFeed(FeedDrive):
    """A feed that says more pages remain but gives no continuation."""

    async def list_changes_since(
        self, page_token: str, *, max_pages: int | None = None, page_size: int = 100
    ) -> dict[str, Any]:
        page = await super().list_changes_since(
            page_token, max_pages=max_pages, page_size=page_size
        )
        return {**page, "new_page_token": None, "more_pages": True}


class TimedIo(Io):
    """Io where every enqueue costs `change_cost` seconds on `clock`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, clock: Clock, change_cost: float) -> None:
        super().__init__(monkeypatch)
        self.clock, self.change_cost = clock, change_cost

    async def _enqueue(self, pool: Any, **kwargs: Any) -> EnqueueResult:
        self.clock.now += self.change_cost
        return await super()._enqueue(pool, **kwargs)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(da, "_clock", fake)
    return fake


async def _walk(
    pool: CursorPool, feed: FeedDrive, limit: int = LIMIT, **settings: Any
) -> dict[str, int]:
    return await da.drain_drive(pool, drive_service=feed, limit=limit, **settings)


def _done(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("drain_drive done")]


def _field(line: str, name: str) -> str:
    found = re.search(rf"\b{name}=(\S+)", line)
    assert found, (name, line)
    return found.group(1)


def _free_space(monkeypatch: pytest.MonkeyPatch, *values: int | Exception) -> list[int]:
    """The blob volume's free bytes: each probe returns (or raises) the next value, the last repeats."""
    probes: list[int] = []

    def _probe() -> int:
        probes.append(len(probes))
        if len(probes) > 100:
            raise RunawayWalk("more than 100 disk probes")
        value = values[min(len(probes) - 1, len(values) - 1)]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(da, "_free_bytes", _probe)
    return probes


def _slow_disk(monkeypatch: pytest.MonkeyPatch, clock: Clock, *costs: float) -> list[int]:
    """A disk probe that finds room and costs `costs[i]` seconds on `clock` (the last cost repeats)."""
    probes: list[int] = []

    def _probe() -> int:
        probes.append(len(probes))
        if len(probes) > 100:
            raise RunawayWalk("more than 100 disk probes")
        clock.now += costs[min(len(probes) - 1, len(costs) - 1)]
        return 10**15

    monkeypatch.setattr(da, "_free_bytes", _probe)
    return probes


def _spend_between_pages(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, where: str, cost: float
) -> None:
    """Make the work that sits between two pages cost `cost` seconds: the second disk probe (the
    first page's own probe is free), or the first done line."""
    if where == "probe":
        _slow_disk(monkeypatch, clock, 0, cost)
        return
    real = da._log_done

    def _slow_log(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        clock.now += cost

    monkeypatch.setattr(da, "_log_done", _slow_log)


def _summed(caplog: pytest.LogCaptureFixture) -> dict[str, int]:
    """The counters of every done line added up: what the tick has to return."""
    total = dict.fromkeys(LEGACY_COUNTER_KEYS, 0)
    for line in _done(caplog):
        found = re.search(r"done: (\{.*?\}) page=", line)
        assert found, line
        for key, value in ast.literal_eval(found.group(1)).items():
            total[key] += value
    return total


@pytest.mark.asyncio
async def test_a_tick_walks_every_page_it_has_room_for_and_stores_the_cursor_after_each(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(450), CursorPool()
    io = Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed)

    done = _done(caplog)
    assert (counters["changes"], counters["enqueued_new"], counters["errors"]) == (450, 450, 0)
    assert set(counters) == LEGACY_COUNTER_KEYS  # the same seven keys, summed over the pages
    assert pool.writes == ['{"token": "c200"}', '{"token": "c400"}', '{"token": "cEND"}']
    assert feed.calls == [("c0", LIMIT, 1), ("c200", LIMIT, 1), ("c400", LIMIT, 1)]
    assert io.seen == [_ref(i) for i in range(450)]
    assert [_field(m, "page") for m in done] == ["1/20", "2/20", "3/20"]
    assert [_field(m, "stop") for m in done] == ["continue", "continue", "end"]
    assert "'changes': 200" in done[0] and "'changes': 50" in done[2]  # a line counts its own page
    for before, after in itertools.pairwise(
        done
    ):  # the chain: a page starts where the last one ended
        assert _field(after, "from_token") == _field(before, "next_token")


@pytest.mark.parametrize(
    ("budget", "pages"), [(1, 1), (100, 1), (101, 2), (250, 3), (300, 3), (301, 4)]
)
@pytest.mark.asyncio
async def test_the_budget_is_spent_between_pages_and_never_inside_one(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, budget: int, pages: int
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(1000)], clock, 100), CursorPool()
    Io(monkeypatch)

    counters = await _walk(pool, feed, budget_s=budget)

    assert (len(feed.calls), counters["changes"]) == (pages, 200 * pages)  # 100 s a page
    assert pool.stored() == {"token": f"c{200 * pages}"}


@pytest.mark.parametrize("budget", [0, -5])
@pytest.mark.asyncio
async def test_a_tick_always_takes_its_first_page_whatever_the_budget(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, caplog: pytest.LogCaptureFixture, budget: int
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(450)], clock, 100), CursorPool()
    Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, budget_s=budget)

    assert (len(feed.calls), counters["changes"]) == (1, 200)
    assert _field(_done(caplog)[0], "stop") == "budget"


@pytest.mark.asyncio
async def test_a_page_in_progress_is_never_cut_by_the_budget(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    feed, pool = _feed(450), CursorPool()
    TimedIo(monkeypatch, clock, 10)  # 200 changes cost 2000 s, against a 5 s budget

    counters = await _walk(pool, feed, budget_s=5)

    assert (counters["changes"], counters["enqueued_new"]) == (200, 200)
    assert pool.stored() == {"token": "c200"} and clock.now == 2000


@pytest.mark.parametrize(("where", "cost"), [("probe", 1), ("probe", 10), ("log", 1), ("log", 10)])
@pytest.mark.asyncio
async def test_a_page_is_never_started_on_a_budget_the_work_between_pages_has_spent(
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    where: str,
    cost: float,
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(1000)], clock, 100), CursorPool()
    Io(monkeypatch)
    _spend_between_pages(monkeypatch, clock, where, cost)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, budget_s=101)  # page one ends at 100 s: 1 s is left

    done = _done(caplog)
    assert (len(feed.calls), counters["changes"]) == (1, 200)
    assert pool.writes == ['{"token": "c200"}']
    assert [(_field(m, "page"), _field(m, "stop")) for m in done] == [
        ("1/20", "continue"),
        ("2/20", "budget"),
    ]
    assert (_field(done[1], "from_token"), _field(done[1], "next_token")) == ("c200", "c200")
    assert "'changes': 0" in done[1] and _field(done[1], "disk_hold") == "0"  # it never started
    assert _summed(caplog) == counters


@pytest.mark.parametrize(
    ("where", "cost"), [("probe", 0), ("probe", 0.5), ("log", 0), ("log", 0.5)]
)
@pytest.mark.asyncio
async def test_work_between_pages_that_leaves_some_budget_does_not_stop_the_tick(
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    where: str,
    cost: float,
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(1000)], clock, 100), CursorPool()
    Io(monkeypatch)
    _spend_between_pages(monkeypatch, clock, where, cost)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, budget_s=101)

    done = _done(caplog)
    assert (len(feed.calls), counters["changes"]) == (2, 400)  # 1 s was left, 0.5 s were used
    assert pool.writes == ['{"token": "c200"}', '{"token": "c400"}']
    assert [(_field(m, "page"), _field(m, "stop")) for m in done] == [
        ("1/20", "continue"),
        ("2/20", "budget"),
    ]
    assert "'changes': 200" in done[1]  # page two ran: its line counts its own page
    assert _summed(caplog) == counters


@pytest.mark.asyncio
async def test_the_first_page_runs_whatever_the_probe_before_it_cost(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(450)], clock, 100), CursorPool()
    Io(monkeypatch)
    _slow_disk(monkeypatch, clock, 500)  # 500 s against a 100 s budget, before the first page

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, budget_s=100)

    assert (len(feed.calls), counters["changes"]) == (1, 200)
    assert [(_field(m, "page"), _field(m, "stop")) for m in _done(caplog)] == [("1/20", "budget")]


@pytest.mark.asyncio
async def test_the_time_spent_before_the_first_page_counts_against_the_budget(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(1000)], clock, 100), CursorPool()
    Io(monkeypatch)
    read = da._parse_cursor

    def _slow_read(raw: Any) -> tuple[str | None, int]:
        clock.now += 200  # the service and the cursor read are what took the time
        return read(raw)

    monkeypatch.setattr(da, "_parse_cursor", _slow_read)

    counters = await _walk(pool, feed, budget_s=250)

    assert (len(feed.calls), counters["changes"]) == (1, 200)  # 200 s before + 100 s of page


@pytest.mark.asyncio
async def test_a_tick_cut_by_the_budget_is_resumed_by_the_next_and_nothing_is_lost(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(1000)], clock, 100), CursorPool()
    io = Io(monkeypatch)

    ticks = []
    for _ in range(5):  # five pages: it can never need more ticks than that
        if pool.stored() == {"token": FeedDrive.END}:
            break
        ticks.append((await _walk(pool, feed, budget_s=250))["changes"])

    assert ticks == [600, 400]
    assert io.seen == [_ref(i) for i in range(1000)]  # every change once, in feed order


@pytest.mark.parametrize("cap", [1, 2, 3])
@pytest.mark.asyncio
async def test_the_page_cap_is_a_hard_limit_whatever_the_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, cap: int
) -> None:
    feed, pool = _feed(1000), CursorPool()
    Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, max_pages=cap, budget_s=10**6)

    last = _done(caplog)[-1]
    assert (len(feed.calls), counters["changes"]) == (cap, 200 * cap)
    assert (_field(last, "stop"), _field(last, "page")) == ("cap", f"{cap}/{cap}")
    assert pool.stored() == {"token": f"c{200 * cap}"}


@pytest.mark.asyncio
async def test_an_int_beyond_the_float_range_is_a_setting_and_not_an_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(1000), CursorPool()
    Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, max_pages=10**400, budget_s=10**400)

    assert (len(feed.calls), counters["changes"]) == (5, 1000)  # the whole feed, no limit reached
    assert _field(_done(caplog)[-1], "stop") == "end"


@pytest.mark.asyncio
async def test_a_page_without_a_continuation_ends_the_tick_and_never_restarts_the_feed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = NoTokenFeed([_change(i) for i in range(450)]), CursorPool('{"token": "c0"}')
    Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed)

    assert feed.calls == [("c0", LIMIT, 1)] and counters["changes"] == 200
    assert pool.writes == [] and _field(_done(caplog)[0], "stop") == "no-token"


@pytest.mark.asyncio
async def test_a_held_page_stops_the_tick_and_the_next_page_is_never_asked_for(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(450), CursorPool()
    io = Io(monkeypatch, fail={_ref(250)})  # a change of the SECOND page fails

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed)

    done = _done(caplog)
    assert feed.calls == [("c0", LIMIT, 1), ("c200", LIMIT, 1)]  # page 3 is never requested
    assert pool.writes == ['{"token": "c200"}', '{"token": "c200", "held": 1}']
    assert (counters["changes"], counters["enqueued_new"], counters["errors"]) == (400, 399, 1)
    assert [_field(m, "stop") for m in done] == ["continue", "held"]
    assert _field(done[1], "next_token") == _field(done[1], "from_token") == "c200"

    io.fail.clear()
    healed = await _walk(pool, feed)  # resumes AT the held page and goes on through page 3
    assert (healed["changes"], healed["already_present"], healed["enqueued_new"]) == (250, 199, 51)
    assert pool.stored() == {"token": FeedDrive.END}
    assert sorted(io.seen) == [_ref(i) for i in range(450)]  # f0250 comes last: it was retried


@pytest.mark.asyncio
async def test_a_system_fault_on_page_two_pins_the_cursor_there_for_any_number_of_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _feed(12), CursorPool()
    io = Io(monkeypatch, fail={_ref(i) for i in range(5, 10)})  # pages of 5: page 2 always fails

    for tick in range(1, 6):
        counters = await _walk(pool, feed, limit=5)
        assert pool.stored() == {"token": "c5", "held": tick}
        assert counters["errors"] == 5
    assert io.seen == [_ref(i) for i in range(5)]  # page 1, once; nothing of pages 2 and 3 yet

    io.fail.clear()
    await _walk(pool, feed, limit=5)
    assert pool.stored() == {"token": FeedDrive.END}
    assert io.seen == [_ref(i) for i in range(12)]


@pytest.mark.asyncio
async def test_a_page_where_nothing_succeeds_holds_for_good_and_the_pages_behind_it_are_not_reached(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(450), CursorPool()
    forbidden = {f"f{i:04d}": _drive_error(403, "forbidden") for i in range(200)}
    io = Io(monkeypatch, download_errors=forbidden)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        for tick in range(1, 6):
            await _walk(pool, feed)
            assert pool.stored() == {"token": "c0", "held": tick}

    assert [call[0] for call in feed.calls] == ["c0"] * 5  # never beyond the held page
    assert io.seen == [] and "giving up" not in caplog.text
    io.download_errors.clear()
    await _walk(pool, feed)
    assert io.seen == [_ref(i) for i in range(450)]


@pytest.mark.asyncio
async def test_a_permanent_file_is_let_go_at_the_third_tick_and_the_pages_behind_it_flow(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(450), CursorPool()
    io = Io(monkeypatch, download_errors={"f0000": _drive_error(403, "forbidden")})

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        for tick in (1, 2):
            await _walk(pool, feed)
            assert pool.stored() == {"token": "c0", "held": tick}
        counters = await _walk(pool, feed)

    last = _done(caplog)[-3:]
    assert pool.stored() == {"token": FeedDrive.END}
    assert (counters["changes"], counters["already_present"], counters["enqueued_new"]) == (
        450,
        199,
        250,
    )
    assert counters["errors"] == 1
    assert [_field(m, "gave_up") for m in last] == ["1", "0", "0"]
    assert [_field(m, "stop") for m in last] == ["continue", "continue", "end"]
    assert io.seen == [_ref(i) for i in range(1, 450)]  # everything but the file that was let go


@pytest.mark.asyncio
async def test_the_page_after_a_let_go_page_starts_its_hold_count_from_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _feed(450), CursorPool('{"token": "c0", "held": 2}')
    forbidden = _drive_error(403, "forbidden")
    Io(monkeypatch, download_errors={"f0000": forbidden, "f0200": forbidden})

    counters = await _walk(pool, feed)

    # page 1 is on its third failing tick and is let go; page 2 fails for the FIRST time: held 1
    assert pool.writes == ['{"token": "c200"}', '{"token": "c200", "held": 1}']
    assert len(feed.calls) == 2 and counters["errors"] == 2


@pytest.mark.asyncio
async def test_a_crash_between_pages_leaves_the_cursor_on_the_last_finished_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _feed(450), CursorPool()
    io = Io(monkeypatch)
    pool.fail_write, pool.fail_after = RuntimeError("write failed"), 1  # the SECOND write fails

    with pytest.raises(RuntimeError):
        await _walk(pool, feed)
    assert pool.stored() == {"token": "c200"}

    pool.fail_write = None
    counters = await _walk(
        pool, feed
    )  # page 2 was enqueued before the write: it reads already_present
    assert (counters["changes"], counters["already_present"], counters["enqueued_new"]) == (
        250,
        200,
        50,
    )
    assert pool.stored() == {"token": FeedDrive.END}
    assert io.seen == [_ref(i) for i in range(450)]


@pytest.mark.asyncio
async def test_a_listing_failure_on_page_two_ends_the_tick_with_page_one_kept_and_the_cursor_on_two(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(450), CursorPool()
    feed.fail_calls = {2}  # Drive raises on the SECOND listing of the tick
    io = Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed)

    assert (counters["changes"], counters["enqueued_new"], counters["errors"]) == (200, 200, 0)
    assert pool.writes == ['{"token": "c200"}'] and len(feed.calls) == 2  # no hold, no `held`
    assert "Drive enumeration failed" in caplog.text
    # the one stop with no done line of its own: this WARNING is its terminal signal
    assert [r.levelno for r in caplog.records if "Drive enumeration failed" in r.getMessage()] == [
        logging.WARNING
    ]
    assert _field(_done(caplog)[-1], "stop") == "continue"
    assert [_field(m, "page") for m in _done(caplog)] == [
        "1/20"
    ]  # the failed listing has no done line

    healed = await _walk(pool, feed)  # resumes AT page two and goes on to the end
    assert (healed["changes"], pool.stored()) == (250, {"token": FeedDrive.END})
    assert io.seen == [_ref(i) for i in range(450)]


@pytest.mark.asyncio
async def test_a_tick_below_the_disk_floor_downloads_nothing_and_holds_the_cursor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stored = '{"token": "c200", "held": 2}'
    feed, pool = _feed(450), CursorPool(stored)
    io = Io(monkeypatch)
    _free_space(monkeypatch, 19 * GIB)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, min_free_gib=20)

    done = _done(caplog)
    assert feed.calls == [] and io.seen == [] and pool.writes == [] and pool.value == stored
    assert counters == {
        **dict.fromkeys(LEGACY_COUNTER_KEYS, 0),
        "errors": 1,
    }  # the tick reads partial
    assert [r.levelno for r in caplog.records if "free disk space" in r.getMessage()] == [
        logging.ERROR
    ]
    assert (
        len(done) == 1 and _field(done[0], "stop") == "disk" and _field(done[0], "disk_hold") == "1"
    )
    assert (_field(done[0], "from_token"), _field(done[0], "next_token")) == ("c200", "c200")
    assert "'errors': 1" in done[0] and _summed(caplog) == counters  # the line says what it returns


@pytest.mark.asyncio
async def test_the_disk_is_checked_before_every_page_and_never_inside_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(450), CursorPool()
    Io(monkeypatch)
    probes = _free_space(
        monkeypatch, 30 * GIB, 25 * GIB, 5 * GIB
    )  # room, room, then below the floor

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, min_free_gib=20)

    assert (len(feed.calls), counters["changes"], counters["errors"]) == (2, 400, 1)
    assert pool.stored() == {"token": "c400"} and len(probes) == 3  # one probe a page, not a change
    assert [_field(m, "stop") for m in _done(caplog)] == ["continue", "continue", "disk"]
    assert _summed(caplog) == counters


@pytest.mark.parametrize(
    ("free", "floor_gib", "proceeds"),
    [
        (20 * GIB, 20, True),
        (20 * GIB - 1, 20, False),
        (GIB, 1, True),
        (GIB - 1, 1, False),
        (0, Decimal("1e-30"), False),  # the smallest floor is one byte, and it is still a floor
        (1, Decimal("1e-30"), True),
    ],
)
@pytest.mark.asyncio
async def test_the_floor_is_inclusive(
    monkeypatch: pytest.MonkeyPatch, free: int, floor_gib: int | Decimal, proceeds: bool
) -> None:
    feed, pool = _feed(3), CursorPool()
    io = Io(monkeypatch)
    _free_space(monkeypatch, free)

    await _walk(pool, feed, min_free_gib=floor_gib)

    assert bool(io.seen) is proceeds


@pytest.mark.asyncio
async def test_a_disk_probe_that_fails_holds_the_cursor_and_logs_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(3), CursorPool()
    Io(monkeypatch)

    _free_space(monkeypatch, OSError(13, "Permission denied", f"/volume/{NAME}"))

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, min_free_gib=20)

    assert feed.calls == [] and pool.writes == [] and counters["errors"] == 1
    assert "cannot read the free disk space (PermissionError at " in caplog.text
    assert NAME not in caplog.text and all(r.exc_info is None for r in caplog.records)


@pytest.mark.parametrize("zero", [0, 0.0, -0.0, "0", Decimal("-0")])
@pytest.mark.asyncio
async def test_a_floor_of_zero_is_the_default_floor_and_the_disk_is_always_probed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, zero: Any
) -> None:
    feed, pool = _feed(3), CursorPool()
    io = Io(monkeypatch)
    probes = _free_space(monkeypatch, 1 * GIB)  # a nearly full volume

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, min_free_gib=zero)

    assert feed.calls == [] and io.seen == [] and pool.writes == [] and len(probes) == 1
    assert counters["errors"] == 1 and "20.0 GiB floor" in caplog.text
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.parametrize(
    "raw",
    [
        *("-3", "-0.5", "-1e-9", "-1e300", "-1e-324", "-0", "0", "0.0", "0e5"),
        *("nan", "sNaN", "inf", "-inf", "1,5", "1e5000", NAME),
    ],
)
@pytest.mark.asyncio
async def test_a_floor_that_is_not_a_positive_number_is_the_default_floor_never_the_guard_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    feed, pool = _feed(3), CursorPool()
    io = Io(monkeypatch)
    probes = _free_space(monkeypatch, 1 * GIB)  # a volume that is nearly full
    monkeypatch.setenv(PACE_ENV[2], raw)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed)

    done = _done(caplog)
    assert feed.calls == [] and io.seen == [] and pool.writes == [] and len(probes) == 1
    assert counters["errors"] == 1 and _summed(caplog) == counters
    assert (_field(done[0], "stop"), _field(done[0], "disk_hold")) == ("disk", "1")
    assert "20.0 GiB floor" in caplog.text  # the default floor, not the one that was typed
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and PACE_ENV[2] in warned[0].getMessage()
    assert NAME not in caplog.text  # the value that was typed is never echoed


@pytest.mark.parametrize(
    ("raw", "free", "proceeds"),
    [
        ("-3", 50 * GIB, True),  # the default floor is met
        ("-3", 19 * GIB, False),
        ("0", 19 * GIB, False),
        ("5", 5 * GIB, True),  # a floor above 0 is honoured as set, inclusive
        ("5", 5 * GIB - 1, False),
        ("1e-324", 0, False),  # an underflow is still a positive floor: one byte (gate round 2)
        ("1e-324", 1, True),
        ("1e300", 10**15, False),  # a floor larger than any volume holds
    ],
)
@pytest.mark.asyncio
async def test_a_floor_from_the_environment_guards_exactly_as_set(
    monkeypatch: pytest.MonkeyPatch, raw: str, free: int, proceeds: bool
) -> None:
    feed, pool = _feed(3), CursorPool()
    io = Io(monkeypatch)
    _free_space(monkeypatch, free)
    monkeypatch.setenv(PACE_ENV[2], raw)

    await _walk(pool, feed)

    assert bool(io.seen) is proceeds


@pytest.mark.asyncio
async def test_once_the_disk_has_room_again_the_walk_resumes_at_the_same_token_and_loses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, pool = _feed(450), CursorPool()
    io = Io(monkeypatch)
    _free_space(monkeypatch, 1 * GIB)
    await _walk(pool, feed, min_free_gib=20)
    assert io.seen == [] and pool.writes == []

    _free_space(monkeypatch, 50 * GIB)
    counters = await _walk(pool, feed, min_free_gib=20)

    assert counters["changes"] == 450 and io.seen == [_ref(i) for i in range(450)]


def test_the_free_space_is_read_from_the_nearest_existing_parent_of_the_blob_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked: list[Path] = []

    def _usage(path: Path) -> types.SimpleNamespace:
        asked.append(Path(path))
        return types.SimpleNamespace(free=1234)

    monkeypatch.setattr(da.shutil, "disk_usage", _usage)
    monkeypatch.setattr(da, "_BLOB_ROOT", tmp_path / "not" / "made" / "yet")
    assert REAL_FREE_BYTES() == 1234 and asked == [tmp_path]

    monkeypatch.setattr(da, "_BLOB_ROOT", tmp_path)  # a root that exists is asked directly
    assert REAL_FREE_BYTES() == 1234 and asked == [tmp_path, tmp_path]


FLOOR_DEFAULT = 20 * GIB


ENV_ONLY = (None, None, None)  # no argument: the settings come from the environment


def _env(*values: str) -> dict[str, str]:
    return dict(zip(PACE_ENV, values, strict=True))


def _env_floor(raw: str, floor: int, warned: int) -> tuple[Any, ...]:
    return ({PACE_ENV[2]: raw}, ENV_ONLY, (300.0, 20, floor), warned)


def _arg_floor(value: Any, floor: int, warned: int) -> tuple[Any, ...]:
    return ({}, (None, None, value), (300.0, 20, floor), warned)


@pytest.mark.parametrize(
    ("env", "args", "expected", "warned"),
    [
        ({}, ENV_ONLY, (300.0, 20, FLOOR_DEFAULT), 0),
        (_env("90", "7", "5"), ENV_ONLY, (90.0, 7, 5 * GIB), 0),
        (_env("90", "7", "5"), (10, 3, 1), (10, 3, GIB), 0),  # an argument wins
        (_env("soon", "nan", "inf"), ENV_ONLY, (300.0, 20, FLOOR_DEFAULT), 3),
        ({PACE_ENV[1]: "0"}, ENV_ONLY, (300.0, 1, FLOOR_DEFAULT), 0),  # at least one page
        ({PACE_ENV[1]: "2.9"}, ENV_ONLY, (300.0, 2, FLOOR_DEFAULT), 0),
        # a floor that is not a positive number is the default floor and one WARNING, never "off"
        *(
            _env_floor(raw, FLOOR_DEFAULT, 1)
            for raw in ("0", "-3", "-1e-12", "-1e-324", "sNaN", "inf")
        ),
        _env_floor("   ", FLOOR_DEFAULT, 0),  # blank: the default, quietly
        _env_floor("1e3992", FLOOR_DEFAULT, 1),  # past the decimal range read here (10**4000 bytes)
        _env_floor("1e-9999999999999999999999", FLOOR_DEFAULT, 1),  # past what Decimal can read
        # a positive number is at least one byte, rounded up exactly
        _env_floor("1e-12", 1, 0),
        _env_floor("1e-324", 1, 0),
        _env_floor("1e-999999", 1, 0),
        _env_floor("1.0000000001", GIB + 1, 0),
        _env_floor("100000000000000000000000000000.5", 10**29 * GIB + GIB // 2, 0),
        _env_floor("1e300", 10**300 * GIB, 0),
        _env_floor("1e3991", 10**3991 * GIB, 0),  # the largest floor that is read
        # the same rules for the argument
        *(
            _arg_floor(value, FLOOR_DEFAULT, 1)
            for value in (-3, 0, -0.0, True, Decimal("sNaN"), float("nan"), float("inf"), -math.inf)
        ),
        _arg_floor(5e-324, 1, 0),
        ({}, (float("nan"), None, None), (300.0, 20, FLOOR_DEFAULT), 1),
        ({}, (None, float("inf"), None), (300.0, 20, FLOOR_DEFAULT), 1),
        ({}, (-5, -5, None), (-5, 1, FLOOR_DEFAULT), 0),  # the tightest budget and cap
        # float() is safe for the budget and the cap: an underflow is the TIGHTEST setting
        ({PACE_ENV[0]: "1e-324"}, ENV_ONLY, (0.0, 20, FLOOR_DEFAULT), 0),
        ({PACE_ENV[0]: "-1e-324"}, ENV_ONLY, (0.0, 20, FLOOR_DEFAULT), 0),
        ({PACE_ENV[1]: "1e-324"}, ENV_ONLY, (300.0, 1, FLOOR_DEFAULT), 0),
        ({PACE_ENV[1]: "-1e-324"}, ENV_ONLY, (300.0, 1, FLOOR_DEFAULT), 0),
        ({PACE_ENV[0]: "1e309"}, ENV_ONLY, (300.0, 20, FLOOR_DEFAULT), 1),  # inf: the default
        ({PACE_ENV[1]: "-1e309"}, ENV_ONLY, (300.0, 20, FLOOR_DEFAULT), 1),
        ({PACE_ENV[0]: "1e300"}, ENV_ONLY, (1e300, 20, FLOOR_DEFAULT), 0),  # honoured as typed
        ({PACE_ENV[1]: "1e300"}, ENV_ONLY, (300.0, int(1e300), FLOOR_DEFAULT), 0),
        # an int is finite however large: honoured as typed, never turned into a float first
        ({}, (10**400, 10**400, None), (10**400, 10**400, FLOOR_DEFAULT), 0),
        ({}, (-(10**400), -(10**400), None), (-(10**400), 1, FLOOR_DEFAULT), 0),
    ],
)
def test_the_pace_comes_from_the_environment_with_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
    args: tuple[Any, ...],
    expected: tuple[float, int, int],
    warned: int,
) -> None:
    for name in PACE_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with caplog.at_level(logging.WARNING, logger=da.logger.name):
        assert da._drain_settings(*args) == expected

    assert sum(name in caplog.text for name in PACE_ENV) == warned  # each bad variable is named


def _floor_oracle(value: Any) -> tuple[int, int]:
    """(floor in bytes, WARNINGs) the spec asks for, by a route that shares with the adapter only the
    grammar of a decimal number: the sign test and the ceiling are exact rational arithmetic."""
    if isinstance(value, str) and not value.strip():
        return FLOOR_DEFAULT, 0  # blank is absent: the default, quietly
    if isinstance(value, bool):
        return FLOOR_DEFAULT, 1
    try:
        number = Decimal(value.strip() if isinstance(value, str) else value)
    except (ArithmeticError, TypeError):
        return FLOOR_DEFAULT, 1  # not a number
    if not number.is_finite() or number <= 0:
        return FLOOR_DEFAULT, 1
    return max(1, math.ceil(Fraction(number) * GIB)), 0


def _generated_floor_strings() -> list[str]:
    """600+ strings from a fixed seed: signs, zeros, long and short digits, fractions, exponents up to
    e+420 and down to e-420, underscores, spaces, and the troublemakers (nan and inf, garbage)."""
    rng = random.Random(20261103)
    strings = [
        *("", " ", "0", "+0", "-0", "00", "0.0", "0.000", "0e5", "-0e-5", "0_0", ".0", "-.0"),
        *(".", "-", "+", "1e-324", "-1e-324", "5e-324", "-5e-324", "1e-400", "-1e-400"),
        *("1e-12", "-1e-12", "1e300", "-1e300", "1e400", "nan", "NaN", "-nan", "+NaN", "sNaN"),
        *("-sNaN", "SNAN", "inf", "-inf", "+inf", "INF", "Infinity", "-Infinity", "infinity"),
        *("abc", "1,5", "0x10", "1 000", "--5", "+-5", "1e", "e5", "1e+", "\u0663", "1/2"),
        "123456789012345678901234567890.123456789012345678901234567890",
        "100000000000000000000000000000.5",
        "0." + "0" * 60 + "1",
        "9" * 60,
        "9" * 60 + "e-60",
    ]
    for _ in range(600):
        whole = "".join(rng.choice("0123456789") for _ in range(rng.randint(0, 8)))
        frac = "".join(rng.choice("0123456789") for _ in range(rng.randint(0, 40)))
        text = rng.choice(["", "", "+", "-"]) + whole + ("." + frac if rng.random() < 0.6 else "")
        if rng.random() < 0.6:
            text += rng.choice("eE") + rng.choice(["", "+", "-"]) + str(rng.randint(0, 420))
        if rng.random() < 0.15:
            at = rng.randint(0, len(text))
            text = text[:at] + "_" + text[at:]
        if rng.random() < 0.2:
            text = " " * rng.randint(0, 2) + text + " " * rng.randint(0, 2)
        strings.append(text)
    return strings


def test_the_floor_is_never_below_one_byte_for_any_string_of_a_seeded_table(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    strings = _generated_floor_strings()
    seen = dict.fromkeys(("quiet", "rejected", "tiny", "fractional", "huge"), 0)
    for raw in strings:
        monkeypatch.setenv(PACE_ENV[2], raw)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=da.logger.name):
            floor = da._drain_settings(None, None, None)[2]
        expected, warnings = _floor_oracle(raw)
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert floor >= 1, raw
        assert (floor, len(warned)) == (expected, warnings), raw
        seen["quiet"] += not raw.strip()
        seen["rejected"] += warnings
        seen["tiny"] += expected == 1
        seen["fractional"] += expected not in (1, FLOOR_DEFAULT) and expected % GIB != 0
        seen["huge"] += expected >= 10**30
    # the table has to exercise every branch, or passing it proves little
    assert len(strings) >= 600
    assert seen["quiet"] >= 2 and seen["rejected"] >= 150 and seen["tiny"] >= 50, seen
    assert seen["fractional"] >= 60 and seen["huge"] >= 100, seen


FLOOR_ARGUMENTS = [
    *(0, 0.0, -0.0, 5e-324, -5e-324, float("inf"), float("-inf"), float("nan")),
    *(1e300, 1e-30, 1, 20, -1, True, False, Decimal("sNaN"), Decimal("NaN"), Decimal("-0")),
    *(Decimal("Infinity"), Decimal("1E-2000"), Decimal("1E+400"), Decimal("20"), Decimal("0.5")),
]


@pytest.mark.parametrize("value", FLOOR_ARGUMENTS, ids=repr)
def test_the_floor_is_never_below_one_byte_for_any_argument(
    caplog: pytest.LogCaptureFixture, value: Any
) -> None:
    with caplog.at_level(logging.WARNING, logger=da.logger.name):
        floor = da._drain_settings(None, None, value)[2]

    expected, warnings = _floor_oracle(value)
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert floor >= 1 and (floor, len(warned)) == (expected, warnings)


@pytest.mark.parametrize("raw", ["1e300", "1e400", "1e3991", "9" * 60])
@pytest.mark.asyncio
async def test_a_floor_larger_than_any_volume_holds_every_tick_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    feed, pool = _feed(3), CursorPool()
    io = Io(monkeypatch)
    probes = _free_space(
        monkeypatch, 10**15
    )  # about a petabyte free, and a walk that goes on is stopped
    monkeypatch.setenv(PACE_ENV[2], raw)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed)

    done = _done(caplog)
    assert feed.calls == [] and io.seen == [] and pool.writes == [] and counters["errors"] == 1
    assert len(probes) == 1 and len(done) == 1
    assert (_field(done[0], "stop"), _field(done[0], "disk_hold")) == ("disk", "1")
    assert _summed(caplog) == counters and "e+" in caplog.text  # the huge figure is in the line


@pytest.mark.parametrize("raw", ["1e3992", "1e5000", "1e999999", "1e9999999999999999999999"])
@pytest.mark.asyncio
async def test_a_floor_beyond_the_decimal_range_is_the_default_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    feed, pool = _feed(3), CursorPool()
    io = Io(monkeypatch)
    _free_space(monkeypatch, 50 * GIB)  # room for the default floor, not for a typed 1e5000
    monkeypatch.setenv(PACE_ENV[2], raw)

    with caplog.at_level(logging.WARNING, logger=da.logger.name):
        counters = await _walk(pool, feed)

    assert io.seen == [_ref(i) for i in range(3)] and counters["errors"] == 0
    assert [PACE_ENV[2] in r.getMessage() for r in caplog.records] == [True]


@pytest.mark.parametrize(
    ("n_bytes", "text"),
    [
        (0, "0.0"),
        (1, "9.313e-10"),
        (100 * 2**20, "0.1"),
        (GIB, "1.0"),
        (20 * GIB, "20.0"),
        (10**15 * GIB, "1.000e+15"),
        (10**400 * GIB, "1.000e+400"),
    ],
)
def test_a_size_is_written_in_gib_and_never_raises_whatever_its_magnitude(
    n_bytes: int, text: str
) -> None:
    assert da._gib_text(n_bytes) == text


@pytest.mark.parametrize("raw", ["1e-324", "-1e-324", "0", "-5"])
@pytest.mark.parametrize("setting", [0, 1])
@pytest.mark.asyncio
async def test_a_budget_or_a_cap_that_is_zero_negative_or_underflows_is_one_page(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, setting: int, raw: str
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(1000)], clock, 100), CursorPool()
    Io(monkeypatch)
    monkeypatch.setenv(PACE_ENV[setting], raw)  # the budget (0) or the page cap (1)

    counters = await _walk(pool, feed)

    assert (len(feed.calls), counters["changes"]) == (1, 200)  # never zero pages, never unlimited


@pytest.mark.asyncio
async def test_the_page_cap_and_the_budget_and_the_floor_reach_the_drain_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(1000)], clock, 100), CursorPool()
    io = Io(monkeypatch)

    monkeypatch.setenv(PACE_ENV[1], "2")
    assert (await _walk(pool, feed))["changes"] == 400  # the cap: 2 pages
    monkeypatch.setenv(PACE_ENV[0], "150")
    monkeypatch.setenv(PACE_ENV[1], "20")
    assert (await _walk(pool, feed))["changes"] == 400  # the budget: 100 s < 150 s, 200 s >= 150 s
    _free_space(monkeypatch, 5 * GIB)
    monkeypatch.setenv(PACE_ENV[2], "10")
    assert (await _walk(pool, feed))["errors"] == 1  # the floor: 5 GiB free < 10 GiB
    assert io.seen == [_ref(i) for i in range(800)]


@pytest.mark.asyncio
async def test_the_runner_reports_the_tick_summed_over_its_pages(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, runner: Any
) -> None:
    pool = CursorPool()
    Io(monkeypatch)
    _wire(monkeypatch, runner, pool, _feed(450))

    with caplog.at_level(logging.INFO):
        code = await runner.main()

    state = json.loads(runner.STATE_FILE.read_text())
    own = [r.getMessage() for r in caplog.records if r.name == "drive_intake_drain"]
    assert code == 0 and (state["changes"], state["enqueued_new"]) == (450, 450)
    assert set(state) == {"job", "ts", "status", "elapsed", *LEGACY_COUNTER_KEYS}
    assert own == [f"drain tick done: {state}"]  # ONE runner line, the pages' sum
    assert len(_done(caplog)) == 3 and pool.stored() == {"token": FeedDrive.END}


@pytest.mark.asyncio
async def test_a_fault_on_the_second_page_stops_the_runner_with_the_first_page_kept(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, runner: Any
) -> None:
    pool = CursorPool()
    pool.fail_write, pool.fail_after = RuntimeError(f"cannot touch {NAME}"), 1
    Io(monkeypatch)
    _wire(monkeypatch, runner, pool, _feed(450))

    with caplog.at_level(logging.DEBUG):
        code = await runner.main()

    assert code == 1 and pool.closed and not runner.STATE_FILE.exists()
    assert pool.stored() == {"token": "c200"}  # page one is kept, page two is retried next tick
    assert "drain tick failed (RuntimeError at " in caplog.text and NAME not in caplog.text


@pytest.mark.asyncio
async def test_a_feed_below_the_limit_under_the_default_pace_gives_todays_counters_and_write(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(150), CursorPool()
    Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed)

    done = _done(caplog)
    assert (counters["changes"], counters["enqueued_new"], counters["errors"]) == (150, 150, 0)
    assert pool.writes == ['{"token": "cEND"}']  # today's single write
    assert len(done) == 1 and done[0].endswith(
        "from_token=c0 next_token=cEND more_pages=False gave_up=0"
    )


@pytest.mark.asyncio
async def test_the_done_line_of_a_one_page_tick_says_page_1_of_the_cap_stop_end_and_no_disk_hold(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(150), CursorPool()
    Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        await _walk(pool, feed)

    done = _done(caplog)
    assert len(done) == 1
    assert [_field(done[0], name) for name in ("page", "stop", "disk_hold")] == ["1/20", "end", "0"]


SUM_PATHS = {  # how a tick is made to stop -> the stop reason on its last done line
    "end": "end",
    "budget": "budget",
    "cap": "cap",
    "disk": "disk",
    "disk-later": "disk",
    "held": "held",
    "no-token": "no-token",
    "let-go": "end",
    "listing": "continue",
    "recheck": "budget",
}


@pytest.mark.parametrize("path", list(SUM_PATHS))
@pytest.mark.asyncio
async def test_the_done_lines_add_up_to_the_counters_the_tick_returns_whichever_way_it_stops(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    feed: FeedDrive = TimedFeed([_change(i) for i in range(450)], clock, 100)
    pool, io, settings = CursorPool(), Io(monkeypatch), {}
    if path == "budget":
        settings = {"budget_s": 150}
    elif path == "cap":
        settings = {"max_pages": 2}
    elif path == "disk":
        _free_space(monkeypatch, 1 * GIB)
        settings = {"min_free_gib": 20}
    elif path == "disk-later":
        _free_space(monkeypatch, 50 * GIB, 1 * GIB)
        settings = {"min_free_gib": 20}
    elif path == "held":
        io.fail = {_ref(250)}
    elif path == "no-token":
        feed = NoTokenFeed([_change(i) for i in range(450)])
    elif path == "let-go":
        pool = CursorPool('{"token": "c0", "held": 2}')
        io.download_errors = {"f0000": _drive_error(403, "forbidden")}
    elif path == "listing":
        feed.fail_calls = {2}
    elif path == "recheck":
        _slow_disk(monkeypatch, clock, 0, 10)
        settings = {"budget_s": 101}

    with caplog.at_level(logging.INFO, logger=da.logger.name):
        counters = await _walk(pool, feed, **settings)

    assert _field(_done(caplog)[-1], "stop") == SUM_PATHS[path]  # the path was really taken
    assert _summed(caplog) == counters


@pytest.mark.asyncio
async def test_a_tick_that_raises_has_a_done_line_only_for_the_pages_whose_cursor_was_stored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    feed, pool = _feed(450), CursorPool()
    pool.fail_write, pool.fail_after = RuntimeError("write failed"), 1  # page two's write fails
    Io(monkeypatch)

    with caplog.at_level(logging.INFO, logger=da.logger.name), pytest.raises(RuntimeError):
        await _walk(pool, feed)

    assert pool.writes == ['{"token": "c200"}']
    assert [_field(m, "next_token") for m in _done(caplog)] == ["c200"]
    assert _summed(caplog)["changes"] == 200


@pytest.mark.asyncio
async def test_the_budget_paces_the_walk_and_is_not_a_wall_clock_limit(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    feed, pool = TimedFeed([_change(i) for i in range(450)], clock, 130), CursorPool()
    Io(monkeypatch)
    _slow_disk(monkeypatch, clock, 500)  # the probe before the first page takes 500 s

    counters = await _walk(pool, feed)  # the default budget: 300 s

    # 300 s of budget plus a 130 s page is the USUAL tick (430 s); a slow probe makes it 630 s, because
    # the first page always runs and the setup, the probe and the logging have no time limit of their own
    assert (len(feed.calls), counters["changes"], clock.now) == (1, 200, 630)


# 50 files at the 2.2 s each measured on the Pro is 110 s; the heaviest tick under 600 s in its log
# (4,950 ticks) took 118 s for 56 files. This pins the usual tick under the interval: an estimate,
# not a ceiling (see the test above).
SLOWEST_PAGE_S = 130


def test_the_default_budget_plus_one_slow_page_stays_under_the_launchd_interval() -> None:
    if not (PLIST.exists()):
        pytest.skip('infra/launchagents/com.balizero.drive-intake-drain.plist not in Rakyat')
    interval = plistlib.loads(PLIST.read_bytes())["StartInterval"]

    assert da._DEFAULT_BUDGET_S + SLOWEST_PAGE_S < interval
