"""Log helpers for the search path: a log line must never carry the user's query.

Exception messages from the vector DB, the embedder or the LLM clients quote their
input, so on this path an exception is logged as its type plus the traceback frames
(what debugging needs) and never as `str(e)` or `exc_info=True`, whose last line is the message.
"""

from __future__ import annotations

import traceback


def exc_summary(exc: BaseException) -> str:
    """Exception type and frames, without the message."""
    return f"{type(exc).__name__}\n{''.join(traceback.format_tb(exc.__traceback__))}"
