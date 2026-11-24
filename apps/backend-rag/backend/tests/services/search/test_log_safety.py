"""The request-scoped redaction backstop: whatever a client library logs while a /api/search
request is in flight, the user's query (and free-form collection) is replaced in the record."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.setup.logging_config import DevelopmentFormatter, StructuredFormatter
from backend.services.search import log_safety
from backend.services.search.log_safety import (
    install_query_log_redaction,
    query_log_scope,
    redact_query,
)

knowledge_router = importlib.import_module("backend.app.modules.knowledge.router")

install_query_log_redaction()
CANARY = "uji-sintetis-kueri-3WK8Q"
FOREIGN = logging.getLogger("backend.core.qdrant_db")


def _all_text(caplog: pytest.LogCaptureFixture) -> str:
    parts: list[str] = []
    for r in caplog.records:
        parts += [r.getMessage(), str(r.args), r.exc_text or ""]
        parts += [str(v) for k, v in r.__dict__.items() if k not in ("args", "msg", "exc_info")]
        if r.exc_info and r.exc_info[1] is not None:
            parts.append(str(r.exc_info[1]))
    return "\n".join(parts)


def test_a_foreign_logger_inside_the_scope_is_redacted_everywhere(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG), query_log_scope(f"{CANARY} kueri panjang"):
        try:
            raise RuntimeError(f"HTTP 400 for {CANARY} kueri panjang")
        except RuntimeError:
            FOREIGN.error(
                "qdrant failed on %s",
                f"{CANARY} kueri panjang",
                extra={"error": f"body quoted {CANARY} kueri panjang"},
                exc_info=True,
            )
        FOREIGN.debug(f"f-string leak {CANARY} kueri panjang")
    text = _all_text(caplog)
    assert "qdrant failed on" in text and "f-string leak" in text, "records never emitted: vacuous"
    assert CANARY not in text
    assert "[query]" in text


def test_outside_the_scope_nothing_is_touched(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        FOREIGN.error("plain %s", CANARY)
    assert CANARY in _all_text(caplog)


def test_short_texts_are_ignored_and_numeric_args_survive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG), query_log_scope("ab", f"{CANARY} lengkap"):
        FOREIGN.info("ab stays, %d results in %.1fms for %s", 3, 1.5, f"{CANARY} lengkap")
    text = _all_text(caplog)
    assert "ab stays, 3 results in 1.5ms for [query]" in text


def test_redact_query_handles_none_and_whitespace_variants() -> None:
    assert redact_query("x", None) == "x"
    assert (
        redact_query("a  b   c here", ("a b c here",)) == "a  b   c here"
    )  # not collapsed in text
    assert redact_query("see a b c here now", ("a  b c here",)) == "see [query] now"


class _Boom:
    async def search(self, **kwargs: Any) -> dict[str, Any]:
        try:
            raise RuntimeError(
                f"upstream rejected {kwargs['query']} / {kwargs['collection_override']}"
            )
        except RuntimeError:
            FOREIGN.error(
                "upstream error for %s",
                kwargs["query"],
                extra={"coll": kwargs["collection_override"]},
                exc_info=True,
            )
            raise


def test_end_to_end_through_the_route_nothing_reaches_a_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.state.search_service = _Boom()
    client = TestClient(app, raise_server_exceptions=False)
    collection = "koleksi-sintetis-5ZP2"
    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            "/api/search",
            json={"query": f"{CANARY} kueri", "collection": collection, "level": 0, "limit": 2},
        )
    assert resp.status_code == 500
    text = "\n".join(_all_text(caplog).splitlines())
    assert "upstream error for" in text, "the foreign record never reached the handler: vacuous"
    assert CANARY not in text and collection not in text


# ---------------------------------------------------------------------------------------------
# Spec v3 section 3 and section 6: record fields, exceptions, failure, innocence, context reach.
# Every case below asserts through the observer (a snapshot of each record taken at `handle`).
# ---------------------------------------------------------------------------------------------

SENT = "zq-sentinel-7f3a91c2"
TEXT = f"{SENT} kueri panjang"
MINIMAL = "[log record withheld: redaction failed]"


@pytest.fixture(autouse=True)
def _foreign_logger_emits_everything():
    old = FOREIGN.level
    FOREIGN.setLevel(logging.DEBUG)
    yield
    FOREIGN.setLevel(old)


@contextlib.contextmanager
def _record_factory(**attrs: Any) -> Iterator[None]:
    """Records built with extra attributes already set, as a foreign factory would."""
    old = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old(*args, **kwargs)
        for key, value in attrs.items():
            setattr(record, key, value)
        return record

    logging.setLogRecordFactory(factory)
    try:
        yield
    finally:
        logging.setLogRecordFactory(old)


def _raise_in_scope(logger: logging.Logger, text: str) -> None:
    try:
        raise RuntimeError(f"upstream quoted {text}")
    except RuntimeError:
        logger.error("failed upstream", exc_info=True)


@pytest.mark.parametrize("level", [logging.DEBUG, logging.ERROR], ids=["debug", "error"])
def test_foreign_logger_at_any_level_leaves_no_sentinel_in_any_attribute(
    caplog: pytest.LogCaptureFixture, observer: Any, level: int
) -> None:
    with caplog.at_level(logging.DEBUG), query_log_scope(TEXT):
        FOREIGN.log(level, "upstream said %s", TEXT)
    assert observer.named("upstream said [query]"), "the record never reached handle: vacuous"
    assert SENT not in observer.blob()


def test_percent_s_and_mapping_args_are_rendered_then_redacted(observer: Any) -> None:
    with query_log_scope(TEXT):
        FOREIGN.warning("tuple %s and %d", TEXT, 7)
        FOREIGN.warning("mapping %(q)s", {"q": TEXT})
    assert observer.named("tuple [query] and 7"), "tuple-args record missing: vacuous"
    assert observer.named("mapping [query]"), "mapping-args record missing: vacuous"
    assert SENT not in observer.blob()
    assert all(s["args"] == () for s in observer.snapshots)


def test_extras_are_walked_recursively_and_objects_become_their_type_name(observer: Any) -> None:
    extra = {
        "top": f"top {TEXT}",
        "nested": {"a": [1, {"b": (f"deep {TEXT}", None)}]},
        "boom": ValueError(f"quoted {TEXT}"),
        "blob": object(),
    }
    with query_log_scope(TEXT):
        FOREIGN.error("with extras", extra=extra)
    (snap,) = observer.named("with extras")
    assert SENT not in observer.blob()
    assert snap["top"] == "top [query]"
    assert snap["nested"] == {"a": [1, {"b": ("deep [query]", None)}]}
    assert snap["boom"] == "ValueError"
    assert snap["blob"] == "object"


def test_standard_string_attributes_are_redacted_too(observer: Any) -> None:
    named = logging.getLogger(f"search.{SENT}")
    with query_log_scope(TEXT, SENT + "x"), query_log_scope(SENT + " ", TEXT):
        record = named.makeRecord(
            named.name,
            logging.ERROR,
            f"/srv/{SENT}.py",
            3,
            "msg",
            (),
            None,
            func=f"fn_{SENT}",
            sinfo=f"Stack: frame {SENT}",
        )
    flat = repr(record.__dict__)
    assert SENT not in flat, "a standard string attribute kept the sentinel"
    assert record.name == "search.[query]" or "[query]" in record.name
    assert "[query]" in record.funcName and "[query]" in record.stack_info


def test_a_cached_exc_text_without_exc_info_lands_redacted_in_the_message(observer: Any) -> None:
    with _record_factory(exc_text=f"Traceback cached\nValueError: {TEXT}", message=f"stale {TEXT}"):
        with query_log_scope(TEXT):
            FOREIGN.error("plain failure")
    (snap,) = observer.named("plain failure")
    assert "ValueError: [query]" in snap["__rendered__"]
    assert snap["exc_text"] is None
    assert "message" not in snap
    assert SENT not in observer.blob()


def test_exc_info_becomes_a_sanitized_traceback_in_the_message(observer: Any) -> None:
    with query_log_scope(TEXT):
        _raise_in_scope(FOREIGN, TEXT)
    (snap,) = observer.named("failed upstream")
    assert snap["exc_info"] is None and snap["exc_text"] is None
    assert "Traceback (most recent call last)" in snap["__rendered__"]
    assert "_raise_in_scope" in snap["__rendered__"], "frames lost: debugging would suffer"
    assert "RuntimeError: upstream quoted [query]" in snap["__rendered__"]
    assert SENT not in observer.blob()


def _formatted(formatter: logging.Formatter, run: Any) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    sink = logging.getLogger("backend.sink.search_log_safety")
    sink.setLevel(logging.DEBUG)
    sink.propagate = False
    sink.addHandler(handler)
    try:
        with query_log_scope(TEXT):
            run(sink)
    finally:
        sink.removeHandler(handler)
    return stream.getvalue()


@pytest.mark.parametrize(
    "formatter", [StructuredFormatter(), DevelopmentFormatter()], ids=["production", "development"]
)
def test_production_and_development_output_carry_the_frames_but_not_the_text(
    formatter: logging.Formatter,
) -> None:
    out = _formatted(formatter, lambda sink: _raise_in_scope(sink, TEXT))
    assert "failed upstream" in out, "nothing was formatted: vacuous"
    assert "_raise_in_scope" in out, "the traceback frames did not reach the sink"
    assert SENT not in out


def test_install_twice_wraps_once(monkeypatch: pytest.MonkeyPatch) -> None:
    before = logging.Logger.makeRecord
    install_query_log_redaction()
    monkeypatch.setattr(log_safety, "_installed", False)  # as after a module reload
    install_query_log_redaction()
    assert logging.Logger.makeRecord is before
    wrappers, fn = 0, logging.Logger.makeRecord
    while fn is not None:
        wrappers += bool(getattr(fn, log_safety._MARKER, False))
        fn = getattr(fn, "__wrapped__", None)
    assert wrappers == 1


def test_unrelated_values_keep_their_type_and_structure_while_the_sentinel_goes(
    observer: Any,
) -> None:
    benign = {
        "n": 7,
        "f": 1.5,
        "yes": True,
        "no": False,
        "none": None,
        "txt": "ab stays because it is short",
        "nested": {"k": [1, (2, "benign"), {"z": None}], "t": ("x", 3.0)},
    }
    with query_log_scope(TEXT, "ab"):
        FOREIGN.info("count=%d flag=%s ab", 3, True, extra={**benign, "leak": TEXT})
    (snap,) = observer.named("count=3 flag=True ab")
    for key, value in benign.items():
        assert snap[key] == value and type(snap[key]) is type(value), key
    assert snap["leak"] == "[query]"
    assert SENT not in observer.blob()


def test_the_registration_threshold_is_four_characters_per_variant(observer: Any) -> None:
    with query_log_scope("abc"):
        FOREIGN.info("abc is three characters")
    with query_log_scope("abcd"):
        FOREIGN.info("abcd is four characters")
    with query_log_scope("  ab  "):  # stripped form is 2 characters: skipped; original kept
        FOREIGN.info("x  ab  y and ab alone")
    with query_log_scope("  alpha   beta  "):
        FOREIGN.info("one alpha   beta two")
        FOREIGN.info("one alpha beta two")
        FOREIGN.info("one   alpha   beta   two")
    rendered = observer.rendered()
    assert "abc is three characters" in rendered
    assert "[query] is four characters" in rendered
    assert "x[query]y and ab alone" in rendered
    assert "one [query] two" in rendered[3:5]
    assert rendered[5] == "one [query] two" or "[query]" in rendered[5]


def test_a_failing_redactor_leaves_exactly_the_fixed_minimal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    real = log_safety._redact_value

    def flaky(value: Any, texts: tuple[str, ...]) -> Any:
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("redactor broke mid-attributes")
        return real(value, texts)

    monkeypatch.setattr(log_safety, "_redact_value", flaky)
    named = logging.getLogger(f"search.{SENT}")
    logging.addLevelName(25, f"LVL_{SENT}")
    try:
        with _record_factory(exc_text=f"cached {TEXT}"), query_log_scope(TEXT):
            record = named.makeRecord(
                named.name,
                25,
                f"/srv/{SENT}.py",
                11,
                f"msg {TEXT}",
                (),
                None,
                func=f"fn_{SENT}",
                extra={"secret": TEXT, "n": 1},
                sinfo=f"Stack {TEXT}",
            )
    finally:
        logging._levelToName.pop(25, None)
        logging._nameToLevel.pop(f"LVL_{SENT}", None)
    d = record.__dict__
    assert calls["n"] > 3, "the redactor never ran far enough to fail: vacuous"
    assert record.msg == MINIMAL and record.args == ()
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert "message" not in d
    assert record.name == "search.log_safety"
    assert record.pathname == record.filename == record.module == record.funcName == "[withheld]"
    assert record.threadName == record.processName == record.taskName == "[withheld]"
    assert set(d) <= set(log_safety._STANDARD_ATTRS), "an extra attribute survived"
    assert record.levelno == 25 and record.levelname == "Level 25"
    assert record.lineno == 11 and record.created > 0
    assert SENT not in repr(d)


@pytest.mark.parametrize(
    ("levelno", "name"),
    [(10, "DEBUG"), (20, "INFO"), (30, "WARNING"), (40, "ERROR"), (50, "CRITICAL")],
)
def test_the_failure_path_names_builtin_levels_from_its_own_table(
    monkeypatch: pytest.MonkeyPatch, levelno: int, name: str
) -> None:
    def broken(value: Any, texts: tuple[str, ...]) -> Any:
        raise RuntimeError("always")

    monkeypatch.setattr(log_safety, "_redact_value", broken)
    with query_log_scope(TEXT):
        record = FOREIGN.makeRecord(FOREIGN.name, levelno, "p.py", 1, "m", (), None)
    assert record.msg == MINIMAL and record.levelname == name


def test_outside_any_scope_the_record_is_untouched(observer: Any) -> None:
    try:
        raise RuntimeError(TEXT)
    except RuntimeError:
        FOREIGN.error("plain %s", TEXT, extra={"k": {"v": TEXT}}, exc_info=True, stack_info=True)
    snap = observer.snapshots[-1]
    assert snap["args"] == (TEXT,) and snap["msg"] == "plain %s"
    assert snap["k"] == {"v": TEXT}
    assert TEXT in snap["exc_info"] and snap["stack_info"]
    assert snap["name"] == FOREIGN.name


def test_after_a_scope_exits_normally_or_by_exception_the_next_record_is_untouched(
    observer: Any,
) -> None:
    with query_log_scope(TEXT):
        pass
    FOREIGN.info("after normal exit %s", TEXT)
    with pytest.raises(KeyError), query_log_scope(TEXT):
        raise KeyError("x")
    FOREIGN.info("after failed exit %s", TEXT)
    assert observer.named(f"after normal exit {TEXT}")
    assert observer.named(f"after failed exit {TEXT}")


@pytest.mark.asyncio
async def test_two_concurrent_scopes_never_redact_each_others_records(observer: Any) -> None:
    other_a, other_b = "zq-sentinel-AAAA1111", "zq-sentinel-BBBB2222"
    ready_a, ready_b = asyncio.Event(), asyncio.Event()

    async def worker(mine: str, theirs: str, ready: asyncio.Event, peer: asyncio.Event) -> None:
        with query_log_scope(mine):
            ready.set()
            await peer.wait()
            FOREIGN.info("mine=%s theirs=%s", mine, theirs)

    await asyncio.gather(
        worker(other_a, other_b, ready_a, ready_b), worker(other_b, other_a, ready_b, ready_a)
    )
    assert observer.named("mine=[query] theirs=" + other_b), "task A record missing: vacuous"
    assert observer.named("mine=[query] theirs=" + other_a), "task B record missing: vacuous"
    assert not observer.named("theirs=[query]")


@pytest.mark.asyncio
async def test_threads_and_tasks_created_inside_the_scope_are_redacted(observer: Any) -> None:
    def in_thread() -> None:
        FOREIGN.info("thread saw %s", TEXT)

    async def in_task() -> None:
        FOREIGN.info("task saw %s", TEXT)

    with query_log_scope(TEXT):
        await asyncio.to_thread(in_thread)
        await asyncio.create_task(in_task())
        async with asyncio.TaskGroup() as group:
            group.create_task(in_task())
    assert observer.named("thread saw [query]") and observer.named("task saw [query]")
    assert SENT not in observer.blob()
