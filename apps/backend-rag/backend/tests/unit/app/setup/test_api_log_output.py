"""Exercise the API output boundary in fresh processes with offline fixtures."""

import io
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

EMAIL = "fixture@example.invalid"
HIDDEN = "fixture-hidden"
PHONE = "+1 (202) 555-0100"
REDACTED = "[REDACTED]"
CONTROL = "elapsed=12.75 ms epoch=1700000000123 path=/v1/jobs/42"


def _exercise_outputs(mode, log_file):
    from uvicorn.logging import AccessFormatter, DefaultFormatter

    from backend.app.services.log_ring_buffer import get_ring_buffer_handler

    original = logging.Handler.handle
    from backend.app.setup.log_redaction import install_api_log_redaction
    from backend.app.setup.logging_config import configure_logging

    assert logging.Handler.handle is original
    framework = logging.getLogger("uvicorn")
    framework.propagate = False
    access = logging.getLogger("uvicorn.access")
    access.propagate = False

    for phase in ("early", "late"):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(DefaultFormatter("%(message)s", use_colors=True))
        framework.handlers[:] = [handler]
        access_handler = logging.StreamHandler(stream)
        access_handler.setFormatter(AccessFormatter("%(request_line)s %(status_code)s"))
        access.handlers[:] = [access_handler]
        if phase == "early":
            configure_logging()
            installed = logging.Handler.handle
            install_api_log_redaction()
            assert logging.Handler.handle is installed and installed is not original
        framework.warning("%s pid %d", phase, 42, extra={"color_message": "%s worker %d"})
        access.warning(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1",
            "GET",
            f"/{phase}/{EMAIL}?q=private-fixture&page=2",
            "1.1",
            200,
        )
        text = stream.getvalue()
        assert f"{phase} worker 42" in text
        assert f"GET /{phase}/{REDACTED}?q=***&page=2 HTTP/1.1" in text
        assert "200" in text and EMAIL not in text and "private-fixture" not in text

    # Applications commonly attach request metadata to each handler at delivery.
    def add_contact(record):
        record.request_contact = EMAIL
        return True

    request_log = logging.getLogger("fixture.request")
    request_log.propagate = False
    enriched = io.StringIO()
    request_handler = logging.StreamHandler(enriched)
    request_handler.addFilter(add_contact)
    request_handler.setFormatter(logging.Formatter("%(message)s contact=%(request_contact)s"))
    request_log.addHandler(request_handler)
    request_log.warning("request completed")
    assert enriched.getvalue().strip() == f"request completed contact={REDACTED}"

    logger = logging.getLogger("fixture.api")
    structured = {"items": [{"name": HIDDEN, "attempt": 2}, EMAIL]}
    structured.update(path=Path("/v1/jobs/42"), when=datetime(2025, 1, 2, tzinfo=timezone.utc))
    deep = {"code": "fixture-too-deep"}
    for _ in range(20):
        deep = {"next": deep}
    try:
        raise ValueError(f"exception marker {EMAIL}")
    except ValueError:
        logger.exception(
            "case %s phone %s NPWP 0000000000000000 CL-000000 passport AA000000 %s",
            EMAIL,
            PHONE,
            CONTROL,
            extra={
                "context": {**structured, "deep": deep},
                "duration_ms": 12.75,
                "user_email": EMAIL,
            },
        )
    assert structured["items"][0]["name"] == HIDDEN
    row = json.loads(log_file.read_text().splitlines()[-1])
    ring = get_ring_buffer_handler().snapshot(module=logger.name)[0]
    for entry in (row, ring):
        assert entry["message"].startswith(f"case {REDACTED} phone {REDACTED}")
        assert f"NPWP {REDACTED} {REDACTED} passport {REDACTED}" in entry["message"]
        assert CONTROL in entry["message"]
        assert entry["exception"]["type"] == "ValueError"
        assert entry["exception"]["message"] == f"exception marker {REDACTED}"
    assert row["context"]["items"] == [{"name": REDACTED, "attempt": 2}, REDACTED]
    assert row["context"]["path"] == "/v1/jobs/42"
    assert row["context"]["when"] == "2025-01-02 00:00:00+00:00"
    assert row["user"] == REDACTED and row["duration_ms"] == 12.75
    assert "Traceback" in row["exception"]["traceback"]
    assert "context" not in ring
    assert row["source"]["function"] == "_exercise_outputs" and row["source"]["line"] > 0
    assert row["environment"] == mode and row["timestamp"]
    for value in (EMAIL, PHONE, HIDDEN, "fixture-too-deep"):
        assert value not in json.dumps([row, ring])

    # Optional ordinary handler attached to a child after bootstrap. Stack info
    # goes through the real stdlib formatter, separately from the log message.
    later = io.StringIO()
    optional = logging.StreamHandler(later)
    optional.setFormatter(logging.Formatter("%(message)s %(details)s"))
    logger.handlers[:] = [optional]
    logger.propagate = False
    record = logger.makeRecord(
        logger.name,
        logging.WARNING,
        __file__,
        1,
        "late event",
        (),
        None,
        extra={"details": {"data": [EMAIL, 7]}},
        sinfo=f"Stack marker {EMAIL}",
    )
    logger.handle(record)
    assert "late event" in later.getvalue() and "7" in later.getvalue()
    assert f"Stack marker {REDACTED}" in later.getvalue() and EMAIL not in later.getvalue()

    logger.handlers[:] = [logging.StreamHandler(later)]
    logger.warning("invalid integer %d", EMAIL)
    assert "log record withheld" in later.getvalue() and EMAIL not in later.getvalue()


