"""Apple Vision OCR for the document-intake worker -- macOS only, 100% on-device.

``classify.ocr_pages`` tries this engine first when ``INTAKE_OCR_PROVIDER=apple_vision``
and falls through to the Ollama vision chain for any page it cannot read. The engine is
a ~140-line Swift helper (``apple_vision_ocr.swift``: ``VNRecognizeTextRequest``,
revision 3, ``.accurate``) that this module compiles on first use and drives as a
subprocess (not PyObjC: no new dependency, a Vision crash cannot take the worker down, and it
is killable on timeout): page bytes in on stdin, one JSON object out on stdout.

Cold start (measured 2026-11-03 on M5 and Pro, macOS 27.2: 40-150 s, then 0.1-0.5 s a page):
the first ``perform`` under a name the OS has not seen compiles Vision's models into
``~/Library/Caches/<executable name>/com.apple.e5rt.e5bundlecache/<os build>/``. Two things
name the warm entry: the executable's NAME (that directory) and the ad-hoc signature's
Identifier (the entry in it), which the linker takes from the ``-o`` file name. A byte-identical
copy under another name is cold (83.9 s, same Identifier); new code under the same name and
Identifier is warm. ``_compile`` therefore always links, and the cache always runs, a file called
``apple-vision-ocr`` (inside a private temp directory, then renames the directory away): a
rebuild after a source change, a purged build directory or a redeploy stays warm, and only the
first run on a host and OS build pays. A run killed halfway does not reliably keep its progress,
so no per-page timeout can ever get through a cold start. ``prepare()`` therefore warms the
helper ONCE per process, in a background thread with its own long budget; pages that arrive
meanwhile are told ``AppleVisionUnavailable`` after a short grace and take the Ollama path,
exactly as before this module existed.

The warm-up is also a self-test: a warm cache can go STALE, and a peer seat saw Vision on Pro
answer "no text" to every page, with no error (2026-11-03, cause unproven). A blank page would
pass that, so the warm-up page carries digits (drawn with Pillow) and fails unless they come
back; the stale cache directory is then parked (moved aside by ``_park``, never deleted, never over
an earlier park, five generations at most) and the helper tried once more. It runs when the engine
is prepared (first use, and after a timeout or a spawn failure), not on every page.

The compiled helper receives client documents, so it runs only from a directory chain that no
other user can write: every directory this module creates is owner-only (0700) whatever the
umask, and before a cached executable is trusted the chain from the cache root down must be
real (not symlinked) directories and a real file of this user without group/other write. What
fails the check is parked (``_park``, reason ``unsafe``) and rebuilt, or, when the roots themselves
fail, the engine is unavailable (``cache_unsafe``). The same rule covers the OS's own cache
directory for the helper. It is checked when the engine is prepared, not per page.

Failure contract: the two exception classes below are the only things ``recognize`` raises
on purpose. Their messages are short fixed codes, never image content or recognised text:
the recognised text is client PII (passports, KITAS, bank statements), so it is returned
to the caller and logged nowhere -- counts and durations only. Image bytes travel on stdin;
no temporary copy of a client document is ever written to disk.

Environment knob: INTAKE_APPLE_VISION_MAX_INFLIGHT, concurrent helper processes (default 2,
max 8). The per-page timeout and the minimum text length live in ``classify.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import functools
import hashlib
import io
import json
import logging
import math
import os
import platform
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("zantara.intake.apple_vision")

__all__ = [
    "AppleVisionError",
    "AppleVisionUnavailable",
    "available",
    "group_rows",
    "prepare",
    "recognize",
]

_SWIFT_SOURCE = Path(__file__).with_name("apple_vision_ocr.swift")
# Vision's warm cache is keyed on this executable NAME and on the Identifier the linker derives
# from it (module docstring): renaming it costs one cold start a host.
_BINARY_NAME = "apple-vision-ocr"
_XCRUN = "/usr/bin/xcrun"
# VNRecognizeTextRequestRevision3 / automaticallyDetectsLanguage need macOS 13. Pinned
# explicitly: the Xcode toolchain's default target can be NEWER than the host OS.
_DEPLOYMENT_TARGET = "13.0"
_COMPILE_TIMEOUT_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 600.0
_FAILURE_MARKER = "compile-failed"
_READY_GRACE_SECONDS = 5.0
_POLL_SECONDS = 0.05

DEFAULT_MAX_INFLIGHT = 2
_MAX_CONFIGURED_INFLIGHT = 8
_WARMUP_TIMEOUT_SECONDS = 600.0
# What the warm-up page says: digits only, so there is no letter to confuse and nothing to "correct".
_CANARY_TEXT = "58217364"
_PARKED_GENERATIONS = 5
_PRIVATE = 0o700

_ROW_LOOKBACK = 3
_ROW_MIN_OVERLAP = 0.5
# Vision's boxes are normalised to the upright page, and a box may overhang the edge by a hair;
# a coordinate further out than this is a broken helper, not a page.
_BOX_SLACK = 0.05
# The only error classes apple_vision_ocr.swift emits. A closed set, not a pattern: whatever
# else arrives in "error" is reported as engine_unknown and never echoed.
_ENGINE_ERROR_CLASSES = frozenset(
    {"empty_input", "decode_failed", "vision_failed", "encode_failed"}
)


class AppleVisionUnavailable(RuntimeError):
    """The engine cannot be used right now: not macOS, no toolchain, compile failed, still
    warming up. The caller should use another engine; nothing was recognised."""


class AppleVisionError(RuntimeError):
    """The engine ran and failed: timeout, crash, bad output, or an ``ok:false`` answer."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _platform() -> str:
    return sys.platform


