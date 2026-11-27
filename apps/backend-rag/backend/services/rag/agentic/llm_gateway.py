"""
LLM Gateway - Unified interface for the agentic RAG chat model calls.

Since Wave 2 (2026-11-27, owner order) every call goes through the Claude
Console door (``backend.llm.claude_oauth_client``: the linked-organization
keys pinned to their workspaces, included monthly Ultra credit, no card spend).
There is no Gemini and no OpenRouter route left in this module.

Key Features:
- Plain completions (``enable_function_calling=False``): one prompt in, text out.
- Structured ReAct turns (``enable_function_calling=True`` with tools): the CLI is
  asked for a schema-validated turn (thought / tool calls / final answer) and the
  result is wrapped in a duck-typed response object whose
  ``candidates[0].content.parts[*].function_call`` carries the calls, so the
  sync and streaming ReAct loops parse it unchanged (``native`` mode).
- Per-call tool narrowing (T-VIS): ``gemini_tools`` overrides the shared list for
  one call only; ``None`` falls back to the shared list, ``[]`` means no tools.
- Circuit breaker, per-query cost cap and fallback chain over model slugs.
- Operator alert when the Console seats report a durable failure (quota/auth).
- Images are NOT forwarded: cloud vision is disabled (local OCR only).

Cost ledger: ``complete_async`` already records every call (real envelope usage,
provider ``claude_console``, endpoint ``rag.gateway.chat``); this module does not
record a second row.
"""

import asyncio
import json
import logging
import os
import re
import time
from types import SimpleNamespace
from typing import Any

from backend.app.core.circuit_breaker import CircuitBreaker
from backend.app.core.constants import HttpTimeoutConstants
from backend.app.core.error_classification import ErrorClassifier, get_error_context
from backend.app.metrics import (
    llm_all_models_failed_total,
    llm_circuit_breaker_open_total,
    llm_circuit_breaker_opened_total,
    llm_cost_limit_reached_total,
    llm_fallback_depth,
    llm_max_depth_reached_total,
    llm_model_error_total,
    llm_query_cost_usd,
    llm_quota_exhausted_total,
    llm_service_unavailable_total,
    metrics_collector,
)
from backend.app.utils.tracing import set_span_attribute, set_span_status, trace_span
from backend.llm.claude_oauth_client import (
    LEDGER_PROVIDER,
    ClaudeOAuthError,
    ClaudeOAuthNotAvailable,
    collect_seats,
    complete_async,
)
from backend.llm.claude_oauth_client import default_model as claude_default_model
from backend.llm.metrics_emitter import emit_llm_metric
from backend.services.llm_clients.pricing import TokenUsage, create_token_usage
from backend.services.rag.agentic.chat_session import ChatSession, MockChatSession

logger = logging.getLogger(__name__)

# Model Tier Constants
TIER_FLASH = 0  # Default tier
TIER_LITE = 1  # Alias for FLASH
TIER_PRO = 2  # Alias for FLASH (no separate pro tier)
TIER_FALLBACK = 3  # Fallback slug (same as primary unless FALLBACK_MODEL_NAME is set)

# The endpoint every gateway call is attributed to in the cost ledger.
GATEWAY_ENDPOINT = "rag.gateway.chat"

# A model slug env override is honoured only when it is a plain ``claude-*``
# slug (the same shape ``claude_oauth_client`` forwards to ``--model``). A
# legacy Gemini slug left in ``PRIMARY_MODEL_NAME`` is ignored, never an error.
_ALLOWED_CLAUDE_MODEL_RE = re.compile(r"^claude-[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")

# One argv string may not exceed ~128 KiB on Linux (MAX_ARG_STRLEN); keep the
# whole prompt under it by dropping the OLDEST history entries first.
_MAX_PROMPT_BYTES = 120_000


class InvalidPromptError(ValueError):
    """The prompt itself cannot be sent to the CLI (empty, oversized, embedded
    NUL byte, or invalid UTF-8). Not a provider failure: it must never count
    against a model's circuit breaker or trigger fallback."""


class PromptTooLargeError(InvalidPromptError):
    """The prompt cannot fit the argv limit even with no history. Not a provider
    failure: it must never count against a model's circuit breaker."""


# The ReAct loops also read a call out of free text (``ACTION: tool(...)``, a
# ``{"tool_name": ...}`` object, in any fencing or escaping the parsers accept).
# Spelling-based filters on that text were bypassed (JSON escapes, nested fences),
# so the check asks the SAME parser the loops use what it would execute.


