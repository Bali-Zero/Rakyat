"""
Conftest for backend/tests/unit/app/setup/ tests.

Mocks optional dependencies not installed in the test venv
so service_initializer tests can import analytics modules.
"""

import sys
from unittest.mock import MagicMock

import pytest

# aiosmtplib is not installed in the test venv
# (weekly_email_reporter imports it at module level)
if "aiosmtplib" not in sys.modules:
    sys.modules["aiosmtplib"] = MagicMock()


def leave_no_sentry_client_behind() -> None:
    """Unbind whatever Sentry client a test bound, without importing the SDK.

    `sentry_sdk.init()` with no DSN is NOT a disabled client: it binds an active client with
    the FastAPI/Starlette integrations on and `max_request_body_size="medium"`. Every later
    test in the same xdist worker then has its request bodies read by the SDK, and the
    garuda upload test's 413 guard saw the whole understated body pulled (50292 bytes).
    """
    sentry_sdk = sys.modules.get("sentry_sdk")
    if sentry_sdk is None:
        return
    client = sentry_sdk.get_client()
    if client.is_active():
        client.close()
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture(autouse=True)
def _no_sentry_client_survives_a_test():
    yield
    leave_no_sentry_client_behind()


@pytest.fixture(autouse=True)
def _restore_crm_singleton_pools():
    """service_initializer hands its (mock) pool to the process-wide CRM
    singletons and nothing took it back. Any later test in the same xdist
    worker then found a "working" audit logger: with a pool it writes the
    audit row and logs the change set, without one it fails fast. The PII log
    regression in test_log_pii_regressions.py passed or failed depending on
    which files shared its worker.
    """
    from backend.app.services.crm.audit_logger import audit_logger
    from backend.app.services.crm.metrics import metrics_collector

    before = (audit_logger.pool, metrics_collector.pool)
    yield
    audit_logger.pool, metrics_collector.pool = before
