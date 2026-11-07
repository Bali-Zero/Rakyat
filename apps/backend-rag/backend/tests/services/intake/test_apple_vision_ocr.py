"""Apple Vision OCR engine: row grouping, build cache, readiness, recognition protocol.

Linux-CI safe: the Swift helper and the Xcode toolchain are replaced by tiny executable
scripts that speak the same stdin/stdout protocol. The last test compiles and runs the
real helper and is skipped anywhere but a Mac with the Xcode toolchain.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import errno
import hashlib
import io
import json
import logging
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.services.intake import apple_vision_ocr as av

_SHEBANG = "#!/usr/bin/env python3\n"
_REAL_CANARY_PAGE = av._canary_page  # the fixture below swaps in a marker for the fake helpers

# Answers with one fixed line plus the byte count it was fed as a second observation.
_HELPER_OK = (
    _SHEBANG
    + """import json, os, sys
data = sys.stdin.buffer.read()
if data == b"CANARY":  # the warm-up page: read its digits back, like a healthy Vision
    print(json.dumps({"ok": True, "width": 1, "height": 1, "observations": [
        {"text": os.environ["FAKE_CANARY_TEXT"], "confidence": 1.0, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.1}]}))
    sys.exit(0)
print(json.dumps({"ok": True, "width": 10, "height": 20, "observations": [
    {"text": "PASPOR X1234567", "confidence": 1.0, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.05},
    {"text": str(len(data)), "confidence": 1.0, "x": 0.1, "y": 0.3, "w": 0.1, "h": 0.05}]}))
"""
)
_HELPER_REFUSES = (
    _SHEBANG
    + """import json, sys
sys.stdin.buffer.read()
print(json.dumps({"ok": False, "error": "decode_failed"}))
sys.exit(2)
"""
)
_HELPER_SLOW = _SHEBANG + "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(30)\n"
# Appends S/E markers to the file named by $HELPER_LOG, so a test can see how runs overlap.
_HELPER_LOGGING = (
    _SHEBANG
    + """import json, os, sys, time
sys.stdin.buffer.read()
log = open(os.environ["HELPER_LOG"], "a")
log.write("S\\n"); log.flush(); time.sleep(0.3); log.write("E\\n"); log.close()
print(json.dumps({"ok": True, "width": 1, "height": 1, "observations": []}))
"""
)
# Prints a good canary answer, then exits 9 or is killed ($HELPER_ENDING = exit | kill).
_HELPER_THEN_DIES = (
    _SHEBANG
    + """import json, os, signal, sys
sys.stdin.buffer.read()
print(json.dumps({"ok": True, "width": 1, "height": 1, "observations": [
    {"text": os.environ["FAKE_CANARY_TEXT"], "confidence": 1.0, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.1}]}), flush=True)
if os.environ["HELPER_ENDING"] == "kill":
    os.kill(os.getpid(), signal.SIGKILL)
sys.exit(9)
"""
)
# Sees nothing while the directory named by $LIVE_CACHE exists: a stale OS cache, simulated.
_HELPER_STALE = (
    _SHEBANG
    + """import json, os, sys
sys.stdin.buffer.read()
seen = [] if os.path.isdir(os.environ["LIVE_CACHE"]) else [
    {"text": os.environ["FAKE_CANARY_TEXT"], "confidence": 1.0, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.1}]
print(json.dumps({"ok": True, "width": 1, "height": 1, "observations": seen}))
"""
)

# What an attacker would leave in a cache directory they can write.
_TROJAN = "#!/bin/sh\n# TROJAN\n"
_TROJAN_RUNS = _SHEBANG + 'import os\nopen(os.environ["TROJAN_MARKER"], "w").write("ran")\n'

# A stand-in for `/usr/bin/xcrun swiftc ... -o <out> <src>`; behaviour via $FAKE_XCRUN_MODE.
_FAKE_XCRUN = (
    _SHEBANG
    + """import os, sys, time
out = sys.argv[sys.argv.index("-o") + 1]
open(os.environ["FAKE_XCRUN_COUNTER"], "a").write(os.path.basename(out) + "\\n")
mode = os.environ.get("FAKE_XCRUN_MODE", "ok")
if mode == "slow":
    time.sleep(0.4)
if mode == "fail":
    sys.stderr.write("error: simulated compile failure\\n")
    sys.exit(1)
body = os.environ.get("FAKE_XCRUN_BODY")  # a file whose content becomes the "compiled" helper
open(out, "w").write(open(body).read() if body else "#!/usr/bin/env python3\\nprint('{}')\\n")
"""
)


def _script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _isolated_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(av, "_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(av, "_vision_cache_parent", lambda: tmp_path / "caches")
    monkeypatch.setattr(av, "_canary_page", lambda: b"CANARY")
    monkeypatch.setenv("FAKE_CANARY_TEXT", av._CANARY_TEXT)
    monkeypatch.delenv("INTAKE_APPLE_VISION_MAX_INFLIGHT", raising=False)
    av.reset_state()
    yield
    av.reset_state()


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Install a fake helper binary as the build result; returns a setter for its body."""
    path = tmp_path / "apple-vision-ocr"

    def install(body: str = _HELPER_OK) -> Path:
        _script(path, body)
        monkeypatch.setattr(av, "_build_binary", lambda: path)
        return path

    install()
    install.path = path  # type: ignore[attr-defined]
    return install


def _obs(text: str, x: float, y: float, h: float, w: float = 0.1) -> dict:
    return {"text": text, "confidence": 1.0, "x": x, "y": y, "w": w, "h": h}


# ---------------------------------------------------------------------------
# group_rows (pure)
# ---------------------------------------------------------------------------


def test_group_rows_orders_rows_top_down_and_words_left_to_right() -> None:
    observations = [
        _obs("second row", 0.1, 0.30, 0.05),
        _obs("right", 0.6, 0.10, 0.05),
        _obs("left", 0.1, 0.11, 0.05),
    ]
    assert av.group_rows(observations) == "left right\nsecond row"


def test_group_rows_keeps_tightly_stacked_lines_apart() -> None:
    # Innocence: lines 0.021 apart with 0.012 boxes never overlap -> never merge.
    observations = [_obs(f"line {i}", 0.1, 0.1 + i * 0.021, 0.012) for i in range(5)]
    assert av.group_rows(observations).split("\n") == [f"line {i}" for i in range(5)]


def test_group_rows_joins_a_line_that_drifts_by_a_fraction_of_its_height() -> None:
    # A skewed photo: the right-hand fragment sits 0.004 lower than the left one (height 0.012).
    observations = [_obs("NAMA", 0.1, 0.300, 0.012), _obs("CONTOH", 0.6, 0.304, 0.012)]
    assert av.group_rows(observations) == "NAMA CONTOH"