def _defuse_unoffered_calls(text: str, allowed_tool_names: set[str]) -> str:
    """Return ``text``, or "" when the loops' own text parser would execute a tool
    outside ``allowed_tool_names`` from it.

    The probe response carries no structured parts, so only the text modes
    (regex, regex_degraded) are exercised: exactly what the loops fall back to.
    """
    if not text:
        return text
    from backend.services.rag.agentic._reasoning_loop_helpers import (
        parse_tool_calls_from_response,
    )

    # silent=True: the probe writes no log record, so a blocked call's argument
    # values (model-controlled, possibly client data) never reach the logs.
    tool_calls, _mode = parse_tool_calls_from_response(_plain_response(text), text, silent=True)
    if any(getattr(call, "tool_name", None) not in allowed_tool_names for call in tool_calls):
        return ""
    return text


# Structured ReAct turn. ``arguments_json`` is a JSON object serialised as a
# string so the schema stays flat and strict for every tool signature.
_REACT_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments_json": {"type": "string"},
                },
                "required": ["name", "arguments_json"],
                "additionalProperties": False,
            },
        },
        "final_answer": {"type": ["string", "null"]},
    },
    "required": ["thought", "tool_calls", "final_answer"],
    "additionalProperties": False,
}

_TOOL_PROTOCOL = """<tool_calling_protocol>
You may call the tools listed in <available_tools>. Answer ONLY with the JSON object required by the output schema:
- "thought": one short sentence on what you will do next.
- "tool_calls": the tools to call now, each with "name" (exactly one of the listed tool names) and "arguments_json" (a JSON object, serialised as a string, using only the parameters that tool declares). Use [] when you need no tool.
- "final_answer": null while you are calling tools; when "tool_calls" is [] and you have what you need, the complete final answer for the user.
Never invent a tool that is not listed.
</tool_calling_protocol>"""

# --- O1 (2026-07-25): operator alert on a durable lane failure -------------
# Real incident 2026-07-22 (Gemini prepay at zero): the agentic LLM could not
# drive retrieval, the WhatsApp bot silently abstained for hours and nobody was
# paged. The architecture stays fail-closed (no external LLM fallback), so this
# alert IS the mitigation. After Wave 2 the lane is the Claude Console seats;
# the signal is the structured ``Last error class:`` tail ``claude_oauth_client``
# puts on ``ClaudeOAuthError`` -- never a substring of a user's text, and a
# timeout, an empty output or a CLI crash is NOT a quota event (cicatrix #3).
_QUOTA_ALERT_DEDUP_KEY = "claude_console_quota_exhausted"
_LAST_ERROR_MARKER = "last error class:"


def _classify_quota_exhaustion(exc: Exception) -> str | None:
    """Return ``rate_limited`` / ``balance_depleted`` / ``auth_failed`` for a
    durable Console-seat failure, else None (not a quota/auth event at all).

    Only :class:`ClaudeOAuthError` qualifies. Rate-limit phrasing is checked
    BEFORE quota phrasing: a transient 429 clears in seconds, a depleted
    balance does not, and the wrong order raised a false CRITICAL once.
    """
    if not isinstance(exc, ClaudeOAuthError):
        return None
    text = str(exc).lower()
    _, marker, tail = text.partition(_LAST_ERROR_MARKER)
    if not marker:
        return None
    if "rate limited" in tail or "rate_limit" in tail:
        return "rate_limited"
    if "quota" in tail:
        return "balance_depleted"
    if "authentication" in tail or "model_access" in tail:
        return "auth_failed"
    return None


async def _alert_quota_exhausted(model_name: str, exc: Exception) -> None:
    """Best-effort operator alert on a durable Console-seat failure. Never
    raises: the caller re-raises ``exc`` unchanged, alert or no alert.

    No PII: metadata carries only the model slug, the error class name and the
    derived subtype -- never the user's query, a phone number or client data.
    """
    try:
        subtype = _classify_quota_exhaustion(exc)
        if subtype is None:
            return

        from backend.services.monitoring.alert_service import AlertLevel, get_alert_service

        subtype_text = {
            "balance_depleted": (
                "the Console org balance appears DEPLETED -- this fails EVERY request "
                "on that seat until topped up"
            ),
            "rate_limited": "transient rate limit -- should self-clear shortly",
            "auth_failed": "authentication or workspace pairing refused by the Console door",
        }[subtype]

        await get_alert_service().send_alert(
            title="Claude Console lane failing",
            message=(
                f"Model {model_name} failed on every Console seat: {subtype_text}. "
                "Architecture is fail-closed by design (no external LLM fallback) -- "
                "this alert IS the mitigation (spec O1)."
            ),
            level=AlertLevel.CRITICAL,
            metadata={
                "model": model_name,
                "error_class": type(exc).__name__,
                "subtype": subtype,
            },
            dedup_key=f"{_QUOTA_ALERT_DEDUP_KEY}:{subtype}",
        )
    except Exception as alert_exc:  # pragma: no cover - defensive, must never break the call path
        logger.warning(
            "Failed to send Console-lane alert for %s: %s", model_name, type(alert_exc).__name__
        )