def available() -> bool:
    """True when this host can run the engine at all: macOS with the Xcode shim."""
    return _platform() == "darwin" and Path(_XCRUN).exists()


def max_inflight() -> int:
    raw = os.getenv("INTAKE_APPLE_VISION_MAX_INFLIGHT", str(DEFAULT_MAX_INFLIGHT))
    try:
        configured = int(raw)
    except ValueError:
        logger.warning(
            "invalid INTAKE_APPLE_VISION_MAX_INFLIGHT=%r; using %d", raw, DEFAULT_MAX_INFLIGHT
        )
        configured = DEFAULT_MAX_INFLIGHT
    return min(max(configured, 1), _MAX_CONFIGURED_INFLIGHT)


def _cache_root() -> Path:
    return Path.home() / "Library" / "Caches" / "nuzantara" / "intake-apple-vision"


# ---------------------------------------------------------------------------
# Row grouping (pure)
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer literal beyond float range (10**400)
        return None
    return number if math.isfinite(number) else None


class _Row:
    """One visual text row: the vertical extent of its members and the words in it."""

    __slots__ = ("bottom", "top", "words")

    def __init__(self, top: float, bottom: float) -> None:
        self.top = top
        self.bottom = bottom
        self.words: list[tuple[float, str]] = []

    def overlap_ratio(self, top: float, bottom: float) -> float:
        """Vertical overlap with this row, as a fraction of the smaller of the two heights."""
        smaller = min(bottom - top, self.bottom - self.top)
        if smaller <= 0:
            return 0.0
        return (min(bottom, self.bottom) - max(top, self.top)) / smaller


