"""
Agentic RAG with Tool Use - Refactored Architecture

This module has been refactored from a 2,200+ line monolithic file into
a well-structured package with clear separation of concerns:

Structure:
- tools.py: Tool class definitions (VectorSearch, WebSearch, Database, Calculator, Vision, Pricing)
- orchestrator.py: Main orchestrator with query processing and streaming (910 lines)
- llm_gateway.py: Unified LLM interface with model fallback cascade (493 lines)
- reasoning.py: ReAct reasoning loop (Thought→Action→Observation) (294 lines)
- prompt_builder.py: System prompt construction with caching
- response_processor.py: Response cleaning and formatting
- context_manager.py: User context and memory management
- tool_executor.py: Tool parsing and execution with rate limiting
- pipeline.py: Response processing pipeline with verification, cleaning, and citation stages

Key Features:
- Quality routing (Fast/Pro/DeepThink)
- ReAct pattern (Reason-Act-Observe)
- Multi-tier fallback (Gemini Pro -> Flash -> Flash-Lite -> OpenRouter)
- Native function calling with regex fallback
- Memory persistence and semantic caching
- Streaming and non-streaming modes
- Response verification and self-correction

Backward Compatibility:
All original exports are maintained for seamless integration with existing code.
"""

import importlib
import importlib.util
import logging
import pkgutil
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.core.semantic_cache import SemanticCache
    from backend.services.misc.clarification_service import ClarificationService

# Lazy import to prevent circular dependencies
_GraphTraversalTool = None


def _get_graph_traversal_tool():
    """Lazy load GraphTraversalTool to prevent circular imports."""
    global _GraphTraversalTool
    if _GraphTraversalTool is None:
        from backend.services.rag.agentic.graph_tool import GraphTraversalTool

        _GraphTraversalTool = GraphTraversalTool
    return _GraphTraversalTool


if TYPE_CHECKING:
    from .kg_orchestrator import AgenticResponse, KGAgenticOrchestrator
    from .orchestrator import AgenticRAGOrchestrator

# PEP 562: the public names below load on first access, so importing a light
# submodule of this package (e.g. ``agentic.tools``) from the ``api`` process no
# longer drags in the orchestrator and, through it, google.genai and friends.
_LAZY_ATTRS = {
    "AgenticResponse": ".kg_orchestrator",
    "KGAgenticOrchestrator": ".kg_orchestrator",
    "AgenticRAGOrchestrator": ".orchestrator",
    "CitationStage": ".pipeline",
    "FormatStage": ".pipeline",
    "PostProcessingStage": ".pipeline",
    "ResponsePipeline": ".pipeline",
    "VerificationStage": ".pipeline",
    "create_default_pipeline": ".pipeline",
    "CalculatorTool": ".tools",
    "CRMTool": ".tools",
    "ImageGenerationTool": ".tools",
    "KBLICanonicalLookupTool": ".tools",
    "PricingTool": ".tools",
    "TeamKnowledgeTool": ".tools",
    "TimeSheetTool": ".tools",
    "VectorSearchTool": ".tools",
    "VisionTool": ".tools",
    "WebSearchTool": ".tools",
}