def _exercise_sentry():
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.transport import Transport

    from backend.app.setup.logging_config import configure_logging
    from backend.app.setup.sentry_config import _init_sentry_blocking

    events = []

    class InMemory(Transport):
        def capture_envelope(self, envelope):
            events.extend(item.get_event() for item in envelope.items if item.type == "event")

    init = sentry_sdk.init

    def offline_init(**options):
        return init(
            **options,
            default_integrations=False,
            integrations=[LoggingIntegration()],
            transport=InMemory(),
        )

    sentry_sdk.init = offline_init
    _init_sentry_blocking("https://public@fixture.invalid/1")
    configure_logging()
    logger = logging.getLogger("fixture.sdk")
    logger.info("breadcrumb marker %s", EMAIL, extra={"context": {"name": HIDDEN}})
    sentry_sdk.add_breadcrumb(message=f"independent marker {EMAIL}", data={"token": HIDDEN})
    # Check what the SDK retained before any event-level before_send hook ran.
    saved = sentry_sdk.get_isolation_scope()._breadcrumbs[-1]
    assert saved["message"] == f"independent marker {REDACTED}"
    assert saved["data"]["token"] == REDACTED
    try:
        raise ValueError(f"exception marker {EMAIL}")
    except ValueError:
        logger.exception("event marker")
    (event,) = events
    assert event["logentry"]["formatted"] == "event marker"
    exc = event["exception"]["values"][-1]
    assert exc["type"] == "ValueError" and exc["value"] == f"exception marker {REDACTED}"
    assert exc["stacktrace"]["frames"]
    crumbs = [c["message"] for c in event["breadcrumbs"]["values"] if "message" in c]
    assert f"breadcrumb marker {REDACTED}" in crumbs
    assert f"independent marker {REDACTED}" in crumbs
    assert EMAIL not in json.dumps(event) and HIDDEN not in json.dumps(event)
    sentry_sdk.get_client().close()


@pytest.mark.parametrize("mode", ["production", "development", "sentry"])
def test_api_outputs(tmp_path, mode):
    log_file = tmp_path / "fixture.log"
    result = subprocess.run(
        [sys.executable, __file__, mode, str(log_file)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[5]),
            "ENVIRONMENT": "development" if mode == "sentry" else mode,
            "LOG_FILE": str(log_file),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "Logging error" not in result.stderr and EMAIL not in result.stderr
    if mode != "sentry":
        assert CONTROL in result.stdout and f"exception marker {REDACTED}" in result.stdout
        assert all(v not in result.stdout for v in (EMAIL, PHONE, HIDDEN, "fixture-too-deep"))
        if mode == "production":
            console = json.loads(result.stdout.splitlines()[-1])
            stored = json.loads(log_file.read_text().splitlines()[-1])
            assert console.pop("timestamp") and stored.pop("timestamp")
            assert console == stored
        else:
            assert "context:" in result.stdout and '"attempt": 2' in result.stdout


if __name__ == "__main__":
    if sys.argv[1] == "sentry":
        _exercise_sentry()
    else:
        _exercise_outputs(sys.argv[1], Path(sys.argv[2]))
