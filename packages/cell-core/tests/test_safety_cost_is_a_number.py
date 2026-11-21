"""Tests for valid proposal costs in DNAInterpreter."""

import math

import pytest
from cell_core.types import DNAConfig, DNARule, Proposal


def make_interpreter():
    from cell_core.safety import DNAInterpreter

    dna = DNAConfig(
        rules=[DNARule(text="Never modify DNA", priority=1)],
        constraints={"max_daily_budget_usd": 10.0, "max_cost_per_investigation_usd": 0.5},
    )
    return DNAInterpreter(dna)


@pytest.mark.parametrize("cost", [math.nan, math.inf, -5.0, True])
def test_rejects_invalid_cost(cost):
    proposal = Proposal(
        action="restart_service", reason="test", confidence=0.9, tier_used=0, cost_usd=cost
    )

    result = make_interpreter().validate(proposal, budget_spent=0.0)

    assert result.can_proceed is False
    assert result.reason == "Invalid cost"


@pytest.mark.parametrize("cost", [0.0, 0.1])
def test_approves_finite_non_negative_cost_within_limits(cost):
    proposal = Proposal(
        action="restart_service", reason="test", confidence=0.9, tier_used=0, cost_usd=cost
    )

    result = make_interpreter().validate(proposal, budget_spent=0.0)

    assert result.can_proceed is True


def test_preserves_investigation_cap_rejection():
    proposal = Proposal(
        action="investigate", reason="test", confidence=0.9, tier_used=1, cost_usd=0.6
    )

    result = make_interpreter().validate(proposal, budget_spent=0.0)

    assert result.can_proceed is False
    assert result.reason == "Investigation cost too high"


@pytest.mark.parametrize("cost", [math.nan, math.inf, -5.0, True])
def test_none_action_passes_regardless_of_cost(cost):
    proposal = Proposal(action="none", reason="stable", confidence=1.0, tier_used=-1, cost_usd=cost)

    result = make_interpreter().validate(proposal, budget_spent=0.0)

    assert result.can_proceed is True
