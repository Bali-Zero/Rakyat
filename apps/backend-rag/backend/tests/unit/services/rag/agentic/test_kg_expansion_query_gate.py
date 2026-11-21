"""
A shared identity's turn must never feed KG auto-expansion.

Expansion stages entities in the shared knowledge graph. Two of its inputs
can carry the asker's own words: the raw query (fallback when a turn has no
source chunks) and tool output that echoes the query, such as the KG tool's
"no entity found for <term>", which arrives as a source chunk. For
``blog_visitor`` (every anonymous AskZantara reader), ``anonymous`` or the WA
mirror, that is one person's words becoming staged global facts. Found by the
cross-family gates on Rakyat #542 and #545 (2026-11-21).
"""

import inspect
import re

import pytest

from backend.services.rag.agentic import orchestrator_core
from backend.services.rag.agentic.orchestrator_core import _kg_expansion_allowed


@pytest.mark.parametrize(
    "user_id",
    [None, "", "anonymous", "blog_visitor", "  Blog_Visitor ", "wa-mirror-internal@balizero.com"],
)
def test_shared_identities_never_feed_kg_expansion(user_id) -> None:
    assert _kg_expansion_allowed(user_id) is False


@pytest.mark.parametrize(
    "user_id",
    ["client@example.com", "3f2b1c9e-0d4a-4b7e-9a51-2c6d8e0f1a23", "blog_visitor2"],
)
def test_personal_identities_keep_todays_behaviour(user_id) -> None:
    assert _kg_expansion_allowed(user_id) is True


def test_the_only_expansion_call_is_behind_the_gate() -> None:
    source = inspect.getsource(orchestrator_core)
    calls = [m.start() for m in re.finditer(r"\.expand_from_response\(", source)]
    assert len(calls) == 1, "a second call site must be gated too"
    guard = source.rfind("_kg_expansion_allowed(user_id)", 0, calls[0])
    assert guard != -1, "the expansion call must sit after the identity gate"
    between = source[guard : calls[0]]
    assert between.count("\n") < 20, "the gate must be the condition of this call"