def group_rows(observations: Iterable[Mapping[str, Any]]) -> str:
    """Rebuild reading order from Vision's boxes: rows joined by newline, words by a space.

    Observations are the helper's ``{"text", "x", "y", "w", "h", ...}`` dicts with a
    TOP-LEFT origin. Sorted by top edge, each one joins the best of the last three open
    rows when their vertical overlap exceeds half of the smaller height (a skewed photo
    drifts a few tenths of a line over a page width), else it opens a new row. Inside a
    row the words are ordered by x. Blank or malformed observations are skipped.
    """
    boxes: list[tuple[float, float, float, str]] = []
    for observation in observations:
        text = observation.get("text")
        left = _number(observation.get("x"))
        top = _number(observation.get("y"))
        height = _number(observation.get("h"))
        if not isinstance(text, str) or not text.strip():
            continue
        if left is None or top is None or height is None:
            continue
        boxes.append((top, top + max(height, 0.0), left, text.strip()))
    boxes.sort(key=lambda box: (box[0], box[2]))

    rows: list[_Row] = []
    for top, bottom, left, text in boxes:
        best: _Row | None = None
        best_ratio = _ROW_MIN_OVERLAP
        for row in reversed(rows[-_ROW_LOOKBACK:]):  # newest first: a tie keeps the newest row
            ratio = row.overlap_ratio(top, bottom)
            if ratio > best_ratio:
                best, best_ratio = row, ratio
        if best is None:
            best = _Row(top, bottom)
            rows.append(best)
        else:
            best.top, best.bottom = min(best.top, top), max(best.bottom, bottom)
        best.words.append((left, text))
    return "\n".join(" ".join(word for _, word in sorted(row.words)) for row in rows)


# ---------------------------------------------------------------------------
# Helper output
# ---------------------------------------------------------------------------


def _valid_observation(item: Any) -> bool:
    """One observation as the helper promises it: text, and a box in the unit square (top-left
    origin) with a positive extent. Python's json accepts NaN and Infinity, so finiteness is
    checked here, not assumed."""
    if not isinstance(item, dict) or not isinstance(item.get("text"), str):
        return False
    x, y, w, h = (_number(item.get(key)) for key in ("x", "y", "w", "h"))
    if x is None or y is None or w is None or h is None:
        return False
    low, high = -_BOX_SLACK, 1 + _BOX_SLACK
    return low <= x <= high and low <= y <= high and 0 < w <= high and 0 < h <= high


def _parse(stdout: bytes, returncode: int | None) -> dict[str, Any]:
    """Validate the helper's single JSON object. Raises AppleVisionError with a fixed code.

    A success is believed only from a process that exited 0 (a helper that printed an answer and
    then crashed or was killed is not an answer), and only if EVERY observation is well formed:
    one malformed observation makes the whole response malformed rather than being dropped.
    """
    try:
        payload = json.loads(stdout)
    except (ValueError, RecursionError):
        raise AppleVisionError(f"no_json_exit_{returncode}") from None
    if not isinstance(payload, dict):
        raise AppleVisionError("bad_shape")
    if payload.get("ok") is not True:
        error = payload.get("error")
        known = isinstance(error, str) and error in _ENGINE_ERROR_CLASSES
        raise AppleVisionError(f"engine_{error if known else 'unknown'}")
    if returncode != 0:  # also a signal (negative) and None: only a clean exit is a success
        raise AppleVisionError("nonzero_exit")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not all(map(_valid_observation, observations)):
        raise AppleVisionError("bad_observations")
    return {
        "width": payload.get("width"),
        "height": payload.get("height"),
        "observations": observations,
    }


# ---------------------------------------------------------------------------
# Build: compile the Swift helper once per (source, recipe), cache it by hash
# ---------------------------------------------------------------------------


def _compile_flags() -> list[str]:
    return [
        "-O",
        "-swift-version",
        "5",
        "-module-name",
        "AppleVisionOCR",
        "-target",
        f"{platform.machine()}-apple-macosx{_DEPLOYMENT_TARGET}",
    ]


@functools.cache
def _build_key() -> str:
    """First 16 hex of sha256(source + flags): a changed source or recipe is a new binary."""
    digest = hashlib.sha256()
    digest.update(_SWIFT_SOURCE.read_bytes())
    digest.update(b"\0")
    digest.update("\0".join(_compile_flags()).encode())
    return digest.hexdigest()[:16]