def test_group_rows_needs_strictly_more_than_half_overlap() -> None:
    # Dyadic numbers: the arithmetic is exact. Overlap 0.125 of 0.25 is exactly one half.
    exactly_half = [_obs("a", 0.0, 0.5, 0.25), _obs("b", 0.5, 0.625, 0.25)]
    more_than_half = [_obs("a", 0.0, 0.5, 0.25), _obs("b", 0.5, 0.5625, 0.25)]
    assert av.group_rows(exactly_half) == "a\nb"
    assert av.group_rows(more_than_half) == "a b"


def test_group_rows_skips_blank_and_malformed_observations() -> None:
    observations = [
        _obs("  ", 0.1, 0.1, 0.05),
        {"text": "no box"},
        {"text": "bad box", "x": "left", "y": 0.2, "h": 0.05},
        {"text": "nan box", "x": float("nan"), "y": 0.2, "h": 0.05},
        {"text": "huge box", "x": 10**400, "y": 0.2, "h": 0.05},  # float() overflows
        {"text": "inf box", "x": 0.1, "y": float("inf"), "h": 0.05},
        {"text": "bool box", "x": True, "y": 0.2, "h": 0.05},
        {"text": None, "x": 0.1, "y": 0.3, "h": 0.05},
        _obs("  kept  ", 0.1, 0.4, 0.05),
    ]
    assert av.group_rows(observations) == "kept"
    assert av.group_rows([]) == ""


# ---------------------------------------------------------------------------
# available() / env knobs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "shim", "expected"),
    [("linux", True, False), ("darwin", False, False), ("darwin", True, True)],
)
def test_available_needs_macos_and_the_xcode_shim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, shim: bool, expected: bool
) -> None:
    monkeypatch.setattr(av, "_platform", lambda: platform)
    xcrun = _script(tmp_path / "xcrun", "#!/bin/sh\n") if shim else tmp_path / "missing-xcrun"
    monkeypatch.setattr(av, "_XCRUN", str(xcrun))
    assert av.available() is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 2), ("1", 1), ("4", 4), ("0", 1), ("-3", 1), ("99", 8), ("many", 2)],
)
def test_max_inflight_is_bounded(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int
) -> None:
    if raw is not None:
        monkeypatch.setenv("INTAKE_APPLE_VISION_MAX_INFLIGHT", raw)
    assert av.max_inflight() == expected


# ---------------------------------------------------------------------------
# Helper output validation
# ---------------------------------------------------------------------------


def test_parse_accepts_an_ok_answer_from_a_clean_exit() -> None:
    raw = json.dumps({"ok": True, "width": 3, "height": 4, "observations": [_obs("a", 0, 0, 1)]})
    assert av._parse(raw.encode(), 0) == {
        "width": 3,
        "height": 4,
        "observations": [_obs("a", 0, 0, 1)],
    }


def test_parse_accepts_a_box_that_overhangs_the_page_edge_by_a_hair() -> None:
    # Innocence for the window: Vision's boxes are not clipped to exactly 0..1.
    observations = [_obs("edge", -0.0004, 0.9996, 0.0004, w=1.0004), _obs("tiny", 0.5, 0.5, 1e-6)]
    raw = json.dumps({"ok": True, "width": 1, "height": 1, "observations": observations})
    assert av._parse(raw.encode(), 0)["observations"] == observations


@pytest.mark.parametrize("returncode", [1, 2, 9, 255, -9, -11, None])
def test_parse_never_believes_a_success_from_a_process_that_did_not_exit_cleanly(
    returncode: int | None,
) -> None:
    answer = json.dumps(
        {"ok": True, "width": 1, "height": 1, "observations": [_obs("a", 0.1, 0.1, 0.05)]}
    )
    with pytest.raises(av.AppleVisionError) as excinfo:
        av._parse(answer.encode(), returncode)
    assert excinfo.value.reason == "nonzero_exit"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"", "no_json_exit_2"),
        (b"not json", "no_json_exit_2"),
        (b"[1, 2]", "bad_shape"),
        (b'{"ok": false, "error": "empty_input"}', "engine_empty_input"),
        (b'{"ok": false, "error": "decode_failed"}', "engine_decode_failed"),
        (b'{"ok": false, "error": "vision_failed"}', "engine_vision_failed"),
        (b'{"ok": false, "error": "encode_failed"}', "engine_encode_failed"),
        (b'{"ok": false}', "engine_unknown"),
        (b'{"ok": false, "error": 7}', "engine_unknown"),
        # Anything outside the helper's closed set is never echoed, however short or tidy.
        (b'{"ok": false, "error": "x1234567"}', "engine_unknown"),
        (b'{"ok": false, "error": "Passport X1234567 unreadable"}', "engine_unknown"),
    ],
)
def test_parse_rejects_with_a_fixed_reason_code(raw: bytes, reason: str) -> None:
    with pytest.raises(av.AppleVisionError) as excinfo:
        av._parse(raw, 2)
    assert excinfo.value.reason == reason
    assert str(excinfo.value) == reason


_BOX = '"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.05'


@pytest.mark.parametrize(
    "observations",
    [
        "7",  # not an object
        "null",
        '{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.05}',  # no text
        f'{{"text": 5, {_BOX}}}',  # text is not a string
        '{"text": "a", "x": 1' + "0" * 400 + ', "y": 0.1, "w": 0.5, "h": 0.05}',  # 10**400
        '{"text": "a", "x": NaN, "y": 0.1, "w": 0.5, "h": 0.05}',  # json.loads accepts these
        '{"text": "a", "x": 0.1, "y": Infinity, "w": 0.5, "h": 0.05}',
        '{"text": "a", "x": 0.1, "y": 0.1, "w": -Infinity, "h": 0.05}',
        '{"text": "a", "x": true, "y": 0.1, "w": 0.5, "h": 0.05}',  # a bool is not a number
        '{"text": "a", "x": "0.1", "y": 0.1, "w": 0.5, "h": 0.05}',
        '{"text": "a", "x": 0.1, "y": null, "w": 0.5, "h": 0.05}',
        '{"text": "a", "x": 0.1, "y": 0.1, "w": 0.5, "h": -0.05}',  # negative height
        '{"text": "a", "x": 0.1, "y": 0.1, "w": 0.5, "h": 0}',  # no extent
        '{"text": "a", "x": 0.1, "y": 0.1, "w": 0, "h": 0.05}',
        '{"text": "a", "x": 5, "y": 0.1, "w": 0.5, "h": 0.05}',  # outside the page
        '{"text": "a", "x": 0.1, "y": 0.1, "w": 0.5, "h": 7}',
        '{"text": "a", "x": 0.1, "y": -3, "w": 0.5, "h": 0.05}',
    ],
)
def test_parse_rejects_a_response_with_one_malformed_observation(observations: str) -> None:
    """The stricter reading: the bad observation is not dropped, the whole response is refused."""
    raw = f'{{"ok": true, "width": 1, "height": 1, "observations": [{{"text": "ok", {_BOX}}}, {observations}]}}'
    with pytest.raises(av.AppleVisionError) as excinfo:
        av._parse(raw.encode(), 0)
    assert excinfo.value.reason == "bad_observations"


