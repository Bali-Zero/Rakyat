"""
KG auto-expansion must not learn from the raw query of a shared identity.

When a turn has no source chunks, ``KGAutoExpansion.expand_from_response``
extracts entities from the query itself and stages them in the shared
knowledge graph. For ``blog_visitor`` (every anonymous AskZantara reader),
``anonymous`` or the WA mirror, that is one person's words becoming staged
global facts. Found by the cross-family gate on Rakyat #542 (2026-11-21).
"""

import inspect
import re

import pytest

from backend.services.rag.agentic import orchestrator_core
from backend.services.rag.agentic.orchestrator_core import _kg_expansion_query

QUERY = "My company PT Example Alpaca in Kintamani needs a KITAS"


@pytest.mark.parametrize(
    "user_id",
    [None, "", "anonymous", "blog_visitor", "  Blog_Visitor ", "wa-mirror-internal@balizero.com"],
)
def test_shared_identities_never_hand_the_query_to_kg_expansion(user_id) -> None:
    assert _kg_expansion_query(QUERY, user_id) is None


@pytest.mark.parametrize(
    "user_id",
    ["client@example.com", "3f2b1c9e-0d4a-4b7e-9a51-2c6d8e0f1a23", "blog_visitor2"],
)
def test_personal_identities_keep_todays_behaviour(user_id) -> None:
    assert _kg_expansion_query(QUERY, user_id) == QUERY


def test_the_only_expansion_call_site_goes_through_the_gate() -> None:
    source = inspect.getsource(orchestrator_core)
    calls = [m.start() for m in re.finditer(r"\.expand_from_response\(", source)]
    assert len(calls) == 1, "a second call site must be gated too"
    call_block = source[calls[0] : calls[0] + 600]
    assert "query=_kg_expansion_query(query, user_id)" in call_block
