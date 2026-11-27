"""API logging boundary, installed after opting in through configure_logging()."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from copy import copy
from datetime import date, time
from decimal import Decimal
from pathlib import PurePath
from typing import Any
from uuid import UUID

from backend.app.setup.sentry_config import (
    PII_REDACTION_PLACEHOLDER,
    _is_credential_header_key,
    _is_pii_key,
    _is_search_param,
    _redact_string,
)
from backend.core.secret_log_redaction import redact_query_values

_STANDARD = vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
_MAX_DEPTH = 12

# Typed flags emitted by InvoiceIssuerService after an external delivery attempt.
_DELIVERY_FLAGS = frozenset(
    {
        "email_attempted",
        "email_sent",
        "accounting_notified",
        "drive_uploaded",
        "delivery_may_have_occurred",
    }
)


def _sensitive(key: Any, value: Any) -> bool:
    if type(value) is bool and isinstance(key, str) and key in _DELIVERY_FLAGS:
        return False
    # The CRM uses short integer row IDs as its public-safe correlation reference.
    # Patterned external client IDs and long identifier-shaped numbers stay masked.
    if key == "client_id" and type(value) is int and 0 < value <= 2_147_483_647:
        return False
    # Preserve the CLI reporting boundary's existing redaction marker.
    if isinstance(value, str) and value == "<withheld>":
        return False
    return (
        _is_pii_key(key)
        or _is_credential_header_key(key)
        or (isinstance(value, str) and _is_search_param(key))
    )


def scrub_log_value(value: Any) -> Any:
    """Copy JSON-like fields before rendering. Bound both nesting and repeated references."""
    remaining = 2048

    def walk(item: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise ValueError("log field exceeds item limit")
        if depth >= _MAX_DEPTH:
            return PII_REDACTION_PLACEHOLDER
        if isinstance(item, str):
            return _redact_string(item)
        if isinstance(item, Mapping):
            return {
                walk(k, depth + 1): PII_REDACTION_PLACEHOLDER
                if _sensitive(k, v)
                else walk(v, depth + 1)
                for k, v in item.items()
            }
        if isinstance(item, (tuple, list)):
            items = [walk(v, depth + 1) for v in item]
            return tuple(items) if isinstance(item, tuple) else items
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, (date, time, Decimal, PurePath, UUID)):
            return _redact_string(str(item))
        return PII_REDACTION_PLACEHOLDER

    return walk(value, 0)


def _sanitize(record: logging.LogRecord) -> None:
    # Use the original argument types for numeric/width conversions. Walk structured
    # arguments before interpolation, while sensitive keys are still distinguishable.
    args = record.args
    is_access = record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5
    if is_access:
        client, method, target, version, status = args
        record.args = (client, method, redact_query_values(target), version, status)

    # Preserve framework objects' normal % formatting (e.g. an HTTP URL or a
    # signal enum). Only their sanitized rendered text can leave the handler.
    opaque_args = False
    if isinstance(args, Mapping):
        record.args = scrub_log_value(args)
    elif record.args:
        clean_args = []
        for arg in record.args:
            if isinstance(arg, (str, Mapping, list, tuple)):
                arg = scrub_log_value(arg)
            elif arg is not None and not isinstance(arg, (int, float)):
                opaque_args = True
            clean_args.append(arg)
        record.args = tuple(clean_args)
    record.msg = (
        scrub_log_value(record.msg)
        if isinstance(record.msg, (Mapping, list, tuple))
        else _redact_string(str(record.msg))
    )
    rendered = record.getMessage()
    safe = _redact_string(rendered)
    if hasattr(record, "color_message"):
        # Uvicorn's colored formatter uses this alternate template and the same args.
        color = str(record.color_message)
        record.color_message = _redact_string(color % record.args if record.args else color)
        if not is_access:
            opaque_args = True

    # Keep benign templates and parameters available to structured log consumers.
    # A label and its identifier may only become recognizable after interpolation.
    if not is_access and (opaque_args or safe != rendered):
        record.msg, record.args = safe, ()

    vars(record).pop("_api_exception", None)
    for key, value in list(vars(record).items()):
        if key in {"msg", "args", "exc_info", "color_message"}:
            continue
        if key not in _STANDARD:
            del vars(record)[key]
            vars(record)[scrub_log_value(key)] = (
                PII_REDACTION_PLACEHOLDER if _sensitive(key, value) else scrub_log_value(value)
            )
        elif isinstance(value, str):
            # LogRecord.name identifies the logger, unlike a context field named "name".
            vars(record)[key] = _redact_string(value)

    if record.exc_info:
        record.exc_text = _redact_string(logging.Formatter().formatException(record.exc_info))
        kind, exc, _ = record.exc_info
        record._api_exception = {
            "type": _redact_string(kind.__name__) if kind else None,
            "message": _redact_string(str(exc)) if exc is not None else None,
        }


def install_api_log_redaction() -> None:
    # A root filter misses nonpropagating loggers, and a snapshot of handlers
    # misses replacements. Sanitize a private copy at normal handler delivery.
    original = logging.Handler.handle
    if getattr(original, "_api_log_redaction", False):
        return

    def handle(handler: logging.Handler, record: logging.LogRecord) -> bool | logging.LogRecord:
        # Preserve stdlib filter ordering and the 3.12 replacement-record contract.
        # A handler filter can enrich its record with request fields before emit.
        record = copy(record)
        accepted = handler.filter(record)
        if not accepted:
            return accepted
        if isinstance(accepted, logging.LogRecord):
            record = copy(accepted)
        try:
            _sanitize(record)
            if record.exc_info:
                # Only the SDK event builder needs the original traceback to
                # group errors. Its payload crosses the existing before_send hook.
                sdk_logging = sys.modules.get("sentry_sdk.integrations.logging")
                if not isinstance(handler, getattr(sdk_logging, "EventHandler", ())):
                    # Keep an exception-bearing record for downstream diagnostics,
                    # without retaining the originating frames or exception object.
                    summary = record._api_exception
                    error = Exception(f"{summary['type']}: {summary['message']}")
                    record.exc_info = (Exception, error, None)
        except Exception:
            record = logging.LogRecord(
                "backend.logging",
                record.levelno,
                "",
                0,
                "[log record withheld: redaction failed]",
                (),
                None,
            )
        handler.acquire()
        try:
            handler.emit(record)
        finally:
            handler.release()
        return record if isinstance(accepted, logging.LogRecord) else accepted

    handle._api_log_redaction = True  # type: ignore[attr-defined]
    logging.Handler.handle = handle