def _runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _ours_alone(path: Path, kind: int) -> bool:
    """``path`` is a real directory (``stat.S_IFDIR``) or file (``stat.S_IFREG``) of this user that
    nobody else can write. lstat, so a symlink is neither and a planted link never passes."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_IFMT(info.st_mode) == kind
        and info.st_uid == os.getuid()
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _make_private_dirs(path: Path) -> None:
    """``mkdir -p`` where every directory it creates is owner-only whatever the umask:
    ``Path.mkdir`` masks its mode by the umask and gives parents the default mode, so each missing
    level is created and then chmod-ed explicitly. Directories that already exist are not touched."""
    for directory in reversed([p for p in (path, *path.parents) if not p.exists()]):
        directory.mkdir(mode=_PRIVATE, exist_ok=True)
        directory.chmod(_PRIVATE)


def _entry_is_ours_alone(directory: Path, binary: Path) -> bool:
    """The key directory and the executable in it, whichever of them exist."""
    return all(
        _ours_alone(path, kind)
        for path, kind in ((directory, stat.S_IFDIR), (binary, stat.S_IFREG))
        if os.path.lexists(path)
    )


def _refuse_after_recent_failure(directory: Path) -> None:
    marker = directory / _FAILURE_MARKER
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return
    if age < _FAILURE_TTL_SECONDS:
        raise AppleVisionUnavailable("compile_failed_recently")


def _compile(directory: Path, binary: Path) -> None:
    """swiftc into a private temp dir, then an atomic rename: readers never see a torso.

    The file KEEPS its final basename (see the module docstring: the OS's Vision cache is keyed
    on that name and on the Identifier the linker derives from it).
    """
    workdir = Path(tempfile.mkdtemp(prefix=".build-", dir=directory))
    scratch = workdir / _BINARY_NAME
    command = [_XCRUN, "swiftc", *_compile_flags(), "-o", str(scratch), str(_SWIFT_SOURCE)]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_COMPILE_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode == 0 and scratch.is_file():
            scratch.chmod(_PRIVATE)
            scratch.replace(binary)
            (directory / _FAILURE_MARKER).unlink(missing_ok=True)
            logger.info("apple vision helper compiled in %.1fs", time.monotonic() - started)
            return
        failure = f"compile_exit_{completed.returncode}"
        logger.warning(
            "apple vision helper compile failed (exit %s): %s",
            completed.returncode,
            completed.stderr[-500:],
        )
    except subprocess.TimeoutExpired:
        failure = "compile_timeout"
        logger.warning(
            "apple vision helper compile timed out after %.0fs", _COMPILE_TIMEOUT_SECONDS
        )
    except OSError as exc:
        failure = f"compile_spawn_{type(exc).__name__}"
    finally:
        scratch.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            workdir.rmdir()
    with contextlib.suppress(OSError):
        (directory / _FAILURE_MARKER).write_text(failure)
    raise AppleVisionUnavailable(failure)


def _build_binary() -> Path:
    """Return the compiled helper, building it on first use. Blocking: run in a thread.

    An existing executable is trusted only inside a chain nobody else can write (module
    docstring): a key directory or executable that fails the check is parked (``_park``) and
    rebuilt; roots that fail it, or a park that is refused, make the engine unavailable
    (``cache_unsafe``).

    Safe against sibling processes (flock) and against concurrent threads of this one
    (they queue on the same flock, then find the finished binary). A failed compile is
    remembered for ten minutes by a marker file next to the binary, so neither a retry
    loop nor a respawning worker can start a compile storm.
    """
    try:
        root = _cache_root()
        _make_private_dirs(root)
        # A root that is not ours alone is nobody's to build under: nothing is created below it.
        if not (_ours_alone(root.parent, stat.S_IFDIR) and _ours_alone(root, stat.S_IFDIR)):
            raise AppleVisionUnavailable("cache_unsafe")
        directory = root / _build_key()
        binary = directory / _BINARY_NAME
        if not _entry_is_ours_alone(directory, binary):
            if not _park(directory, "unsafe"):
                raise AppleVisionUnavailable("cache_unsafe")
            logger.warning("apple vision helper cache was not ours alone; parked, rebuilding")
        _make_private_dirs(directory)
        if _runnable(binary):
            return binary
        with (directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _runnable(binary):  # a sibling compiled it while we waited for the lock
                return binary
            _refuse_after_recent_failure(directory)
            _compile(directory, binary)
    except OSError as exc:
        raise AppleVisionUnavailable(f"cache_unusable_{type(exc).__name__}") from None
    return binary


def _canary_page() -> bytes:
    """A white PNG that says ``_CANARY_TEXT`` in large type. Synthetic: nothing here is PII."""
    from PIL import Image, ImageDraw, ImageFont  # a runtime dependency of the intake package

    page = Image.new("RGB", (900, 220), "white")
    font = ImageFont.load_default(size=96)
    ImageDraw.Draw(page).text((40, 40), _CANARY_TEXT, fill="black", font=font)
    png = io.BytesIO()
    page.save(png, format="PNG")
    return png.getvalue()


def _canary(binary: Path) -> bool:
    """True when the digits of the warm-up page come back; False is a helper that sees no text
    (a stale OS cache). Raises AppleVisionUnavailable if the run itself fails."""
    page = _canary_page()  # a Pillow problem is not a spawn problem: let it name itself
    try:
        completed = subprocess.run(
            [str(binary)],
            input=page,
            capture_output=True,
            timeout=_WARMUP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AppleVisionUnavailable("warmup_timeout") from None
    except OSError as exc:
        raise AppleVisionUnavailable(f"warmup_spawn_{type(exc).__name__}") from None
    try:
        observations = _parse(completed.stdout, completed.returncode)["observations"]
    except AppleVisionError as exc:
        raise AppleVisionUnavailable(f"warmup_{exc.reason}") from None
    return _CANARY_TEXT in "".join(group_rows(observations).split())


def _vision_cache_parent() -> Path:
    """The OS keeps what it compiles for this helper in ``<here>/<executable name>/``."""
    return Path.home() / "Library" / "Caches"


def _park(src: Path, reason: str) -> bool:
    """Move ``src`` out of the active path: never deletes, never overwrites, never follows a
    symlink (a link is moved as a link), never retries.

    The destination is reserved first. ``mkdtemp`` creates ``<name>.<reason>-<UTC>-<random>``
    beside ``src`` (owner-only, a name nobody holds) and ``src`` is renamed to ``<that>/<name>``:
    inside a directory that is empty by construction, so the rename has nothing to replace and an
    earlier park is never touched. Where ``_PARKED_GENERATIONS`` of those already exist for this
    (name, reason) nothing is parked (``park_cap_reached``). False means ``src`` is still where it
    was and the caller must not use it: there was nothing to move, the cap was reached, or the one
    attempt failed (``park_failed``; a container it made is removed with ``rmdir``, which cannot
    take content with it).
    """
    container: Path | None = None
    try:
        try:
            src.lstat()  # lstat, not exists(): a dangling or planted symlink is something to move
        except FileNotFoundError:
            return False
        prefix = f"{src.name}.{reason}-"
        if sum(name.startswith(prefix) for name in os.listdir(src.parent)) >= _PARKED_GENERATIONS:
            logger.warning("apple vision will not park %s: park_cap_reached", src.name)
            return False
        utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        container = Path(tempfile.mkdtemp(prefix=f"{prefix}{utc}-", dir=src.parent))
        container.chmod(_PRIVATE)  # mkdtemp's 0700 goes through the umask: 0o277 leaves 0500
        os.rename(src, container / src.name)
    except OSError:
        if container is not None:
            with contextlib.suppress(OSError):
                os.rmdir(container)
        logger.warning("apple vision could not park %s: park_failed", src.name)
        return False
    return True


def _warm_up(binary: Path) -> None:
    """Prove the helper still reads text, paying the OS cold start while doing it."""
    live = _vision_cache_parent() / _BINARY_NAME
    if os.path.lexists(live) and not _ours_alone(live, stat.S_IFDIR) and not _park(live, "unsafe"):
        raise AppleVisionUnavailable("cache_unsafe")  # the OS cache others can write is not loaded
    started = time.monotonic()
    if not _canary(binary):
        if not (_park(live, "stale") and _canary(binary)):  # the retry is a cold start again
            raise AppleVisionUnavailable("canary_unreadable")
        logger.warning("apple vision read nothing; its OS cache was parked and it reads again")
    logger.info("apple vision ready; self-test took %.1fs", time.monotonic() - started)


# ---------------------------------------------------------------------------
# Readiness: build + warm once per process, off the event loop
# ---------------------------------------------------------------------------


class _Engine:
    """Process-wide readiness of the helper, shared by every page and every thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._binary: Path | None = None
        self._preparing = False
        self._failure: tuple[str, float] | None = None  # (code, monotonic retry-after)

    def poll(self) -> Path | None:
        """The ready binary; None while a background prepare is running (started if idle)."""
        with self._lock:
            if self._binary is not None:
                return self._binary
            if self._failure is not None:
                code, retry_after = self._failure
                if time.monotonic() < retry_after:
                    raise AppleVisionUnavailable(code)
                self._failure = None
            if not self._preparing:
                self._preparing = True
                threading.Thread(
                    target=self._prepare, name="apple-vision-prepare", daemon=True
                ).start()
            return None

    def mark_cold(self) -> None:
        """Forget readiness: the next page re-warms in the background (cache purged?)."""
        with self._lock:
            self._binary = None

    def _prepare(self) -> None:
        binary: Path | None = None
        code = ""
        try:
            binary = _build_binary()
            _warm_up(binary)
        except AppleVisionUnavailable as exc:
            binary, code = None, str(exc)
        except Exception as exc:  # a thread must never die with the state half-written
            binary, code = None, f"prepare_crashed_{type(exc).__name__}"
        with self._lock:
            self._preparing = False
            if binary is not None:
                self._binary, self._failure = binary, None
            else:
                self._binary = None
                self._failure = (code, time.monotonic() + _FAILURE_TTL_SECONDS)
                logger.warning(
                    "apple vision unavailable (%s); retry in %.0fs", code, _FAILURE_TTL_SECONDS
                )


