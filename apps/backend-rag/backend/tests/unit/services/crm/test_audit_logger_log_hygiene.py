"""The CRM audit logger writes the full change set to crm_audit_log and only
field NAMES to the log stream. Asserted on the fully formatted production JSON
output, extras included."""

import io
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.core.logging_config import StructuredJSONFormatter
from backend.app.services.crm.audit_logger import CRMAuditLogger

FAKE_NAME = "Qwxzy Synthetic Vellum"
FAKE_CLIENT_EMAIL = "qwxzy.vellum@example.com"
FAKE_ACTOR = "fake.person.zq@example.invalid"


@pytest.fixture
def json_stream():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredJSONFormatter("%(message)s"))
    target = logging.getLogger("backend.app.services.crm.audit_logger")
    old_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    yield stream
    target.removeHandler(handler)
    target.setLevel(old_level)


def _logger_with_pool():
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    audit = CRMAuditLogger()
    audit.initialize(pool)
    return audit, conn


@pytest.mark.asyncio
async def test_the_log_stream_carries_field_names_never_values(json_stream):
    audit, _ = _logger_with_pool()
    ok = await audit.log_state_change(
        entity_type="client",
        entity_id=5,
        old_state={"full_name": None, "email": None},
        new_state={"full_name": FAKE_NAME, "email": FAKE_CLIENT_EMAIL},
        user_email=FAKE_ACTOR,
        change_type="create",
    )
    assert ok is True
    out = json_stream.getvalue()
    for secret in (FAKE_NAME, FAKE_CLIENT_EMAIL, FAKE_ACTOR):
        assert secret not in out, f"{secret!r} reached the formatted log output"
    audit_lines = [json.loads(line) for line in out.splitlines() if "CRM AUDIT" in line]
    assert len(audit_lines) == 1
    rec = audit_lines[0]
    assert rec["changed_fields"] == ["email", "full_name"]
    assert rec["entity_id"] == 5 and rec["change_type"] == "create"
    assert rec["actor"].startswith("actor:")


@pytest.mark.asyncio
async def test_the_audit_row_still_gets_the_full_change_set(json_stream):
    audit, conn = _logger_with_pool()
    await audit.log_state_change(
        entity_type="client",
        entity_id=5,
        old_state={},
        new_state={"full_name": FAKE_NAME},
        user_email=FAKE_ACTOR,
        change_type="create",
    )
    args = conn.execute.await_args.args
    assert FAKE_NAME in args[7]  # changes column
    assert args[4] == FAKE_ACTOR  # the DB row keeps the real actor
