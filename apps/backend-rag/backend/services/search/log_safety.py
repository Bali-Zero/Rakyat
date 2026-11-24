"""Log helpers for the search path: a log line must never carry the user's query.

Exception messages from the vector DB, the embedder or the LLM clients quote their
input, so on this path an exception is logged as its type plus the traceback frames
(what debugging needs) and never as `str(e)` or `exc_info=True`, whose last line is the message.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import traceback
from collections.abc import Iterator
from typing import Any


def exc_summary(exc: BaseException) -> str:
    """Exception type and frames, without the message."""
    return f"{type(exc).__name__}\n{''.join(traceback.format_tb(exc.__traceback__))}"


# --- Request-scoped redaction: the backstop for every logger the search path reaches ------------
#
# Call sites on the path are fixed one by one, but the vector DB, embedder, LLM and cache
# clients log exception messages that quote their input, and they are not ours to rewrite. While
# a request's texts are registered here, EVERY string attribute of every log record created in
# that context (any logger, any level) is redacted, `extra` values recursively, and the
# traceback is folded into the message as redacted text. If redaction itself fails the record
# is replaced by a fixed minimal one. It matches the full text (and its stripped and
# whitespace-collapsed forms); slices of it are handled at the call sites.

_CURRENT_QUERY: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "search_log_redaction_texts", default=()
)
_MIN_REDACTED_LEN = 4
_PLACEHOLDER = "[query]"
_WITHHELD = "[withheld]"
_WITHHELD_MESSAGE = "[log record withheld: redaction failed]"
_WITHHELD_LOGGER = "search.log_safety"
_STANDARD_ATTRS = frozenset(logging.LogRecord("x", 0, "x", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}
_BUILTIN_LEVEL_NAMES = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR", 50: "CRITICAL"}
_MARKER = "_search_log_redaction_wrapper"
_installed = False


def _variants(text: str) -> list[str]:
    """The text, stripped and whitespace-collapsed, each kept only if >= 4 chars, longest first."""
    found = {text, text.strip(), " ".join(text.split())}
    return sorted((v for v in found if len(v) >= _MIN_REDACTED_LEN), key=len, reverse=True)


def redact_query(text: str, queries: str | tuple[str, ...] | None) -> str:
    """Replace each registered text (and its stripped / whitespace-collapsed forms) in `text`."""
    if not queries:
        return text
    if isinstance(queries, str):
        queries = (queries,)
    for query in queries:
        for variant in _variants(query):
            text = text.replace(variant, _PLACEHOLDER)
    return text


@contextlib.contextmanager
def query_log_scope(*texts: str | None) -> Iterator[None]:
    """Register the user-supplied texts (query, free-form collection) for redaction for
    everything logged inside this context. Each text and its stripped and collapsed forms
    is registered only if it is at least 4 characters long."""
    registered: list[str] = []
    for text in texts:
        if text:
            for variant in _variants(text):
                if variant not in registered:
                    registered.append(variant)
    token = _CURRENT_QUERY.set(tuple(registered))
    try:
        yield
    finally:
        _CURRENT_QUERY.reset(token)


def _redact_value(value: Any, texts: tuple[str, ...]) -> Any:
    """Strings redacted; dict, list and tuple walked; numbers, booleans and None kept;
    anything else (an exception object, an arbitrary object) replaced by its type name."""
    if isinstance(value, str):
        return redact_query(value, texts)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {_redact_value(k, texts): _redact_value(v, texts) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, texts) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v, texts) for v in value)
    return type(value).__name__


def _sanitize_record(record: logging.LogRecord, texts: tuple[str, ...]) -> None:
    message = redact_query(record.getMessage(), texts)
    exc_info = record.exc_info
    if exc_info and not isinstance(exc_info, bool) and exc_info[1] is not None:
        trace = "".join(traceback.format_exception(*exc_info))
    else:
        trace = record.exc_text or ""
    if trace:
        message = f"{message}\n{redact_query(trace.rstrip(chr(10)), texts)}"
    record.msg = message
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.__dict__.pop("message", None)
    for key, value in list(record.__dict__.items()):
        if key in ("msg", "args", "exc_info", "exc_text"):
            continue
        if key in _STANDARD_ATTRS and not isinstance(value, str):
            continue
        record.__dict__[key] = _redact_value(value, texts)


def _withhold_record(record: logging.LogRecord) -> None:
    """The fixed minimal record: no text of the original survives, level and clock do."""
    keep = {
        "levelno",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "process",
        "lineno",
    }
    for key in list(record.__dict__):
        if key not in _STANDARD_ATTRS:
            del record.__dict__[key]
        elif key not in keep and isinstance(record.__dict__[key], str):
            record.__dict__[key] = _WITHHELD
    record.__dict__.pop("message", None)
    record.msg = _WITHHELD_MESSAGE
    record.args = ()
    record.exc_info = record.exc_text = record.stack_info = None
    record.name = _WITHHELD_LOGGER
    record.pathname = record.filename = record.module = record.funcName = _WITHHELD
    record.threadName = record.processName = record.taskName = _WITHHELD
    levelno = record.levelno
    record.levelname = _BUILTIN_LEVEL_NAMES.get(levelno, f"Level {levelno}")


def install_query_log_redaction() -> None:
    """Wrap Logger.makeRecord once (idempotent).

    makeRecord, not the record factory: `extra` fields are attached to the record after the
    factory has returned, so only here can they be redacted too.
    """
    global _installed
    if _installed or getattr(logging.Logger.makeRecord, _MARKER, False):
        _installed = True
        return
    original = logging.Logger.makeRecord

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = original(self, *args, **kwargs)
        try:
            texts = _CURRENT_QUERY.get()
            if not texts:
                return record
            try:
                _sanitize_record(record, texts)
            except Exception:
                _withhold_record(record)
        except Exception:  # the wrapper never raises
            pass
        return record

    setattr(make_record, _MARKER, True)
    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]
    _installed = True
