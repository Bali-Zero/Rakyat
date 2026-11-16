"""Guilt + innocence for the CRM singleton pool isolation in this directory.

The two tests are ordered on purpose (pytest keeps definition order inside a
file, xdist --dist loadfile keeps a file on one worker): the first leaves the
singletons initialized the way service_initializer does, the second must see
them as the first test found them.
"""

from unittest.mock import MagicMock

from backend.app.services.crm.audit_logger import audit_logger
from backend.app.services.crm.metrics import metrics_collector


def test_a_test_may_initialize_the_crm_singletons():
    audit_logger.initialize(MagicMock())
    metrics_collector.initialize(MagicMock())
    assert audit_logger.pool is not None
    assert metrics_collector.pool is not None


def test_the_next_test_does_not_inherit_their_pools():
    assert audit_logger.pool is None
    assert metrics_collector.pool is None