def _resolve_gateway_model(env_var: str, fallback: str) -> str:
    """A ``claude-*`` slug from ``env_var``, else ``fallback``.

    Fly still carries ``PRIMARY_MODEL_NAME=gemini-2.5-flash`` until the legacy
    secrets are unset; a non-Claude value must be ignored quietly.
    """
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return fallback
    if _ALLOWED_CLAUDE_MODEL_RE.match(raw):
        return raw
    logger.info("%s is not a claude-* slug; using %s", env_var, fallback)
    return fallback


def _entry_text(item: dict) -> str:
    """The text of one history item, whether it uses ``parts`` or ``content``."""
    parts = item.get("parts")
    if isinstance(parts, list):
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
        if texts:
            return "\n".join(str(t) for t in texts)
    content = item.get("content")
    return str(content) if content else ""


def _role_label(role: Any) -> str:
    return "User" if role == "user" else "Assistant"


def _extract_history_entries(
    chat: Any,
    conversation_messages: list[dict] | None,
    current_message: str,
) -> list[tuple[str, str]]:
    """Prior turns as ``(label, text)``; never includes ``current_message``."""
    entries: list[tuple[str, str]] = []
    history = getattr(chat, "history", None) if chat is not None else None
    if isinstance(history, list):
        for item in history:
            if not isinstance(item, dict):
                continue
            text = _entry_text(item)
            if text:
                entries.append((_role_label(item.get("role")), text))
        return entries

    messages = list(conversation_messages or [])
    if (
        messages
        and isinstance(messages[-1], dict)
        and messages[-1].get("role") == "user"
        and messages[-1].get("content") == current_message
    ):
        messages = messages[:-1]
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = _entry_text(item)
        if text:
            entries.append((_role_label(item.get("role")), text))
    return entries


def _compact_tool_specs(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        specs.append(
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            }
        )
    return specs


def _build_claude_prompt(
    *,
    message: str,
    system_prompt: str,
    history_entries: list[tuple[str, str]],
    tools: list[dict[str, Any]] | None,
) -> str:
    """One prompt: system, tools, history, then ``message`` verbatim and last.

    ``message`` stays the final section byte for byte: the CRM scope note is a
    suffix of it and must reach the provider on every send.
    """
    head: list[str] = []
    if system_prompt and system_prompt.strip():
        head.append(f"<system_instructions>\n{system_prompt}\n</system_instructions>")
    if tools:
        head.append(
            "<available_tools>\n"
            f"{json.dumps(_compact_tool_specs(tools), ensure_ascii=False)}\n"
            "</available_tools>"
        )
        head.append(_TOOL_PROTOCOL)

    def _render(entries: list[tuple[str, str]]) -> str:
        sections = list(head)
        if entries:
            lines = "\n".join(f"{label}: {text}" for label, text in entries)
            sections.append(f"<conversation_history>\n{lines}\n</conversation_history>")
        sections.append(message)
        return "\n\n".join(sections)

    def _utf8_len(text: str) -> int:
        try:
            return len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidPromptError("prompt contains invalid UTF-8 surrogates") from exc

    entries = list(history_entries)
    prompt = _render(entries)
    while entries and _utf8_len(prompt) > _MAX_PROMPT_BYTES:
        entries.pop(0)
        prompt = _render(entries)
    prompt_bytes = _utf8_len(prompt)
    if prompt_bytes > _MAX_PROMPT_BYTES:
        # System prompt, tools and message alone are over the argv limit. Nothing
        # of them is cut (the CRM scope note is a suffix of the message): the call
        # is refused outright instead of failing later with E2BIG.
        raise PromptTooLargeError(
            f"prompt is {prompt_bytes} bytes without history; limit {_MAX_PROMPT_BYTES}"
        )
    if "\x00" in prompt:
        # Subprocess argv rejects embedded NUL bytes with ValueError at spawn;
        # reject here before the CLI runs so it never counts as a model strike.
        raise InvalidPromptError("prompt contains embedded NUL byte")
    return prompt