@pytest.mark.parametrize("raw", ['{"ok": true}', '{"ok": true, "observations": "text"}'])
def test_parse_rejects_a_success_without_an_observation_list(raw: str) -> None:
    with pytest.raises(av.AppleVisionError) as excinfo:
        av._parse(raw.encode(), 0)
    assert excinfo.value.reason == "bad_observations"


def test_parse_survives_an_answer_nested_past_the_recursion_limit() -> None:
    with pytest.raises(av.AppleVisionError, match="no_json_exit_0"):
        av._parse(b"[" * 200_000, 0)


# ---------------------------------------------------------------------------
# Build: compile once, cache by hash, remember failures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_xcrun(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    counter = tmp_path / "xcrun-calls"
    monkeypatch.setattr(av, "_XCRUN", str(_script(tmp_path / "fake-xcrun", _FAKE_XCRUN)))
    monkeypatch.setenv("FAKE_XCRUN_COUNTER", str(counter))
    monkeypatch.delenv("FAKE_XCRUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_XCRUN_BODY", raising=False)

    def calls() -> list[str]:
        """The basename of the -o target of every compile so far."""
        return counter.read_text().splitlines() if counter.exists() else []

    return calls


def test_build_compiles_once_into_a_directory_named_by_the_source_hash(
    fake_xcrun, tmp_path: Path
) -> None:
    first = av._build_binary()
    second = av._build_binary()

    expected = hashlib.sha256(av._SWIFT_SOURCE.read_bytes())
    expected.update(b"\0" + "\0".join(av._compile_flags()).encode())
    assert first == second == tmp_path / "cache" / expected.hexdigest()[:16] / "apple-vision-ocr"
    assert os.access(first, os.X_OK)
    assert len(fake_xcrun()) == 1
    assert sorted(p.name for p in first.parent.iterdir()) == [".lock", "apple-vision-ocr"]


def test_the_compiler_output_keeps_the_final_basename_whatever_the_process(
    monkeypatch: pytest.MonkeyPatch, fake_xcrun, tmp_path: Path
) -> None:
    """The linker derives the code signature's Identifier from the -o file name, and the OS's
    Vision cache is keyed on that Identifier (60-150 s cold start). A temp name with a pid in it
    would make every rebuild a never-seen identity: the exact bug this pins."""
    first = av._build_binary()
    monkeypatch.setattr(av, "_cache_root", lambda: tmp_path / "second-cache")
    second = av._build_binary()

    assert first != second  # two real builds, in two directories
    assert fake_xcrun() == ["apple-vision-ocr", "apple-vision-ocr"]
    for built in (first, second):  # the private temp dir is gone, nothing is left behind
        assert [p.name for p in built.parent.iterdir() if p.name.startswith(".build-")] == []


def test_build_key_follows_the_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = av._build_key()
    changed = tmp_path / "other.swift"
    changed.write_text("// a different helper\n")
    monkeypatch.setattr(av, "_SWIFT_SOURCE", changed)
    av._build_key.cache_clear()
    assert av._build_key() != before


def test_build_failure_is_remembered_for_ten_minutes(
    monkeypatch: pytest.MonkeyPatch, fake_xcrun
) -> None:
    monkeypatch.setenv("FAKE_XCRUN_MODE", "fail")

    with pytest.raises(av.AppleVisionUnavailable, match="compile_exit_1"):
        av._build_binary()
    with pytest.raises(av.AppleVisionUnavailable, match="compile_failed_recently"):
        av._build_binary()
    assert len(fake_xcrun()) == 1  # the second call never reached the compiler

    marker = av._cache_root() / av._build_key() / av._FAILURE_MARKER
    aged = time.time() - av._FAILURE_TTL_SECONDS - 5
    os.utime(marker, (aged, aged))
    with pytest.raises(av.AppleVisionUnavailable, match="compile_exit_1"):
        av._build_binary()
    assert len(fake_xcrun()) == 2  # after the TTL it tries again


def test_concurrent_builds_compile_exactly_once(
    monkeypatch: pytest.MonkeyPatch, fake_xcrun
) -> None:
    monkeypatch.setenv("FAKE_XCRUN_MODE", "slow")
    results: list[Path] = []

    def build() -> None:
        results.append(av._build_binary())

    threads = [threading.Thread(target=build) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4 and len(set(results)) == 1
    assert len(fake_xcrun()) == 1


def test_build_reports_an_unusable_cache_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file in the way")
    monkeypatch.setattr(av, "_cache_root", lambda: blocker / "cache")
    with pytest.raises(av.AppleVisionUnavailable, match="cache_unusable"):
        av._build_binary()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o277])
def test_created_directories_are_owner_only_whatever_the_umask(tmp_path: Path, mask: int) -> None:
    """mkdir's mode is masked by the umask and parents get the default mode: 0o277 leaves 0o500."""
    target = tmp_path / "a" / "b" / "c"
    previous = os.umask(mask)
    try:
        av._make_private_dirs(target)
    finally:
        os.umask(previous)
    assert [_mode(p) for p in (target.parent.parent, target.parent, target)] == [0o700] * 3