_engine = _Engine()
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def reset_state() -> None:
    """Forget readiness, failures and gates. Tests only: production state is process-long."""
    global _engine
    _engine = _Engine()
    _gates.clear()
    _build_key.cache_clear()


async def prepare(wait: float | None = None) -> Path:
    """Ensure the helper is compiled and warm; return its path.

    Starts the background prepare on first use. ``wait`` bounds how long THIS caller
    waits (None = until it finishes or fails); on expiry it gets AppleVisionUnavailable
    while the prepare carries on for the next caller.
    """
    deadline = None if wait is None else time.monotonic() + wait
    while True:
        binary = _engine.poll()
        if binary is not None:
            return binary
        if deadline is not None and time.monotonic() >= deadline:
            raise AppleVisionUnavailable("preparing")
        await asyncio.sleep(_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _inflight_slot() -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Semaphore(max_inflight())
    async with gate:
        yield


async def _reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def recognize(image_bytes: bytes, timeout: float) -> dict[str, Any]:
    """OCR one page image. Returns ``{"width", "height", "observations": [...]}``.

    ``timeout`` bounds the helper run only; waiting for a free slot and for the one-time
    warm-up are outside it. Raises AppleVisionUnavailable (use another engine) or
    AppleVisionError (it ran and failed). Never logs or raises with recognised text.
    """
    binary = await prepare(wait=_READY_GRACE_SECONDS)
    async with _inflight_slot():
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                str(binary),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            _engine.mark_cold()  # binary vanished or unrunnable: rebuild + re-warm
            raise AppleVisionUnavailable(f"spawn_{type(exc).__name__}") from None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(image_bytes), timeout=timeout)
        except asyncio.TimeoutError:
            _engine.mark_cold()  # a purged OS cache looks exactly like this
            raise AppleVisionError("timeout") from None
        finally:
            await _reap(process)
    result = _parse(stdout, process.returncode)
    logger.debug(
        "apple vision recognized observations=%d ms=%d",
        len(result["observations"]),
        int((time.monotonic() - started) * 1000),
    )
    return result