def _plain_response(text: str) -> Any:
    return SimpleNamespace(text=text, candidates=[], function_calls=None)


def _parse_react_response(response: Any, allowed_tool_names: set[str]) -> tuple[str, Any, bool]:
    """Turn a structured ReAct completion into ``(text, response_obj, has_calls)``.

    Tool calls become ``response_obj.candidates[0].content.parts[*].function_call``
    (``name``/``args``) so ``parse_tool_calls_from_response`` reads them as
    ``native``. A call to a tool outside ``allowed_tool_names`` is dropped (the
    per-request scoping is enforced here as well as in the prompt). With no
    call, a non-empty ``final_answer`` is returned as ``Final Answer: ...``.
    """
    raw_text = getattr(response, "text", "") or ""
    payload = getattr(response, "structured", None)
    if payload is None and raw_text.strip():
        candidate = raw_text.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate).strip()
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and ("tool_calls" in parsed or "final_answer" in parsed):
            payload = parsed
    if not isinstance(payload, dict):
        return raw_text, _plain_response(raw_text), False

    thought = str(payload.get("thought") or "").strip()
    parts: list[Any] = []
    raw_calls = payload.get("tool_calls")
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            tool_name = call.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            if tool_name not in allowed_tool_names:
                logger.warning("LLMGateway: dropped a call to a tool outside this request's scope")
                continue
            raw_args = call.get("arguments_json")
            if isinstance(raw_args, dict):
                args = raw_args
            elif raw_args is None or (isinstance(raw_args, str) and not raw_args.strip()):
                args = {}
            else:
                try:
                    args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    logger.warning("LLMGateway: malformed tool arguments (%s)", type(exc).__name__)
                    continue
                if not isinstance(args, dict):
                    logger.warning("LLMGateway: tool arguments were not a JSON object")
                    continue
            parts.append(
                SimpleNamespace(function_call=SimpleNamespace(name=tool_name, args=args), text=None)
            )

    if parts:
        response_obj = SimpleNamespace(
            text=thought,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
            function_calls=[p.function_call for p in parts],
        )
        return thought, response_obj, True

    final_answer = payload.get("final_answer")
    if isinstance(final_answer, str) and final_answer.strip():
        clean = final_answer.strip()
        text_out = clean if "Final Answer:" in clean else f"Final Answer: {clean}"
        return text_out, _plain_response(text_out), False
    return thought, _plain_response(thought), False


def _tool_turn_summary(thought: str, response_obj: Any) -> str:
    """What the model asked for, for the chat history of the next ReAct step."""
    calls: list[str] = []
    for part in response_obj.candidates[0].content.parts:
        fc = part.function_call
        try:
            args = json.dumps(fc.args, ensure_ascii=False)
        except (TypeError, ValueError):
            args = "{}"
        calls.append(f"{fc.name}({args})")
    summary = f"{thought}\n[called tools: {', '.join(calls)}]".strip()
    return summary[:2000]