def test_a_directory_is_born_owner_only_not_fixed_up_afterwards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Passing the mode to mkdir closes the window in which a 0777 directory exists before the chmod."""
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    previous = os.umask(0)
    try:
        av._make_private_dirs(tmp_path / "a" / "b")
    finally:
        os.umask(previous)
    assert [_mode(tmp_path / "a"), _mode(tmp_path / "a" / "b")] == [0o700, 0o700]


def test_a_build_under_a_permissive_umask_leaves_only_owner_only_paths(
    monkeypatch: pytest.MonkeyPatch, fake_xcrun, tmp_path: Path
) -> None:
    root = tmp_path / "levels" / "cache"  # the parent has to be created too
    monkeypatch.setattr(av, "_cache_root", lambda: root)
    previous = os.umask(0)
    try:
        binary = av._build_binary()
    finally:
        os.umask(previous)
    assert [_mode(p) for p in (root.parent, root, binary.parent, binary)] == [0o700] * 4


def test_a_cached_executable_in_a_chain_only_we_can_write_is_reused(fake_xcrun) -> None:
    first = av._build_binary()
    first.chmod(0o755)  # readable and runnable by others is fine; only WRITE decides
    assert av._build_binary() == first and len(fake_xcrun()) == 1


@pytest.mark.parametrize(
    "flaw",
    [
        "directory_group_writable",
        "executable_other_writable",
        "directory_is_a_symlink",
        "executable_is_a_symlink",
    ],
)
def test_a_cached_executable_in_an_unsafe_place_is_parked_and_rebuilt(
    fake_xcrun, tmp_path: Path, flaw: str
) -> None:
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    directory = root / av._build_key()
    elsewhere = tmp_path / "elsewhere"  # a place somebody else controls
    elsewhere.mkdir(mode=0o700)
    if flaw == "directory_is_a_symlink":
        _script(elsewhere / "apple-vision-ocr", _TROJAN)
        directory.symlink_to(elsewhere)
    else:
        directory.mkdir(mode=0o700)
        if flaw == "executable_is_a_symlink":
            _script(elsewhere / "apple-vision-ocr", _TROJAN)
            (directory / "apple-vision-ocr").symlink_to(elsewhere / "apple-vision-ocr")
        else:
            _script(directory / "apple-vision-ocr", _TROJAN)
        if flaw == "directory_group_writable":
            directory.chmod(0o770)
        if flaw == "executable_other_writable":
            (directory / "apple-vision-ocr").chmod(0o757)

    binary = av._build_binary()

    assert binary == directory / "apple-vision-ocr" and "TROJAN" not in binary.read_text()
    assert len(fake_xcrun()) == 1  # rebuilt, not reused
    assert _mode(directory) == 0o700 and not directory.is_symlink()
    (container,) = root.glob(f"{av._build_key()}.unsafe-*")
    parked = container / av._build_key()  # the key directory (or the link to it), moved whole
    assert os.path.lexists(parked / "apple-vision-ocr")  # kept as evidence, not deleted


@pytest.mark.parametrize(
    "flaw",
    [
        "root_group_writable",
        "parent_group_writable",
        "root_is_a_symlink",
        "root_owned_by_someone_else",
    ],
)
def test_a_cache_root_that_is_not_ours_alone_makes_the_engine_unavailable(
    monkeypatch: pytest.MonkeyPatch, fake_xcrun, tmp_path: Path, flaw: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    root = parent / "cache"
    if flaw == "root_is_a_symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        root.symlink_to(tmp_path / "elsewhere")
    else:
        root.mkdir(mode=0o700)
    if flaw == "root_group_writable":
        root.chmod(0o770)
    if flaw == "parent_group_writable":
        parent.chmod(0o770)
    if flaw == "root_owned_by_someone_else":
        me = os.getuid()
        monkeypatch.setattr(av.os, "getuid", lambda: me + 1)
    monkeypatch.setattr(av, "_cache_root", lambda: root)

    with pytest.raises(av.AppleVisionUnavailable, match="cache_unsafe"):
        av._build_binary()
    assert fake_xcrun() == []  # nothing was compiled or run
    assert list(root.iterdir()) == []  # and nothing was created in a place others can write


async def test_an_executable_planted_in_a_writable_directory_never_runs(
    monkeypatch: pytest.MonkeyPatch, fake_xcrun, tmp_path: Path
) -> None:
    """End to end: build, self-test and a page, with a trojan waiting in the cache directory."""
    marker = tmp_path / "trojan-ran"
    monkeypatch.setenv("TROJAN_MARKER", str(marker))
    monkeypatch.setenv("FAKE_XCRUN_BODY", str(_script(tmp_path / "good-helper", _HELPER_OK)))
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    directory = root / av._build_key()
    directory.mkdir()
    directory.chmod(0o777)
    _script(directory / "apple-vision-ocr", _TROJAN_RUNS)

    binary = await av.prepare(wait=60)
    result = await av.recognize(b"abc", timeout=20)

    assert not marker.exists()
    assert binary.read_text() == _HELPER_OK and result["observations"]


@pytest.mark.parametrize("flaw", ["group_writable", "symlink"])
def test_an_os_cache_directory_others_can_write_is_parked_before_the_helper_runs(
    tmp_path: Path, helper, flaw: str
) -> None:
    caches = tmp_path / "caches"
    caches.mkdir()
    live = caches / "apple-vision-ocr"
    if flaw == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        live.symlink_to(tmp_path / "elsewhere")
    else:
        (live / "bundle").mkdir(parents=True)
        live.chmod(0o777)

    av._warm_up(helper(_HELPER_OK))

    (container,) = caches.glob("apple-vision-ocr.unsafe-*")
    assert os.path.lexists(container / "apple-vision-ocr") and not os.path.lexists(live)


def test_pillow_is_imported_lazily_inside_the_warm_up_page_only() -> None:
    """Fly runs Linux and never draws a warm-up page: importing this module must not need Pillow."""
    tree = ast.parse(Path(av.__file__).read_text())
    imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    assert not [n for n in imports if "PIL" in ast.dump(n)]


def test_the_warm_up_page_is_a_png_of_dark_digits_on_white() -> None:
    from PIL import Image

    with Image.open(io.BytesIO(_REAL_CANARY_PAGE())) as page:
        assert page.size == (900, 220)
        assert page.convert("L").getextrema() == (0, 255)


def test_a_healthy_helper_passes_the_self_test_and_its_cache_is_left_alone(
    helper, tmp_path
) -> None:
    live = tmp_path / "caches" / "apple-vision-ocr"
    live.mkdir(parents=True)
    live.chmod(0o755)  # what the OS creates: readable by others is fine, writable is not
    av._warm_up(helper(_HELPER_OK))
    assert live.is_dir()
    with pytest.raises(av.AppleVisionUnavailable, match="warmup_engine_decode_failed"):
        av._warm_up(helper(_HELPER_REFUSES))


@pytest.mark.parametrize("heals", [True, False])
def test_a_blind_helper_has_its_cache_parked_once_then_is_trusted_or_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helper, heals: bool
) -> None:
    live = tmp_path / "caches" / "apple-vision-ocr"
    (live / "bundle").mkdir(parents=True)
    live.chmod(0o755)
    monkeypatch.setenv("LIVE_CACHE", str(live if heals else tmp_path))  # blind until moved / always
    binary = helper(_HELPER_STALE)
    if heals:
        av._warm_up(binary)
    else:
        with pytest.raises(av.AppleVisionUnavailable, match="canary_unreadable"):
            av._warm_up(binary)
    (container,) = (tmp_path / "caches").glob("apple-vision-ocr.stale-*")
    assert (container / "apple-vision-ocr" / "bundle").is_dir() and not live.exists()
    assert _mode(container) == 0o700  # what is kept for inspection is private too


def test_parking_never_deletes_and_stops_at_five_generations(tmp_path: Path) -> None:
    caches = tmp_path / "caches"
    live = caches / "apple-vision-ocr"
    assert av._park(live, "stale") is False  # nothing to move
    live.mkdir(parents=True)
    for generation in range(5):
        (caches / f"apple-vision-ocr.stale-{generation}").mkdir()
    assert av._park(live, "stale") is False
    assert live.is_dir()  # the live directory stayed where it was
    assert av._park(live, "unsafe") is True  # each kind has its own five


# ---------------------------------------------------------------------------
# Parking: one helper, a container reserved first, never over an earlier park
# ---------------------------------------------------------------------------

_STAMP = "20261103T000000Z"  # the second in which a name made of seconds gets taken twice
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _freeze_the_park_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stamp every park of the test with _STAMP, so a scheme that names by the second collides."""
    real = time.strftime
    monkeypatch.setattr(
        time, "strftime", lambda fmt, *rest: _STAMP if fmt == _STAMP_FORMAT else real(fmt, *rest)
    )


