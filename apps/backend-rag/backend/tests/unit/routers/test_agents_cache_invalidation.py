import fnmatch
from types import SimpleNamespace

import pytest

from backend.app.routers import agents
from backend.core.cache import CacheService


async def _record_invalidations(monkeypatch):
    patterns = []

    async def recorder(pattern):
        patterns.append(pattern)

    monkeypatch.setattr(agents, "invalidate_cache", recorder)
    return patterns


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        "create_client_journey",
        "complete_journey_step",
        "add_compliance_tracking",
    ],
)
async def test_mutations_invalidate_the_agents_cached_prefixes(monkeypatch, handler):
    """Mutations must clear keys made by both cached agent endpoints."""
    patterns = await _record_invalidations(monkeypatch)
    journey = SimpleNamespace(journey_id="journey-synthetic", steps=[])
    item = SimpleNamespace(item_id="compliance-synthetic")
    monkeypatch.setattr(
        agents.journey_orchestrator,
        "create_journey",
        lambda **_kwargs: journey,
    )
    monkeypatch.setattr(agents.journey_orchestrator, "complete_step", lambda *_args: None)
    monkeypatch.setattr(agents.journey_orchestrator, "get_journey", lambda *_args: journey)
    monkeypatch.setattr(
        agents.compliance_monitor,
        "add_compliance_item",
        lambda **_kwargs: item,
    )

    if handler == "create_client_journey":
        await agents.create_client_journey(
            agents.CreateJourneyRequest(
                journey_type="synthetic_journey", client_id="client-synthetic"
            ),
            current_user={},
        )
    elif handler == "complete_journey_step":
        await agents.complete_journey_step(
            "journey-synthetic", "step-synthetic", current_user={}
        )
    else:
        await agents.add_compliance_tracking(
            agents.AddComplianceItemRequest(
                client_id="client-synthetic",
                compliance_type="visa_expiry",
                title="Synthetic compliance item",
                description="Synthetic description",
                deadline="2030-01-01",
            ),
            current_user={},
        )

    cache = CacheService()
    for prefix in ("agents_status", "agents_analytics"):
        key = cache._generate_key(prefix, {"user": "synthetic"})
        assert any(fnmatch.fnmatch(key, pattern) for pattern in patterns)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        "create_client_journey",
        "complete_journey_step",
        "add_compliance_tracking",
    ],
)
async def test_mutations_do_not_invalidate_unrelated_cached_prefixes(monkeypatch, handler):
    """Mutation invalidation remains scoped to the agents cache namespaces."""
    patterns = await _record_invalidations(monkeypatch)
    journey = SimpleNamespace(journey_id="journey-synthetic", steps=[])
    item = SimpleNamespace(item_id="compliance-synthetic")
    monkeypatch.setattr(agents.journey_orchestrator, "create_journey", lambda **_kwargs: journey)
    monkeypatch.setattr(agents.journey_orchestrator, "complete_step", lambda *_args: None)
    monkeypatch.setattr(agents.journey_orchestrator, "get_journey", lambda *_args: journey)
    monkeypatch.setattr(agents.compliance_monitor, "add_compliance_item", lambda **_kwargs: item)

    if handler == "create_client_journey":
        await agents.create_client_journey(
            agents.CreateJourneyRequest(
                journey_type="synthetic_journey", client_id="client-synthetic"
            ),
            current_user={},
        )
    elif handler == "complete_journey_step":
        await agents.complete_journey_step(
            "journey-synthetic", "step-synthetic", current_user={}
        )
    else:
        await agents.add_compliance_tracking(
            agents.AddComplianceItemRequest(
                client_id="client-synthetic",
                compliance_type="visa_expiry",
                title="Synthetic compliance item",
                description="Synthetic description",
                deadline="2030-01-01",
            ),
            current_user={},
        )

    unrelated_key = CacheService()._generate_key("unrelated", {"user": "synthetic"})
    assert not any(fnmatch.fnmatch(unrelated_key, pattern) for pattern in patterns)
