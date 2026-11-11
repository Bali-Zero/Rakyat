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
# Call sites on the path are fixed one by one above, but the vector DB, embedder, LLM and cache
# clients log exception messages that quote their input, and they are not ours to rewrite. While
# a request's query is registered here, every log record created in that context (any logger,
# any level) has the query text replaced in its message, its `extra` strings and its traceback
# text. It matches the full query; slices of it are handled at the call sites.

_CURRENT_QUERY: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "search_log_redaction_texts", default=()
)
_MIN_REDACTED_LEN = 4
_PLACEHOLDER = "[query]"
_STANDARD_ATTRS = frozenset(logging.LogRecord("x", 0, "x", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}
_installed = False


def redact_query(text: str, queries: str | tuple[str, ...] | None) -> str:
    """Replace each registered text (and its whitespace-collapsed form) in `text`."""
    if not queries:
        return text
    if isinstance(queries, str):
        queries = (queries,)
    for query in queries:
        for variant in {query, query.strip(), " ".join(query.split())}:
            if len(variant) >= _MIN_REDACTED_LEN:
                text = text.replace(variant, _PLACEHOLDER)
    return text


@contextlib.contextmanager
def query_log_scope(*texts: str | None) -> Iterator[None]:
    """Register the user-supplied texts (query, free-form collection) for redaction for
    everything logged inside this context. Texts shorter than 4 characters are ignored."""
    token = _CURRENT_QUERY.set(tuple(t for t in texts if t and len(t) >= _MIN_REDACTED_LEN))
    try:
        yield
    finally:
        _CURRENT_QUERY.reset(token)


def install_query_log_redaction() -> None:
    """Wrap Logger.makeRecord once (idempotent).

    makeRecord, not the record factory: `extra` fields are attached to the record after the
    factory has returned, so only here can they be redacted too.
    """
    global _installed
    if _installed:
        return
    original = logging.Logger.makeRecord

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = original(self, *args, **kwargs)
        texts = _CURRENT_QUERY.get()
        if not texts:
            return record
        try:
            record.msg = redact_query(record.getMessage(), texts)
            record.args = ()
            for key, value in list(record.__dict__.items()):
                if key not in _STANDARD_ATTRS and isinstance(value, str):
                    record.__dict__[key] = redact_query(value, texts)
            if record.exc_info and record.exc_info[1] is not None:
                text = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = redact_query(text, texts)
                # The exception object still carries the original message: handlers that read
                # it directly (Sentry's logging integration) would ship it. The redacted
                # traceback text above is what is kept.
                record.exc_info = None
        except Exception:
            record.msg = "[log record withheld: redaction failed]"
            record.args = ()
            record.exc_info = None
        return record

    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]
    _installed = True