def _round2_park(path: Path, kind: str) -> bool:
    """Round 2's _park, verbatim: the defect the collision tests below are written to catch."""
    stamp = time.strftime(_STAMP_FORMAT, time.gmtime())
    parked = path.with_name(f"{path.name}.{kind}-{stamp}")
    try:
        if len(list(path.parent.glob(f"{path.name}.{kind}-*"))) >= av._PARKED_GENERATIONS:
            return False
        path.rename(parked)
    except OSError:
        return False
    if parked.is_dir() and not parked.is_symlink():
        with contextlib.suppress(OSError):
            parked.chmod(0o700)
    return True


def _make(path: Path, kind: str, targets: Path) -> None:
    """Something to park, or to find already parked: a file, a directory holding a file, an empty
    directory, a symlink to a directory holding a file, or a symlink to nothing (dangling)."""
    if kind == "file":
        path.write_text(f"bytes of {path.name}")
    elif kind == "dir":
        path.mkdir()
        (path / "payload").write_text(f"bytes of {path.name}")
    elif kind == "empty_dir":
        path.mkdir()
    elif kind == "symlink":
        (targets / path.name).mkdir()
        (targets / path.name / "keep").write_text("the link's target")
        path.symlink_to(targets / path.name)
    else:
        path.symlink_to(targets / "nowhere")


def _fingerprint(path: Path) -> tuple[int, int, object]:
    """Inode, type and content (bytes, link target or entries) of ``path``, never following a link."""
    info = path.lstat()
    kind = stat.S_IFMT(info.st_mode)
    if kind == stat.S_IFLNK:
        content: object = os.readlink(path)
    elif kind == stat.S_IFDIR:
        content = sorted(os.listdir(path))
    else:
        content = path.read_bytes()
    return info.st_ino, kind, content


def _collision_problems(park, base: Path, kind: str, obstacle: str) -> list[str]:
    """What is wrong when ``kind`` is parked while ``obstacle`` already holds the name a
    second-precision scheme would give the park. Empty: it landed in a container of its own and the
    earlier park is untouched (same inode, same bytes, link target or entries)."""
    caches, targets = base / "caches", base / "targets"
    caches.mkdir()
    targets.mkdir()
    live, taken = caches / "apple-vision-ocr", caches / f"apple-vision-ocr.stale-{_STAMP}"
    _make(live, kind, targets)
    _make(taken, obstacle, targets)
    artifact, earlier = _fingerprint(live), _fingerprint(taken)

    problems = []
    if park(live, "stale") is not True:
        problems.append("did not park")
    if not os.path.lexists(taken) or _fingerprint(taken) != earlier:
        problems.append("touched the earlier park")
    containers = [p for p in caches.glob(f"apple-vision-ocr.stale-{_STAMP}-*") if p.is_dir()]
    if len(containers) != 1:
        problems.append(f"{len(containers)} containers")
    else:
        parked = containers[0] / "apple-vision-ocr"
        if not os.path.lexists(parked) or _fingerprint(parked) != artifact:
            problems.append("artifact not intact in its container")
        if _mode(containers[0]) != 0o700:
            problems.append("container not owner-only")
    if os.path.lexists(live):
        problems.append("left the artifact in place")
    return problems


@pytest.mark.parametrize("obstacle", ["file", "symlink", "empty_dir"])
@pytest.mark.parametrize("kind", ["dir", "file", "symlink"])
def test_a_park_lands_in_a_container_of_its_own_never_on_an_earlier_park(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, obstacle: str
) -> None:
    _freeze_the_park_stamp(monkeypatch)
    assert _collision_problems(av._park, tmp_path, kind, obstacle) == []


@pytest.mark.parametrize("obstacle", ["file", "symlink", "empty_dir"])
@pytest.mark.parametrize("kind", ["dir", "file", "symlink"])
def test_round_2_park_fails_the_collision_test_it_was_written_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, obstacle: str
) -> None:
    """The revert proof: the same scenario, run against round 2's _park, finds a defect."""
    _freeze_the_park_stamp(monkeypatch)
    assert _collision_problems(_round2_park, tmp_path, kind, obstacle) != []


@pytest.mark.parametrize(
    ("kind", "obstacle"), [("file", "file"), ("file", "symlink"), ("dir", "empty_dir")]
)
def test_round_2_park_reported_success_while_replacing_an_earlier_park(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, obstacle: str
) -> None:
    """Not a refusal: its rename succeeded over a file, over a symlink and over an empty directory."""
    _freeze_the_park_stamp(monkeypatch)
    problems = _collision_problems(_round2_park, tmp_path, kind, obstacle)
    assert "touched the earlier park" in problems and "did not park" not in problems


