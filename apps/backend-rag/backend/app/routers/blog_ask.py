"""
Blog Ask Router - AskZantara widget endpoint for public blog articles
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, model_validator

from backend.services.rag.agentic import AgenticRAGOrchestrator, create_agentic_rag

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/blog", tags=["blog"])

# Global orchestrator instance (lazy loaded)
_blog_orchestrator: AgenticRAGOrchestrator | None = None


async def get_blog_orchestrator(request: Request) -> Any:
    """Get or create the RAG orchestrator for blog queries"""
    global _blog_orchestrator
    if _blog_orchestrator is None:
        db_pool = getattr(request.app.state, "db_pool", None)
        search_service = getattr(request.app.state, "search_service", None)
        _blog_orchestrator = create_agentic_rag(retriever=search_service, db_pool=db_pool)
    return _blog_orchestrator


class BlogAskRequest(BaseModel):
    """Request model for blog article questions"""

    question: str
    article_title: str | None = None
    article_category: str | None = None
    article_content: str | None = None  # Optional context from article
    context: dict[str, Any] | str | None = None

    @model_validator(mode="after")
    def _populate_article_fallbacks_from_context(self) -> "BlogAskRequest":
        if isinstance(self.context, dict):
            if not self.article_title:
                raw_title = (
                    self.context.get("article_title")
                    or self.context.get("title")
                    or self.context.get("topic")
                )
                if isinstance(raw_title, str) and raw_title.strip():
                    self.article_title = raw_title.strip()
            if not self.article_category:
                raw_category = self.context.get("article_category") or self.context.get("category")
                if isinstance(raw_category, str) and raw_category.strip():
                    self.article_category = raw_category.strip()
            if not self.article_content:
                raw_content = self.context.get("article_content") or self.context.get("content")
                if isinstance(raw_content, str) and raw_content.strip():
                    self.article_content = raw_content.strip()
        elif isinstance(self.context, str) and self.context.strip():
            if not self.article_title:
                self.article_title = self.context.strip()
        return self


class BlogAskSource(BaseModel):
    """Source citation model for AskZantara blog responses"""

    title: str = "Source"
    snippet: str = ""
    url: str | None = None


class BlogAskResponse(BaseModel):
    """Response model for blog article questions"""

    answer: str
    sources: list[BlogAskSource] = []
    confidence: float = 0.0


@router.post("/ask", response_model=BlogAskResponse)
async def ask_zantara(request: Request, body: BlogAskRequest) -> BlogAskResponse:
    """
    Public endpoint for AskZantara widget on blog articles.

    Allows visitors to ask questions about article topics.
    No authentication required.
    """
    try:
        orchestrator = await get_blog_orchestrator(request)

        # Build context-aware query
        query = body.question

        # Add article context to help RAG system
        context_parts = []
        if body.article_title:
            context_parts.append(f"Article: {body.article_title}")
        if body.article_category:
            context_parts.append(f"Category: {body.article_category}")

        if context_parts:
            query = f"[Context: {', '.join(context_parts)}] {query}"

        logger.info(f"📝 Blog ask: {body.question[:100]}... (category={body.article_category})")

        # Call RAG orchestrator
        result = await orchestrator.process_query(
            query=query,
            user_id="blog_visitor",
            session_id=None,
        )

        # Format sources for response
        sources: list[BlogAskSource] = []
        if result.sources:
            for source in result.sources[:5]:  # Limit to 5 sources
                if isinstance(source, dict):
                    metadata = source.get("metadata")
                    raw_url = (
                        source.get("url")
                        or source.get("source_url")
                        or (metadata.get("url") if isinstance(metadata, dict) else None)
                    )
                    raw_text = source.get("snippet") or source.get("content") or ""
                    sources.append(
                        BlogAskSource(
                            title=str(source.get("title") or "Source"),
                            snippet=str(raw_text)[:200] if raw_text else "",
                            url=raw_url.strip()
                            if isinstance(raw_url, str) and raw_url.strip()
                            else None,
                        ),
                    )
                elif hasattr(source, "title"):
                    raw_url = getattr(source, "url", None) or getattr(source, "source_url", None)
                    raw_text = getattr(source, "snippet", None) or getattr(source, "content", "")
                    sources.append(
                        BlogAskSource(
                            title=str(getattr(source, "title", None) or "Source"),
                            snippet=str(raw_text)[:200] if raw_text else "",
                            url=raw_url.strip()
                            if isinstance(raw_url, str) and raw_url.strip()
                            else None,
                        ),
                    )

        # Calculate confidence from evidence score if available
        confidence = 0.7  # Default confidence
        if hasattr(result, "evidence_score"):
            confidence = result.evidence_score
        elif hasattr(result, "debug_info") and result.debug_info:
            confidence = result.debug_info.get("evidence_score", 0.7)

        return BlogAskResponse(
            answer=result.answer,
            sources=sources,
            confidence=confidence,
        )

    except Exception as e:
        logger.error(f"❌ Blog ask error: {e!s}")
        raise HTTPException(
            status_code=500,
            detail="Sorry, I couldn't process your question. Please try again.",
        ) from e
