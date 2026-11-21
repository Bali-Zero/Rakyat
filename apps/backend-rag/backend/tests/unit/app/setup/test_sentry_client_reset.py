"""A Sentry client bound by one test must not outlive it.

`sentry_sdk.init()` used as a teardown left an active client reading request bodies, and the
garuda upload 413 test in the same worker saw 50292 bytes pulled (PR #313, shard 3, twice).
"""

from __future__ import annotations

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from backend.tests.unit.app.setup.conftest import leave_no_sentry_client_behind


def test_the_reset_unbinds_the_client_and_its_request_body_reader():
    sentry_sdk.init(dsn="https://public@o0.ingest.sentry.io/1", max_request_body_size="medium")
    assert sentry_sdk.get_client().get_integration(FastApiIntegration) is not None, (
        "precondition: init must have bound the FastAPI integration, or this proves nothing"
    )

    leave_no_sentry_client_behind()

    client = sentry_sdk.get_client()
    assert not client.is_active()
    assert client.get_integration(FastApiIntegration) is None