def _spy_on_rename(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    real = os.rename

    def spy(src, dst, *args, **kwargs):
        calls.append((str(src), str(dst)))
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", spy)
    return calls


def _make_rename_fail(monkeypatch: pytest.MonkeyPatch, error: int = errno.EXDEV) -> list[str]:
    attempts: list[str] = []

    def failing(src, dst, *args, **kwargs):
        attempts.append(str(dst))
        raise OSError(error, "injected")

    monkeypatch.setattr(os, "rename", failing)
    return attempts


def test_parks_in_the_same_second_get_containers_of_their_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Five parks within one second (one stamp) are five containers: nothing is replaced."""
    _freeze_the_park_stamp(monkeypatch)
    caches, targets = tmp_path / "caches", tmp_path / "targets"
    caches.mkdir()
    targets.mkdir()
    live = caches / "apple-vision-ocr"
    for generation in range(av._PARKED_GENERATIONS):  # the fifth is still allowed
        _make(live, "dir", targets)
        assert av._park(live, "stale") is True, generation
    containers = list(caches.glob(f"apple-vision-ocr.stale-{_STAMP}-*"))
    assert len(containers) == av._PARKED_GENERATIONS
    assert all((c / "apple-vision-ocr" / "payload").is_file() for c in containers)


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o277])
def test_the_container_is_owner_only_whatever_the_umask(tmp_path: Path, mask: int) -> None:
    """mkdtemp's 0700 goes through the umask: 0o277 would leave 0500, and nothing can be moved in."""
    caches, targets = tmp_path / "caches", tmp_path / "targets"
    caches.mkdir()
    targets.mkdir()
    live = caches / "apple-vision-ocr"
    _make(live, "dir", targets)
    previous = os.umask(mask)
    try:
        parked = av._park(live, "stale")
    finally:
        os.umask(previous)
    assert parked is True
    (container,) = caches.glob("apple-vision-ocr.stale-*")
    assert _mode(container) == 0o700


def test_at_the_cap_nothing_is_parked_nothing_is_deleted_and_the_refusal_is_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caches, targets = tmp_path / "caches", tmp_path / "targets"
    caches.mkdir()
    targets.mkdir()
    live = caches / "apple-vision-ocr"
    for generation in range(av._PARKED_GENERATIONS):
        _make(live, "dir", targets)
        assert av._park(live, "stale") is True, generation
    _make(live, "dir", targets)
    before = {p.name: _fingerprint(p) for p in caches.iterdir()}
    renames = _spy_on_rename(monkeypatch)
    caplog.set_level(logging.INFO)

    assert av._park(live, "stale") is False

    assert renames == []  # no rename was even attempted
    assert "park_cap_reached" in caplog.text and "park_failed" not in caplog.text
    assert {p.name: _fingerprint(p) for p in caches.iterdir()} == before  # all still there
    assert (live / "payload").is_file()
    assert av._park(live, "unsafe") is True  # the five are counted per reason


def test_nothing_to_move_reserves_nothing_and_says_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caches = tmp_path / "caches"
    caches.mkdir()
    reserved: list[int] = []
    real = av.tempfile.mkdtemp
    monkeypatch.setattr(av.tempfile, "mkdtemp", lambda *a, **k: reserved.append(1) or real(*a, **k))
    caplog.set_level(logging.INFO)

    assert av._park(caches / "apple-vision-ocr", "stale") is False

    assert reserved == [] and list(caches.iterdir()) == [] and "park_" not in caplog.text


def test_a_rename_that_fails_is_one_attempt_and_leaves_no_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caches, targets = tmp_path / "caches", tmp_path / "targets"
    caches.mkdir()
    targets.mkdir()
    live = caches / "apple-vision-ocr"
    _make(live, "dir", targets)
    artifact = _fingerprint(live)
    attempts = _make_rename_fail(monkeypatch)
    caplog.set_level(logging.INFO)

    assert av._park(live, "stale") is False

    assert len(attempts) == 1  # one attempt, no retry
    assert "park_failed" in caplog.text and "park_cap_reached" not in caplog.text
    assert _fingerprint(live) == artifact  # the artifact stayed where it was
    assert [p.name for p in caches.iterdir()] == ["apple-vision-ocr"]  # the empty container is gone


def test_a_container_that_cannot_be_reserved_is_one_attempt_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caches, targets = tmp_path / "caches", tmp_path / "targets"
    caches.mkdir()
    targets.mkdir()
    live = caches / "apple-vision-ocr"
    _make(live, "dir", targets)
    artifact = _fingerprint(live)
    attempts: list[int] = []

    def refusing(*args, **kwargs):
        attempts.append(1)
        raise PermissionError(errno.EACCES, "injected")

    monkeypatch.setattr(av.tempfile, "mkdtemp", refusing)
    caplog.set_level(logging.INFO)

    assert av._park(live, "stale") is False

    assert len(attempts) == 1 and "park_failed" in caplog.text
    assert _fingerprint(live) == artifact and [p.name for p in caches.iterdir()] == [live.name]


def test_the_container_of_a_failed_park_goes_only_if_it_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """rmdir, not rmtree: whatever a half-done move left in the container is kept."""
    caches, targets = tmp_path / "caches", tmp_path / "targets"
    caches.mkdir()
    targets.mkdir()
    live = caches / "apple-vision-ocr"
    _make(live, "dir", targets)
    artifact = _fingerprint(live)

    def fails_after_something_landed(src, dst, *args, **kwargs):
        (Path(dst).parent / "left-behind").write_text("evidence")
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(os, "rename", fails_after_something_landed)

    assert av._park(live, "stale") is False

    (container,) = caches.glob("apple-vision-ocr.stale-*")
    assert (container / "left-behind").read_text() == "evidence"
    assert _fingerprint(live) == artifact


@pytest.mark.parametrize("target", ["directory", "nothing"])
def test_a_symlink_is_parked_as_a_symlink_and_its_target_is_left_alone(
    tmp_path: Path, target: str
) -> None:
    caches, elsewhere = tmp_path / "caches", tmp_path / "elsewhere"
    caches.mkdir()
    if target == "directory":
        elsewhere.mkdir()
        (elsewhere / "keep").write_text("not ours to touch")
        elsewhere.chmod(0o755)
    live = caches / "apple-vision-ocr"
    live.symlink_to(elsewhere)
    link = _fingerprint(live)

    assert av._park(live, "unsafe") is True

    (container,) = caches.glob("apple-vision-ocr.unsafe-*")
    assert _fingerprint(container / "apple-vision-ocr") == link  # the link itself, not its target
    assert not os.path.lexists(live)
    if target == "directory":
        assert (elsewhere / "keep").read_text() == "not ours to touch"
        assert _mode(elsewhere) == 0o755  # not chmod-ed through the link
    else:
        assert not os.path.lexists(elsewhere)  # a dangling link is parked, not "nothing to move"


async def test_a_refused_park_leaves_the_engine_unavailable_so_pages_take_ollama(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helper
) -> None:
    """The cap reached on a stale OS cache: the cache stays, the engine says no, and `recognize`
    raises AppleVisionUnavailable, which classify turns into the Ollama chain (see
    test_every_non_read_falls_back_to_ollama)."""
    caches = tmp_path / "caches"
    live = caches / "apple-vision-ocr"
    (live / "bundle").mkdir(parents=True)
    live.chmod(0o755)
    for generation in range(av._PARKED_GENERATIONS):
        (caches / f"apple-vision-ocr.stale-held-{generation}").mkdir()
    monkeypatch.setenv("LIVE_CACHE", str(live))  # the helper is blind while the cache is there
    helper(_HELPER_STALE)

    with pytest.raises(av.AppleVisionUnavailable, match="canary_unreadable"):
        await av.prepare(wait=30)
    with pytest.raises(av.AppleVisionUnavailable, match="canary_unreadable"):
        await av.recognize(b"abc", timeout=5)

    assert (live / "bundle").is_dir()
    assert len(list(caches.glob("apple-vision-ocr.stale-*"))) == av._PARKED_GENERATIONS


def test_a_park_that_fails_never_lets_the_os_cache_be_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helper
) -> None:
    marker = tmp_path / "helper-ran"
    monkeypatch.setenv("TROJAN_MARKER", str(marker))
    live = tmp_path / "caches" / "apple-vision-ocr"
    live.mkdir(parents=True)
    live.chmod(0o777)  # others can write it: it is not loaded, and here it cannot be parked either
    binary = helper(_TROJAN_RUNS)  # would leave a marker if it ran
    attempts = _make_rename_fail(monkeypatch)

    with pytest.raises(av.AppleVisionUnavailable, match="cache_unsafe"):
        av._warm_up(binary)

    assert len(attempts) == 1 and not marker.exists() and live.is_dir()