def __getattr__(name: str):
    if name in _LAZY_ATTRS:
        value = getattr(importlib.import_module(_LAZY_ATTRS[name], __name__), name)
    elif not name.startswith("__") and importlib.util.find_spec(f".{name}", __name__):
        # Any submodule (context_manager, tools, ...): the eager __init__ left
        # every transitively imported submodule bound as an attribute.
        value = importlib.import_module(f".{name}", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__():
    submodules = {m.name for m in pkgutil.iter_modules(__path__)}
    return sorted(set(globals()) | set(_LAZY_ATTRS) | submodules)


logger = logging.getLogger(__name__)

# Export all public classes
__all__ = [
    "AgenticRAGOrchestrator",
    "AgenticResponse",
    # Tools
    "CRMTool",
    "CalculatorTool",
    "CitationStage",
    "FormatStage",
    "GraphTraversalTool",
    "ImageGenerationTool",
    "KBLICanonicalLookupTool",
    "KGAgenticOrchestrator",
    "PostProcessingStage",
    "PricingTool",
    # Pipeline components
    "ResponsePipeline",
    "TeamKnowledgeTool",
    "TimeSheetTool",
    "VectorSearchTool",
    "VerificationStage",
    "VisionTool",
    "WebSearchTool",
    # Submodules (for direct access)
    "context_manager",
    "create_agentic_rag",
    "create_default_pipeline",
    "create_kg_agentic_rag",
    "llm_gateway",
    "memory_handler",
    "prompt_builder",
    "reasoning",
]


def create_agentic_rag(
    retriever,
    db_pool,
    _web_search_client=None,
    semantic_cache: "SemanticCache" = None,
    clarification_service: "ClarificationService" = None,
    nlm_enrichment_service: Any = None,  # noqa: ARG001 — Phase 6 DEPRECATED, kept for call-site compat
    specialized_service_router: Any = None,
    faq_cache: Any = None,  # NotebookLMCacheService (P7 SPEC v2 D3: exact-match FAQ cache)
) -> "AgenticRAGOrchestrator":
    """
    Factory function to create a fully configured AgenticRAGOrchestrator.

    This function assembles all required tools and initializes the orchestrator
    with proper configuration. It maintains backward compatibility with the
    original agentic.py interface.

    Args:
        retriever: Knowledge base retriever (SearchService/KnowledgeService)
        db_pool: PostgreSQL connection pool for database queries
        _web_search_client: Optional web search client (disabled by default)
        semantic_cache: Optional semantic cache for query results
        clarification_service: Optional service for resolving ambiguous queries
        nlm_enrichment_service: Optional NLM enrichment service for CAUTIOUS-zone queries
        specialized_service_router: Optional SpecializedServiceRouter for complex query routing
        faq_cache: Optional NotebookLMCacheService for exact-match FAQ caching (< 1ms lookup).
            Without this, every orchestrator built via this factory silently loses the
            FAQ-cache fast path (P7 scar — prewarm/harvester entries become unreachable).

    Returns:
        Configured AgenticRAGOrchestrator instance

    Tool Priority:
        1. KBLICanonicalLookupTool (exact five-digit KBLI source)
        2. VectorSearchTool (semantic knowledge base search)
        3. PricingTool (official Bali Zero pricing)
        4. TeamKnowledgeTool (team member queries)
        5. KnowledgeGraphTool (structured knowledge graph)
        6. CalculatorTool (numerical computations)
        7. VisionTool (visual document analysis)
        8. ImageGenerationTool (AI image generation)
        9. WebSearchTool (web search for out-of-KB queries via Brave)
        10. CRMTool (live client/practice database queries)
        11. TimeSheetTool (team timesheet management)
    """
    logger.debug("create_agentic_rag: Initializing tools...")

    # Initialize New Knowledge Graph Builder (Dec 2025)
    # Uses PostgreSQL persistence and LLM semantic extraction
    from backend.services.autonomous_agents.knowledge_graph_builder import KnowledgeGraphBuilder
    from backend.services.tools.knowledge_graph_tool import KnowledgeGraphTool

    from .tools import (
        CalculatorTool,
        CRMTool,
        ImageGenerationTool,
        KBLICanonicalLookupTool,
        PricingTool,
        TeamKnowledgeTool,
        TimeSheetTool,
        VectorSearchTool,
        VisionTool,
        WebSearchTool,
    )

    # We pass the ai_client later if needed, but for now we use the factory's db_pool
    kg_builder = KnowledgeGraphBuilder(search_service=retriever, db_pool=db_pool)

    # Exact lookup comes first: explicit KBLI codes must not be answered from
    # a semantically nearby vector result. Vector search remains the default
    # discovery tool for activity descriptions without a known code.
    # ZANTARA LEAN STRATEGY (Dec 2025): Reduced to essential tools only.
    tools = [
        KBLICanonicalLookupTool(),
        VectorSearchTool(retriever, user_level=3),
        PricingTool(),
        TeamKnowledgeTool(db_pool),
        KnowledgeGraphTool(kg_builder),
        CalculatorTool(),
        VisionTool(),
        ImageGenerationTool(),
        WebSearchTool(),
        CRMTool(db_pool),
        TimeSheetTool(),
    ]

    # WA team-assistant Phase 2 (Zero GO 2026-07-20, flag-gated, default OFF):
    # 4 read-only CRM-scoped tools per team member (my-clients/my-practices/
    # my-deadlines/practice-detail). Read ONCE here, at orchestrator-
    # construction time — when the flag is off these tools are absent from
    # `tools` (and therefore from the Gemini function-declaration schema)
    # for EVERY caller, not just WA senders. See team_crm_tools.py module
    # docstring for the full design + the per-request self-gate inside
    # each tool's execute().
    from .team_crm_tools import create_team_crm_tools, is_team_crm_tools_enabled

    if is_team_crm_tools_enabled():
        tools.extend(create_team_crm_tools(db_pool))
        logger.debug("create_agentic_rag: WA team-assistant Phase 2 CRM tools armed")

    logger.debug("create_agentic_rag: Tools list created")

    logger.debug("create_agentic_rag: Instantiating AgenticRAGOrchestrator...")
    # Through the module, not a bare name: resolves lazily, and tests that
    # monkeypatch the package attribute still take effect.
    orchestrator = sys.modules[__name__].AgenticRAGOrchestrator(
        tools=tools,
        db_pool=db_pool,
        semantic_cache=semantic_cache,
        faq_cache=faq_cache,
        retriever=retriever,
        clarification_service=clarification_service,
        specialized_service_router=specialized_service_router,
    )
    logger.debug("create_agentic_rag: Orchestrator instantiated")
    return orchestrator


def create_kg_agentic_rag(
    retriever,
    db_pool,
) -> "KGAgenticOrchestrator":
    """
    Factory function to create a KG-enhanced Agentic RAG Orchestrator.

    This orchestrator combines Knowledge Graph traversal with vector search
    and LLM reasoning for explainable, high-quality answers.

    Args:
        retriever: Knowledge base retriever (SearchService)
        db_pool: PostgreSQL connection pool for KG database access

    Returns:
        Configured KGAgenticOrchestrator instance
    """
    logger.debug("create_kg_agentic_rag: Initializing KG orchestrator...")

    orchestrator = sys.modules[__name__].KGAgenticOrchestrator(
        db_pool=db_pool,
        retriever=retriever,
    )

    logger.info("✅ KGAgenticOrchestrator created")
    return orchestrator
