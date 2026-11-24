"""
RAG (Retrieval-Augmented Generation) services.

Contains agent-based RAG, hybrid retrieval, knowledge graph integration,
and vision RAG capabilities.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.rag.hybrid_search import HybridSearchService, get_hybrid_search_service


def __getattr__(name: str):
    # Public-index tools import only deterministic data processing. Resolve
    # the existing service exports on first access so these tools need no app credentials.
    if name in __all__:
        from backend.services.rag import hybrid_search

        return getattr(hybrid_search, name)
    raise AttributeError(name)


__all__ = [
    "HybridSearchService",
    "get_hybrid_search_service",
]