def test_an_unsafe_executable_that_cannot_be_parked_is_not_run_and_nothing_is_built_over_it(
    fake_xcrun, tmp_path: Path
) -> None:
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    directory = root / av._build_key()
    directory.mkdir()
    directory.chmod(0o777)
    _script(directory / "apple-vision-ocr", _TROJAN)
    for generation in range(av._PARKED_GENERATIONS):
        (root / f"{av._build_key()}.unsafe-held-{generation}").mkdir()

    with pytest.raises(av.AppleVisionUnavailable, match="cache_unsafe"):
        av._build_binary()

    assert fake_xcrun() == []  # nothing was compiled
    assert "TROJAN" in (directory / "apple-vision-ocr").read_text()  # and it is where it was


_DELETING_CALLS = frozenset({"rmtree", "remove", "removedirs", "unlink", "rmdir", "unlinkat"})
_MOVING_CALLS = frozenset({"rename", "renames", "replace", "move"})


def _calls(source: str, names: frozenset[str]) -> list[tuple[str, str, tuple[str, ...]]]:
    """(enclosing function, callee as written, arguments as written) of every call in ``source``
    whose last name is in ``names``: ``os.rmdir(container)`` in ``_park`` is
    ``("_park", "os.rmdir", ("container",))``."""
    owner: dict[ast.Call, str] = {}
    for function in ast.walk(ast.parse(source)):
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    owner[node] = function.name  # an inner function is visited later and wins
    found = []
    for call, function in owner.items():
        callee = ast.unparse(call.func)
        if callee.rsplit(".", 1)[-1] in names:
            found.append((function, callee, tuple(ast.unparse(arg) for arg in call.args)))
    return sorted(found)


def _park_path(source: str) -> set[str]:
    """``_park`` and every function that calls it."""
    callers = set()
    for function in ast.walk(ast.parse(source)):
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_park":
                    callers.add(function.name)
    return {"_park"} | callers


_GUILTY_PARKER = """
def _park(src, reason):
    container = Path(tempfile.mkdtemp(dir=src.parent))
    os.rename(src, container / src.name)
    shutil.rmtree(container)
    src.replace(container)


def _warm_up(binary):
    if not _park(live, "stale"):
        live.unlink()
        os.remove(live)
        live.rmdir()


def _elsewhere():
    shutil.rmtree(cache)
"""


def test_the_park_path_scan_flags_every_deleting_and_moving_call_it_is_meant_to_catch() -> None:
    on_path = _park_path(_GUILTY_PARKER)
    assert on_path == {"_park", "_warm_up"}  # _elsewhere neither parks nor calls _park
    flagged = {
        (fn, callee) for fn, callee, _ in _calls(_GUILTY_PARKER, _DELETING_CALLS) if fn in on_path
    }
    assert flagged == {
        ("_park", "shutil.rmtree"),
        ("_warm_up", "live.unlink"),
        ("_warm_up", "os.remove"),
        ("_warm_up", "live.rmdir"),
    }
    assert [(fn, callee) for fn, callee, _ in _calls(_GUILTY_PARKER, _MOVING_CALLS)] == [
        ("_park", "os.rename"),
        ("_park", "src.replace"),
    ]


def test_nothing_on_the_real_park_path_deletes_but_the_empty_container_rmdir() -> None:
    source = Path(av.__file__).read_text()
    on_path = _park_path(source)
    assert on_path == {"_park", "_build_binary", "_warm_up"}  # every caller goes through _park
    assert [c for c in _calls(source, _DELETING_CALLS) if c[0] in on_path] == [
        ("_park", "os.rmdir", ("container",))
    ]
    assert _calls(source, _MOVING_CALLS) == [
        ("_compile", "scratch.replace", ("binary",)),  # the build's own atomic install, not a park
        ("_park", "os.rename", ("src", "container / src.name")),
    ]
    park = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_park"
    )
    assert not [node for node in ast.walk(park) if isinstance(node, ast.For | ast.While)]


def test_every_deleting_call_of_the_module_is_listed_and_none_names_a_parked_artifact() -> None:
    """The inventory: _compile removes its own build scratch and the failure marker of the directory
    it builds in; a parked artifact sits in a container beside the old path, which none of these
    calls names. A new deleting call anywhere in the module has to be added here on purpose."""
    source = Path(av.__file__).read_text()
    assert _calls(source, _DELETING_CALLS) == [
        ("_compile", "(directory / _FAILURE_MARKER).unlink", ()),
        ("_compile", "scratch.unlink", ()),
        ("_compile", "workdir.rmdir", ()),
        ("_park", "os.rmdir", ("container",)),
    ]


@pytest.mark.parametrize("ending", ["exit", "kill"])
async def test_a_helper_that_dies_after_a_good_answer_is_not_believed(
    monkeypatch: pytest.MonkeyPatch, helper, ending: str
) -> None:
    monkeypatch.setenv("HELPER_ENDING", ending)
    with pytest.raises(av.AppleVisionUnavailable, match="warmup_nonzero_exit"):
        av._warm_up(helper(_HELPER_THEN_DIES))  # the self-test judges with the same parser

    monkeypatch.setattr(av, "_warm_up", lambda binary: None)
    with pytest.raises(av.AppleVisionError) as excinfo:
        await av.recognize(b"abc", timeout=20)
    assert excinfo.value.reason == "nonzero_exit"


