"""Flagged single-search, single-completion path for anonymous RAG questions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.core.config import settings
from backend.llm.claude_oauth_client import complete_async
from backend.services.rag.agentic.schema import CoreResult
from backend.services.rag.agentic.tools import KBLICanonicalLookupTool, PricingTool

logger = logging.getLogger(__name__)

_CONCURRENCY = asyncio.Semaphore(4)
_BACKOFF_SECONDS = 120.0
_backoff_until = 0.0
_KBLI_CODE = re.compile(r"\b\d{5}\b")
_PRICING_QUERY = re.compile(
    r"price|cost|fee|harga|biaya|prezzo|costo|\$|juta|cicil|bayar|pay",
    re.I,
)
_RULES = """You are ZANTARA, the assistant of Bali Zero, a consultancy in Bali for visas and KITAS, company setup (PT PMA), tax and legal matters in Indonesia. Today is {today}.

Rules:
- Reply in the language of the question.
- Base every legal, tax or immigration fact on the DOCUMENTS below and cite them as [n]. If the documents do not cover a point, say so plainly and give only general guidance, marked as such. Never invent article numbers, rates, deadlines or fees.
- If the question rests on a false or unverifiable premise (a law, article or figure the documents do not support), say so first.
- Prices of Bali Zero services: use only the PRICING block. If it is absent or does not list the service, say the team will confirm the quote.
- Never help to break the law; explain the legal route instead.
- Be concise: lead with the direct answer, then the key details, at most about 300 words unless the question asks for a list or verbatim text.
- When you refer the client to the team, give info@balizero.com; never give a personal email address.
"""


def _history_block(conversation_history: list[dict] | None) -> str:
    eligible = [
        item for item in conversation_history or [] if item.get("role") in {"user", "assistant"}
    ][-6:]
    return "\n".join(
        f"{item['role'].upper()}: {str(item.get('content', ''))[:600]}" for item in eligible
    )


def _pricing_service_type(query_lower: str) -> str:
    if "kitas" in query_lower:
        return "kitas"
    if "pt pma" in query_lower or "company" in query_lower:
        return "business_setup"
    return "all"


async def try_fast_answer(
    *,
    query: str,
    conversation_history: list[dict] | None,
    vector_tool: Any,
    start_time: float,
    channel: str | None = None,
    agent_role: Any | None = None,
    profile: Any | None = None,
    images: list[dict] | None = None,
) -> CoreResult | None:
    """Return a fast answer when eligible, otherwise leave the existing path untouched."""
    global _backoff_until

    if (
        not settings.fast_answer_enabled
        or images
        or agent_role is not None
        or profile
        or channel == "whatsapp"
        or not query
        or not query.strip()
        or len(query) > 4000
        or vector_tool is None
        or time.monotonic() < _backoff_until
        or _CONCURRENCY.locked()
    ):
        return None

    await _CONCURRENCY.acquire()
    try:
        tools_started = time.perf_counter()
        search_started = time.perf_counter()
        payload = json.loads(await vector_tool.execute(query=query))
        search_duration = time.perf_counter() - search_started
        sources = payload.get("sources") or []
        if not sources:
            return None

        blocks: list[tuple[str, str]] = []
        tools_called = ["vector_search"]
        codes = list(dict.fromkeys(_KBLI_CODE.findall(query)))[:3]
        if codes:
            tools_called.append("kbli_lookup")
        for code in codes:
            try:
                output = await KBLICanonicalLookupTool().execute(code=code)
                blocks.append(("KBLI CATALOGUE", output[:4000]))
            except Exception as exc:
                logger.warning("Fast answer KBLI tool failed: %s", type(exc).__name__)

        query_lower = query.lower()
        if _PRICING_QUERY.search(query):
            tools_called.append("get_pricing")
            try:
                output = await PricingTool().execute(
                    service_type=_pricing_service_type(query_lower)
                )
                blocks.append(("PRICING", output[:6000]))
            except Exception as exc:
                logger.warning("Fast answer pricing tool failed: %s", type(exc).__name__)

        tools_duration = time.perf_counter() - tools_started
        today = datetime.now(ZoneInfo("Asia/Makassar")).date().isoformat()
        prompt_parts = [_RULES.format(today=today), f"DOCUMENTS:\n{payload.get('content', '')}"]
        prompt_parts.extend(f"{heading}:\n{content}" for heading, content in blocks)
        history = _history_block(conversation_history)
        if history:
            prompt_parts.append(f"CONVERSATION SO FAR:\n{history}")
        prompt_parts.append(f"QUESTION:\n{query}")
        prompt = "\n\n".join(prompt_parts)

        llm_started = time.perf_counter()
        completion = await complete_async(
            prompt,
            model=settings.fast_answer_model,
            endpoint="agentic_rag_fast_answer",
            timeout_s=20,
            total_timeout_s=25,
        )
        llm_duration = time.perf_counter() - llm_started
        answer = getattr(completion, "text", completion)
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("empty fast answer")

        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(1, len(answer) // 4)
        return CoreResult(
            answer=answer.strip(),
            sources=sources,
            model_used=settings.fast_answer_model,
            route_used="fast_answer",
            tools_called=tools_called,
            document_count=len(sources),
            context_used=len(prompt),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            timings={
                "total": time.time() - start_time,
                "search": search_duration,
                "tools": tools_duration,
                "llm": llm_duration,
                "reasoning": llm_duration,
                "embedding": 0.0,
                "rerank": 0.0,
            },
        )
    except Exception as exc:
        _backoff_until = time.monotonic() + _BACKOFF_SECONDS
        logger.warning("Fast answer failed; backoff armed: %s", type(exc).__name__)
        return None
    finally:
        _CONCURRENCY.release()