class LLMGateway:
    """
    Unified gateway for LLM interactions over the Claude Console seats.

    Responsibilities:
    - Route requests to a model slug by tier (all tiers resolve to claude-* slugs)
    - Turn a ReAct step with tools into a structured, schema-validated turn
    - Cascade over the fallback chain with a circuit breaker and a cost cap
    - Provide a cheap health check

    Attributes:
        gemini_tools (list): Tool declarations (kept under their historical name)
        model_name_pro / model_name_flash / model_name_fallback (str): claude-* slugs
    """

    def __init__(self, gemini_tools: list | None = None) -> None:
        """Initialize the gateway.

        Args:
            gemini_tools: Optional tool declarations for the ReAct loop. The
                name is historical; the declarations are provider-neutral dicts.

        Note:
            ``PRIMARY_MODEL_NAME`` / ``FALLBACK_MODEL_NAME`` are honoured only
            when they are plain ``claude-*`` slugs.
        """
        self._gemini_tools = gemini_tools or []

        default_claude = claude_default_model()
        primary = _resolve_gateway_model("PRIMARY_MODEL_NAME", default_claude)
        fallback = _resolve_gateway_model("FALLBACK_MODEL_NAME", primary)
        self.model_name_pro = primary
        self.model_name_flash = primary
        self.model_name_fallback = fallback

        logger.info(
            "LLMGateway: Claude Console lane ready (%s primary, %s fallback)", primary, fallback
        )

        # Circuit breaker configuration using CircuitBreaker class
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._circuit_breaker_threshold = 5
        self._circuit_breaker_timeout = HttpTimeoutConstants.CIRCUIT_BREAKER_TIMEOUT  # seconds
        self._max_fallback_depth = 3
        self._max_fallback_cost_usd = 0.10  # Max $0.10 per query

    @property
    def _available(self) -> bool:
        """Whether this process has a Console seat to call.

        Tests can set ``gateway._available = True/False`` to override.
        """
        if "_available_override" in self.__dict__:
            return self.__dict__["_available_override"]
        return bool(collect_seats())

    @_available.setter
    def _available(self, value: bool) -> None:
        """Set availability for testing purposes."""
        self.__dict__["_available_override"] = value

    @property
    def gemini_tools(self) -> list:
        """Get the tool declarations used for the ReAct loop."""
        return self._gemini_tools

    def set_gemini_tools(self, tools: list) -> None:
        """Set or update the tool declarations (after construction)."""
        self._gemini_tools = tools or []
        logger.debug(f"LLMGateway: Updated gemini_tools ({len(self.gemini_tools)} tools)")

    def create_chat_with_history(
        self,
        history_to_use: list[dict] | None,
        model_tier: int = TIER_FLASH,
        system_instruction: str = "",
    ) -> "ChatSession":
        """
        Create a ChatSession with conversation history for multi-turn chat.

        The session is a pure in-memory history container: each call rebuilds
        its prompt from it, so no provider client is held.

        Args:
            history_to_use: Previous conversation messages [{role, content}, ...]
            model_tier: Model tier (TIER_FLASH, TIER_PRO, etc.)
            system_instruction: System prompt for the session

        Returns:
            ChatSession with history, or MockChatSession when no seat is configured
        """
        model_name = self._get_model_for_tier(model_tier)
        if not self._available:
            logger.warning("No Claude Console seat available, returning mock session")
            return MockChatSession(
                history=history_to_use or [],
                model=model_name,
                system_instruction=system_instruction,
            )

        history: list[dict] = []
        for msg in history_to_use or []:
            if not isinstance(msg, dict):
                continue
            role = "user" if msg.get("role") == "user" else "model"
            history.append({"role": role, "parts": [{"text": str(msg.get("content", ""))}]})

        return ChatSession(
            client=None,
            model=model_name,
            history=history,
            system_instruction=system_instruction,
        )

    def _get_model_for_tier(self, tier: int) -> str:
        """Get model name for given tier."""
        if tier == TIER_PRO:
            return self.model_name_pro
        if tier in (TIER_LITE, TIER_FALLBACK):
            return self.model_name_fallback
        return self.model_name_flash

    async def send_message(
        self,
        chat: Any,
        message: str,
        system_prompt: str = "",
        tier: int = TIER_FLASH,
        enable_function_calling: bool = True,
        conversation_messages: list[dict] | None = None,
        images: list[dict] | None = None,  # Accepted for compatibility; never forwarded.
        gemini_tools: list[dict] | None = None,
    ) -> tuple[str, str, Any, TokenUsage]:
        """Send a message with tier-based routing and a fallback chain.

        Args:
            chat: Active chat session (history container) or None
            message: User message or continuation prompt (sent verbatim, last)
            system_prompt: System instructions
            tier: Requested model tier (TIER_PRO=2, TIER_FLASH=0, TIER_LITE=1)
            enable_function_calling: Ask for a structured ReAct turn when tools exist
            conversation_messages: Conversation history when no ``chat`` is given
            images: Ignored. Cloud vision is disabled; nothing is forwarded.
            gemini_tools: PER-CALL override of the tool declarations (T-VIS).
                ``self._gemini_tools`` is shared by every concurrent request, so
                a caller narrows the tools for ONE call here instead of mutating
                it. ``None`` (default) uses the shared list; ``[]`` sends no tools
                and does NOT fall back.

        Returns:
            (response_text, model_name_used, response_object, token_usage). The
            response object exposes ``candidates[0].content.parts[*].function_call``
            when the model asked for tools.

        Raises:
            RuntimeError: If every model in the fallback chain fails.
        """
        query_cost_tracker = {"cost": 0.0, "depth": 0}
        t0 = time.perf_counter()
        try:
            text_content, model_used, response_obj, token_usage = await self._send_with_fallback(
                chat=chat,
                message=message,
                system_prompt=system_prompt,
                model_tier=tier,
                enable_function_calling=enable_function_calling,
                conversation_messages=conversation_messages or [],
                query_cost_tracker=query_cost_tracker,
                images=images,
                gemini_tools_override=gemini_tools,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000)
            provider = LEDGER_PROVIDER
            logger.info(
                "LLM gateway call",
                extra={
                    "provider": provider,
                    "model": model_used,
                    "tier": tier,
                    "latency_ms": latency_ms,
                    "prompt_tokens": token_usage.prompt_tokens,
                    "completion_tokens": token_usage.completion_tokens,
                },
            )
            await emit_llm_metric(
                provider=provider,
                model=model_used,
                latency_ms=latency_ms,
                prompt_tokens=token_usage.prompt_tokens,
                completion_tokens=token_usage.completion_tokens,
            )
            return (text_content, model_used, response_obj, token_usage)
        except InvalidPromptError as e:
            latency_ms = round((time.perf_counter() - t0) * 1000)
            logger.warning(
                "LLM prompt rejected before dispatch (%s)",
                type(e).__name__,
                extra={
                    "tier": tier,
                    "fallback_depth": query_cost_tracker["depth"],
                    "latency_ms": latency_ms,
                },
            )
            raise RuntimeError(f"All LLM models failed: {e}") from None
        except Exception as e:
            latency_ms = round((time.perf_counter() - t0) * 1000)
            logger.exception(
                "All LLM models failed",
                extra={
                    "tier": tier,
                    "fallback_depth": query_cost_tracker["depth"],
                    "total_cost": query_cost_tracker["cost"],
                    "latency_ms": latency_ms,
                },
            )
            llm_all_models_failed_total.inc()
            raise RuntimeError(f"All LLM models failed: {e}") from None

    def _get_circuit_breaker(self, model_name: str) -> CircuitBreaker:
        """Get or create circuit breaker for model."""
        if model_name not in self._circuit_breakers:
            self._circuit_breakers[model_name] = CircuitBreaker(
                failure_threshold=self._circuit_breaker_threshold,
                success_threshold=2,
                timeout=self._circuit_breaker_timeout,
                name=f"llm_{model_name}",
            )
        return self._circuit_breakers[model_name]

    def _is_circuit_open(self, model_name: str) -> bool:
        """Check if circuit breaker is open."""
        circuit = self._get_circuit_breaker(model_name)
        return circuit.is_open()

    def _record_success(self, model_name: str) -> None:
        """Record successful call."""
        circuit = self._get_circuit_breaker(model_name)
        circuit.record_success()

    def _record_failure(self, model_name: str, error: Exception) -> None:
        """Record failed call with error classification."""
        circuit = self._get_circuit_breaker(model_name)
        circuit.record_failure()

        # Classify error and log metrics
        _error_category, _error_severity = ErrorClassifier.classify_error(error)
        error_type = type(error).__name__

        # Log with structured context
        error_context = get_error_context(error, model=model_name)
        logger.warning("LLM call failed for %s", model_name, extra=error_context)

        # Record metrics if circuit opened
        if circuit.is_open():
            llm_circuit_breaker_opened_total.labels(
                model=model_name,
                error_type=error_type,
            ).inc()

    def _get_fallback_chain(self, model_tier: int) -> list[str]:
        """Get fallback chain for given tier."""
        chain = []
        if model_tier == TIER_PRO:
            chain.append(self.model_name_pro)

        # RAG-06 Fix: Ensure TIER_LITE (1) and TIER_PRO (2) also try the flash model
        if model_tier <= TIER_PRO and self.model_name_flash not in chain:
            chain.append(self.model_name_flash)

        if self.model_name_fallback not in chain:
            chain.append(self.model_name_fallback)

        return chain

    async def _send_with_fallback(
        self,
        chat: Any,
        message: str,
        system_prompt: str,
        model_tier: int,
        enable_function_calling: bool,
        conversation_messages: list[dict],
        query_cost_tracker: dict,
        images: list[dict] | None = None,
        gemini_tools_override: list[dict] | None = None,
    ) -> tuple[str, str, Any, TokenUsage]:
        """Send a message down the fallback chain.

        Honours the circuit breaker, the per-query cost cap and the maximum
        fallback depth. There is no third-party fallback: when every slug in the
        chain fails the call fails closed.

        Returns:
            (response_text, model_name_used, response_object, token_usage)

        Raises:
            RuntimeError: If all models in the chain fail.
        """
        models_to_try = self._get_fallback_chain(model_tier)

        for model_name in models_to_try:
            # Check circuit breaker
            if self._is_circuit_open(model_name):
                logger.debug("Circuit breaker OPEN for %s, skipping", model_name)
                llm_circuit_breaker_open_total.labels(model=model_name).inc()
                continue

            # Check cost limit
            if query_cost_tracker["cost"] >= self._max_fallback_cost_usd:
                logger.warning(
                    f"Cost limit reached ({query_cost_tracker['cost']:.4f} USD), "
                    f"stopping fallback cascade",
                )
                llm_cost_limit_reached_total.inc()
                break

            # Check fallback depth
            if query_cost_tracker["depth"] >= self._max_fallback_depth:
                logger.warning(
                    f"Max fallback depth reached ({query_cost_tracker['depth']}), stopping cascade",
                )
                llm_max_depth_reached_total.inc()
                break

            # Check if a seat is available
            if not self._available:
                continue

            try:
                text_content, response, token_usage = await self._call_model(
                    model_name,
                    with_tools=enable_function_calling,
                    chat=chat,
                    message=message,
                    images=images,
                    _system_prompt=system_prompt,
                    gemini_tools_override=gemini_tools_override,
                    conversation_messages=conversation_messages,
                )

                self._record_success(model_name)
                query_cost_tracker["cost"] += token_usage.cost_usd
                query_cost_tracker["depth"] += 1

                llm_fallback_depth.observe(query_cost_tracker["depth"])
                llm_query_cost_usd.observe(query_cost_tracker["cost"])

                logger.debug("LLMGateway: %s response received", model_name)
                return (text_content, model_name, response, token_usage)

            except InvalidPromptError as e:
                # Same prompt, same refusal on every slug: not a model failure,
                # so no circuit-breaker strike and no fallback attempt.
                logger.warning("LLMGateway: prompt rejected before CLI (%s)", type(e).__name__)
                raise

            except ClaudeOAuthError as e:
                self._record_failure(model_name, e)
                logger.warning("Console seats failed for %s", model_name)
                if _classify_quota_exhaustion(e) is not None:
                    llm_quota_exhausted_total.labels(model=model_name).inc()
                else:
                    llm_model_error_total.labels(
                        model=model_name, error_type=type(e).__name__
                    ).inc()
                metrics_collector.record_llm_fallback(model_name, "next_model")
                continue

            except (ClaudeOAuthNotAvailable, asyncio.TimeoutError) as e:
                self._record_failure(model_name, e)
                logger.warning(
                    "Claude Console unavailable for %s (%s)", model_name, type(e).__name__
                )
                llm_service_unavailable_total.labels(model=model_name).inc()
                metrics_collector.record_llm_fallback(model_name, "next_model")
                continue

            except Exception as e:
                self._record_failure(model_name, e)
                error_type = type(e).__name__
                logger.warning("Error with %s: %s", model_name, error_type)
                llm_model_error_total.labels(model=model_name, error_type=error_type).inc()
                metrics_collector.record_llm_fallback(model_name, "next_model")
                continue

        logger.error("LLMGateway: every model in the fallback chain failed (fail-closed)")
        raise RuntimeError("All Claude Console models in fallback chain failed") from None

    async def _call_model(
        self,
        model_name: str,
        with_tools: bool = False,
        chat: Any = None,
        message: str = "",
        images: list[dict] | None = None,
        _system_prompt: str = "",
        gemini_tools_override: list[dict] | None = None,
        conversation_messages: list[dict] | None = None,
    ) -> tuple[str, Any, TokenUsage]:
        """Call one model slug and return (text, response_object, token_usage)."""
        if not self._available:
            raise ClaudeOAuthNotAvailable("No Claude Console seats configured")

        # T-VIS: `gemini_tools_override` narrows the tool declarations for THIS call
        # only. `None` -> the shared list; `[]` -> no tools (must NOT fall back).
        effective_tools = (
            self._gemini_tools if gemini_tools_override is None else gemini_tools_override
        )

        if images:
            # Cloud vision is disabled (local OCR only): never serialise or forward bytes.
            logger.warning(
                "LLMGateway: ignoring %d image attachment(s); cloud vision is disabled",
                len(images),
            )

        effective_system = _system_prompt or getattr(chat, "system_instruction", "") or ""
        use_react_tools = bool(with_tools and effective_tools)
        history_entries = _extract_history_entries(chat, conversation_messages, message)
        prompt = _build_claude_prompt(
            message=message,
            system_prompt=effective_system if isinstance(effective_system, str) else "",
            history_entries=history_entries,
            tools=effective_tools if use_react_tools else None,
        )
        if not prompt.strip():
            raise InvalidPromptError("Prompt must be non-empty")

        with trace_span(
            "backend.llm.call",
            {
                "model": model_name,
                "with_tools": use_react_tools,
                "message_length": len(message),
                "has_images": bool(images),
                "image_count": len(images) if images else 0,
            },
        ):
            try:
                cl_resp = await complete_async(
                    prompt,
                    model=model_name,
                    endpoint=GATEWAY_ENDPOINT,
                    json_schema=_REACT_TURN_SCHEMA if use_react_tools else None,
                    timeout_s=HttpTimeoutConstants.MEDIUM_TIMEOUT,
                    total_timeout_s=HttpTimeoutConstants.LONG_TIMEOUT,
                )
            except Exception as gen_exc:
                # O1: best-effort operator alert on a durable lane failure, then
                # re-raise unchanged -- never alters the fallback cascade.
                await _alert_quota_exhausted(model_name, gen_exc)
                raise

            has_calls = False
            allowed = {t["name"] for t in effective_tools if isinstance(t, dict) and t.get("name")}
            if use_react_tools:
                text_content, response_obj, has_calls = _parse_react_response(cl_resp, allowed)
            else:
                text_content = cl_resp.text or ""
                response_obj = _plain_response(text_content)

            if with_tools:
                # The loop that sent this turn reads calls out of free text too.
                # Whatever the tools offered (none included), only offered names
                # may survive in it.
                defused = _defuse_unoffered_calls(
                    text_content, allowed if use_react_tools else set()
                )
                if defused != text_content:
                    logger.warning("LLMGateway: defused a text-form call to an unoffered tool")
                    text_content = defused
                    response_obj.text = defused

            if not text_content and not has_calls:
                logger.warning("LLMGateway: empty response from %s", model_name)

            # Keep the session history current for the next step.
            history = getattr(chat, "history", None) if chat is not None else None
            if isinstance(history, list):
                history.append({"role": "user", "parts": [{"text": message}]})
                if has_calls:
                    history.append(
                        {
                            "role": "model",
                            "parts": [{"text": _tool_turn_summary(text_content, response_obj)}],
                        }
                    )
                elif text_content:
                    history.append({"role": "model", "parts": [{"text": text_content}]})

            usage_basis = (
                json.dumps(cl_resp.structured, ensure_ascii=False)
                if cl_resp.structured is not None
                else (cl_resp.text or "")
            )
            token_usage = create_token_usage(
                prompt_tokens=max(1, len(prompt) // 4),
                completion_tokens=max(1, len(usage_basis) // 4),
                model=model_name,
            )
            # Downstream tracers (kg_orchestrator) read the token counts off the
            # response object under the provider-neutral ``usage_metadata`` name.
            response_obj.usage_metadata = SimpleNamespace(
                prompt_token_count=token_usage.prompt_tokens,
                candidates_token_count=token_usage.completion_tokens,
                total_token_count=token_usage.total_tokens,
            )
            set_span_attribute("prompt_tokens", token_usage.prompt_tokens)
            set_span_attribute("completion_tokens", token_usage.completion_tokens)
            set_span_attribute("cost_usd", token_usage.cost_usd)
            set_span_attribute("has_function_call", "true" if has_calls else "false")
            set_span_attribute("response_length", len(text_content))
            set_span_status("ok")
            return text_content, response_obj, token_usage

    async def health_check(self) -> dict[str, bool]:
        """Check the Console lane with one tiny completion.

        Returns:
            {"claude_console": bool, "primary": bool, "fallback": bool}. All False
            (and no CLI is spawned) when no seat is configured.
        """
        status = {"claude_console": False, "primary": False, "fallback": False}
        if not self._available:
            logger.warning("LLMGateway Health: no Claude Console seat configured")
            return status

        try:
            result = await complete_async(
                "Reply with OK",
                model=self.model_name_flash,
                endpoint=GATEWAY_ENDPOINT,
                max_output_tokens=16,
                timeout_s=HttpTimeoutConstants.HEALTH_CHECK_TIMEOUT,
                total_timeout_s=HttpTimeoutConstants.HEALTH_CHECK_TIMEOUT,
            )
            if (result.text or "").strip():
                status = {"claude_console": True, "primary": True, "fallback": True}
        except Exception as e:
            logger.warning("LLMGateway Health: check failed (%s)", type(e).__name__)
        return status

    async def close(self) -> None:
        """Shut down the gateway (no persistent client is held any more)."""
        logger.info("LLMGateway: shut down")