def test_warm_up_has_its_own_budget(monkeypatch: pytest.MonkeyPatch, helper) -> None:
    monkeypatch.setattr(av, "_WARMUP_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(av.AppleVisionUnavailable, match="warmup_timeout"):
        av._warm_up(helper(_HELPER_SLOW))
    assert time.monotonic() - started < 10


# ---------------------------------------------------------------------------
# Readiness: build + warm once per process, never blocking a page past the grace
# ---------------------------------------------------------------------------


async def test_pages_are_told_to_wait_while_the_warm_up_runs_then_it_is_ready(
    monkeypatch: pytest.MonkeyPatch, helper
) -> None:
    release = threading.Event()
    warm_ups: list[Path] = []

    def slow_warm_up(binary: Path) -> None:
        warm_ups.append(binary)
        release.wait(timeout=30)

    monkeypatch.setattr(av, "_warm_up", slow_warm_up)
    try:
        for _ in range(3):  # concurrent pages share ONE warm-up
            with pytest.raises(av.AppleVisionUnavailable, match="preparing"):
                await av.prepare(wait=0.1)
    finally:
        release.set()

    assert await av.prepare(wait=10) == helper.path
    assert warm_ups == [helper.path]


async def test_a_failed_prepare_is_remembered_then_retried_after_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def broken() -> Path:
        attempts.append(1)
        raise av.AppleVisionUnavailable("compile_exit_1")

    monkeypatch.setattr(av, "_build_binary", broken)
    with pytest.raises(av.AppleVisionUnavailable, match="compile_exit_1"):
        await av.prepare(wait=10)
    with pytest.raises(av.AppleVisionUnavailable, match="compile_exit_1"):
        await av.prepare(wait=0)
    assert len(attempts) == 1  # the second call did not start another prepare

    code, _ = av._engine._failure
    av._engine._failure = (code, time.monotonic() - 1)  # the TTL has run out
    with pytest.raises(av.AppleVisionUnavailable, match="compile_exit_1"):
        await av.prepare(wait=10)
    assert len(attempts) == 2


async def test_an_unexpected_crash_while_preparing_is_contained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash() -> Path:
        raise ZeroDivisionError

    monkeypatch.setattr(av, "_build_binary", crash)
    with pytest.raises(av.AppleVisionUnavailable, match="prepare_crashed_ZeroDivisionError"):
        await av.prepare(wait=10)


# ---------------------------------------------------------------------------
# recognize(): protocol, failure modes, gate
# ---------------------------------------------------------------------------


async def test_recognize_pipes_the_page_on_stdin_and_returns_the_observations(helper) -> None:
    result = await av.recognize(b"abc", timeout=20)

    assert result["width"] == 10 and result["height"] == 20
    assert [o["text"] for o in result["observations"]] == ["PASPOR X1234567", "3"]  # 3 bytes seen
    assert av.group_rows(result["observations"]) == "PASPOR X1234567\n3"


async def test_a_refusal_from_the_helper_is_an_error_not_a_crash(helper) -> None:
    await av.prepare(wait=10)  # warm while the helper is still good
    helper(_HELPER_REFUSES)
    with pytest.raises(av.AppleVisionError) as excinfo:
        await av.recognize(b"abc", timeout=20)
    assert excinfo.value.reason == "engine_decode_failed"

    helper(_HELPER_OK)  # one bad page does not poison the engine
    assert (await av.recognize(b"abc", timeout=20))["observations"]


async def test_a_timeout_kills_the_helper_and_sends_the_engine_back_to_warm_up(
    monkeypatch: pytest.MonkeyPatch, helper
) -> None:
    helper(_HELPER_SLOW)
    warm_ups: list[Path] = []
    monkeypatch.setattr(av, "_warm_up", lambda binary: warm_ups.append(binary))
    await av.prepare(wait=10)

    started = time.monotonic()
    with pytest.raises(av.AppleVisionError) as excinfo:
        await av.recognize(b"abc", timeout=0.5)
    assert excinfo.value.reason == "timeout"
    assert time.monotonic() - started < 10  # killed, not waited out (the helper sleeps 30 s)

    await av.prepare(wait=10)  # cold again -> a fresh warm-up, then ready
    assert len(warm_ups) == 2


async def test_a_vanished_binary_is_unavailable_and_triggers_a_rebuild(
    monkeypatch: pytest.MonkeyPatch, helper
) -> None:
    builds: list[int] = []
    path = helper.path

    def build() -> Path:
        builds.append(1)
        return path

    monkeypatch.setattr(av, "_build_binary", build)
    await av.prepare(wait=10)
    path.unlink()
    with pytest.raises(av.AppleVisionUnavailable, match="spawn_FileNotFoundError"):
        await av.recognize(b"abc", timeout=5)

    _script(path, _HELPER_OK)
    assert (await av.recognize(b"abc", timeout=20))["observations"]
    assert len(builds) == 2


async def test_the_inflight_gate_serialises_helpers_when_set_to_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helper
) -> None:
    log = tmp_path / "helper.log"
    monkeypatch.setenv("HELPER_LOG", str(log))
    monkeypatch.setenv("INTAKE_APPLE_VISION_MAX_INFLIGHT", "1")
    monkeypatch.setattr(av, "_warm_up", lambda binary: None)  # keep the warm-up out of the log
    helper(_HELPER_LOGGING)

    await asyncio.gather(*(av.recognize(b"x", timeout=20) for _ in range(3)))

    assert log.read_text().split() == ["S", "E"] * 3  # never two helpers at once


async def test_recognised_text_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture, helper
) -> None:
    caplog.set_level(logging.DEBUG)
    result = await av.recognize(b"abc", timeout=20)
    assert "PASPOR X1234567" in av.group_rows(result["observations"])  # it WAS recognised

    helper(_HELPER_REFUSES)
    with pytest.raises(av.AppleVisionError):
        await av.recognize(b"abc", timeout=20)
    assert "X1234567" not in caplog.text


# ---------------------------------------------------------------------------
# The real helper, on a Mac with the Xcode toolchain (skipped everywhere else)
# ---------------------------------------------------------------------------

_REAL_TOOLCHAIN = sys.platform == "darwin" and Path("/usr/bin/xcrun").exists()


@pytest.mark.skipif(not _REAL_TOOLCHAIN, reason="needs macOS with the Xcode toolchain")
async def test_real_helper_compiles_and_reads_a_synthetic_identifier_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiles apple_vision_ocr.swift for real and OCRs a synthetic (non-PII) image.

    On a Mac that never ran the helper under this name, prepare() pays the one-time Vision
    cold start (1-3 minutes); afterwards the whole test takes seconds.
    """
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.load_default(size=64)
    except TypeError:
        pytest.skip("Pillow too old for a scalable default font")
    image = Image.new("RGB", (1200, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 50), "PASPOR X1234567", fill="black", font=font)
    png = io.BytesIO()
    image.save(png, format="PNG")

    monkeypatch.setattr(av, "_canary_page", _REAL_CANARY_PAGE)  # the real self-test, too
    await av.prepare()
    result = await av.recognize(png.getvalue(), timeout=60)

    assert (result["width"], result["height"]) == (1200, 360)
    assert av.group_rows(result["observations"]).replace(" ", "") == "PASPORX1234567"
