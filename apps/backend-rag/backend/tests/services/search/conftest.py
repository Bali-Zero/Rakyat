"""An observer for the search log guards: it sees every record that reaches a logger's `handle`,
non-propagating loggers included, snapshots it (a deep copy of its attributes plus the rendered
message), and then delegates to the saved original so handlers, filters and Sentry still run."""

from __future__ import annotations

import copy
import logging
import traceback
from typing import Any

import pytest


def snapshot_record(record: logging.LogRecord) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key == "exc_info":
            snap[key] = (
                "".join(traceback.format_exception(*value))
                if value and not isinstance(value, bool) and value[1] is not None
                else value
            )
            continue
        try:
            snap[key] = copy.deepcopy(value)
        except Exception:
            snap[key] = repr(value)
    try:
        snap["__rendered__"] = record.getMessage()
    except Exception as exc:  # a broken record must still be searched
        snap["__rendered__"] = (
            f"<unrenderable {type(exc).__name__}: {record.msg!r} {record.args!r}>"
        )
    return snap


def flatten(value: Any) -> str:
    """Every string anywhere in `value` (keys, values, nested), plus reprs of other objects."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(flatten(k) + "\n" + flatten(v) for k, v in value.items())
    if isinstance(value, list | tuple | set | frozenset):
        return "\n".join(flatten(v) for v in value)
    return repr(value)


class RecordObserver:
    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []

    def blob(self) -> str:
        return "\n".join(flatten(s) for s in self.snapshots)

    def rendered(self) -> list[str]:
        return [s["__rendered__"] for s in self.snapshots]

    def named(self, needle: str) -> list[dict[str, Any]]:
        return [s for s in self.snapshots if needle in s["__rendered__"]]


@pytest.fixture
def observer(monkeypatch: pytest.MonkeyPatch) -> RecordObserver:
    obs = RecordObserver()
    original = logging.Logger.handle

    def handle(self: logging.Logger, record: logging.LogRecord) -> None:
        obs.snapshots.append(snapshot_record(record))
        return original(self, record)

    monkeypatch.setattr(logging.Logger, "handle", handle)
    return obs
