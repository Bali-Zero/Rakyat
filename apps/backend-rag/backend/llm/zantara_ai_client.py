"""
ZANTARA AI Client - conversational engine behind ``app.state.ai_client``.

Since Wave 2 Stage 3 (2026-11-27, owner order) every call goes through the
Claude Console door (``backend.llm.claude_oauth_client``: the linked-organization
keys pinned to their workspaces, included monthly Ultra credit, no card spend).
There is no Gemini route left in this module and no Google credential is read.

Cost ledger: ``complete_async`` already records every call (real envelope usage,
provider ``claude_console``, endpoint ``zantara_ai.chat``); this module writes
no second row.

Logging: counts and exception class names only. A prompt, a message or a model
answer is client data and never reaches a log record from here.
"""

import asyncio
import logging
import os
import re
from typing import Any

from backend.app.core.config import settings
from backend.app.core.constants import HttpTimeoutConstants
from backend.llm.claude_oauth_client import (
    LEDGER_PROVIDER,
    ClaudeOAuthError,
    ClaudeOAuthNotAvailable,
    collect_seats,
    complete_async,
)
from backend.llm.claude_oauth_client import default_model as claude_default_model
from backend.llm.fallback_messages import get_fallback_message
from backend.llm.prompt_manager import PromptManager
from backend.llm.retry_handler import RetryHandler, _classify_error, _compute_delay
from backend.llm.token_estimator import TokenEstimator

logger = logging.getLogger(__name__)

# The endpoint every call of this client is attributed to in the cost ledger.
ZANTARA_ENDPOINT = "zantara_ai.chat"

# A model slug override is honoured only when it is a plain ``claude-*`` slug
# (the shape ``claude_oauth_client`` forwards to ``--model``). A legacy Gemini
# slug left in an env var or a caller argument is ignored, never an error.
_ALLOWED_CLAUDE_MODEL_RE = re.compile(r"^claude-[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")

# One argv string may not exceed ~128 KiB on Linux: keep the whole prompt under
# it by dropping the OLDEST history entries first.
_MAX_PROMPT_BYTES = 120_000

# The structured tail ``claude_oauth_client`` puts on ``ClaudeOAuthError``.
_LAST_ERROR_MARKER = "last error class:"


# Constants
class ZantaraAIClientConstants:
    """Constants for ZantaraAIClient configuration."""

    MAX_RETRIES = 3
    BASE_RETRY_DELAY = 2.0
    RETRY_BACKOFF_FACTOR = 2
    MOCK_STREAM_DELAY = 0.05
    FALLBACK_STREAM_DELAY = 0.1
    DEFAULT_MAX_TOKENS = 8192  # Maximum tokens for comprehensive responses
    DEFAULT_TEMPERATURE = 0.4  # Lower for more factual, consistent responses
    DEFAULT_STREAM_MAX_TOKENS = (
        8192  # Aligned with DEFAULT_MAX_TOKENS - streaming needs same capacity
    )
    MAX_TEMPERATURE = 2.0
    MIN_TEMPERATURE = 0.0
    MAX_MAX_TOKENS = 8192
    MIN_MAX_TOKENS = 1


def _resolve_model(explicit_model: str | None) -> str:
    """A ``claude-*`` slug from the caller or ``CHANNEL_MODEL_NAME``, else the default."""
    if explicit_model:
        if _ALLOWED_CLAUDE_MODEL_RE.match(explicit_model):
            return explicit_model
        logger.info("ZantaraAIClient: ignoring a non claude-* model argument")
    raw = os.getenv("CHANNEL_MODEL_NAME", "").strip()
    if raw:
        if _ALLOWED_CLAUDE_MODEL_RE.match(raw):
            return raw
        logger.info("ZantaraAIClient: CHANNEL_MODEL_NAME is not a claude-* slug; ignored")
    return claude_default_model()


def _is_terminal_auth_or_quota_error(exc: Exception) -> bool:
    """True for a durable Console-seat failure (auth, model access, depleted quota).

    A transient rate limit is NOT terminal: it clears in seconds and is retried.
    Only :class:`ClaudeOAuthError` qualifies, read from its structured tail.
    """
    if not isinstance(exc, ClaudeOAuthError):
        return False
    _, marker, tail = str(exc).lower().partition(_LAST_ERROR_MARKER)
    if not marker:
        return False
    if "rate limited" in tail or "rate_limit" in tail:
        return False
    return "authentication" in tail or "model_access" in tail or "quota" in tail


def _utf8_len(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("prompt contains invalid UTF-8 surrogates") from exc


def _build_prompt(system: str, messages: list[dict[str, str]]) -> str:
    """One prompt: system, history, then the last message verbatim and last.

    Raises ``ValueError`` (before any CLI is spawned) for a prompt the CLI can
    never accept: no non-system message, blank, over the argv limit even with no
    history, an embedded NUL byte, or invalid UTF-8.
    """
    turns: list[tuple[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        turns.append(("User" if role == "user" else "Assistant", content))
    if not turns:
        raise ValueError("messages must contain at least one non-system message")

    last_message = turns[-1][1]
    if not last_message.strip():
        # A system prompt alone must never make a blank question a valid prompt.
        raise ValueError("prompt must be non-empty")
    entries = turns[:-1]
    head: list[str] = []
    if system and system.strip():
        head.append(f"<system_instructions>\n{system}\n</system_instructions>")

    def _render(history: list[tuple[str, str]]) -> str:
        sections = list(head)
        if history:
            lines = "\n".join(f"{label}: {text}" for label, text in history)
            sections.append(f"<conversation_history>\n{lines}\n</conversation_history>")
        sections.append(last_message)
        return "\n\n".join(sections)

    prompt = _render(entries)
    while entries and _utf8_len(prompt) > _MAX_PROMPT_BYTES:
        entries.pop(0)
        prompt = _render(entries)
    if _utf8_len(prompt) > _MAX_PROMPT_BYTES:
        raise ValueError(f"prompt exceeds {_MAX_PROMPT_BYTES} bytes without history")
    if "\x00" in prompt:
        raise ValueError("prompt contains embedded NUL byte")
    if not prompt.strip():
        raise ValueError("prompt must be non-empty")
    return prompt


class ZantaraAIClient:
    """
    ZANTARA AI Client – conversational engine for the chat surfaces.

    Runs on the Claude Console lane (``claude_oauth_client``). Handles chat
    completion, streaming (the answer arrives whole, then is yielded), error
    handling with retry logic and token estimation for the response payload.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        """
        Initialize ZantaraAIClient.

        Args:
            api_key: Accepted for backward compatibility and ignored: the Console
                seats come from ``ANTHROPIC_CONSOLE_KEY_<n>`` /
                ``ANTHROPIC_CONSOLE_WORKSPACE_<n>``.
            model: ``claude-*`` model slug (defaults to the lane default).

        Raises:
            ValueError: If no Console seat is configured in production.
        """
        self.mock_mode = False

        if not collect_seats():
            if (settings.environment or "").lower() == "production":
                logger.critical("No Claude Console seat configured in PRODUCTION environment")
                raise ValueError(
                    "No Claude Console seats configured in production "
                    "(set ANTHROPIC_CONSOLE_KEY_<n> and ANTHROPIC_CONSOLE_WORKSPACE_<n>)"
                )
            logger.warning(
                "No Claude Console seat configured - defaulting to MOCK MODE (Development only)"
            )
            self.mock_mode = True

        self.model = _resolve_model(model)

        # Initialize pricing even in mock mode
        self.pricing = {
            "input": getattr(settings, "zantara_ai_cost_input", 0.15),
            "output": getattr(settings, "zantara_ai_cost_output", 0.60),
        }

        # Initialize helper services
        self.prompt_manager = PromptManager()
        self.retry_handler = RetryHandler(
            max_retries=ZantaraAIClientConstants.MAX_RETRIES,
            base_delay=ZantaraAIClientConstants.BASE_RETRY_DELAY,
            backoff_factor=ZantaraAIClientConstants.RETRY_BACKOFF_FACTOR,
        )
        self.token_estimator = TokenEstimator(model=self.model)

        logger.info("✅ ZantaraAIClient initialized")
        logger.info("   Engine model: %s", self.model)
        logger.info("   Mode: %s", "Mock" if self.mock_mode else LEDGER_PROVIDER)

    def get_model_info(self) -> dict[str, Any]:
        """
        Get current model information.

        Returns:
            Dictionary with model, provider, and pricing info
        """
        return {
            "model": self.model,
            "provider": "mock" if self.mock_mode else LEDGER_PROVIDER,
            "pricing": self.pricing,
        }

    def _build_system_prompt(
        self,
        memory_context: str | None = None,
        identity_context: str | None = None,
        use_rich_prompt: bool = True,
    ) -> str:
        """
        Build ZANTARA system prompt with context injection.

        Delegates to PromptManager for actual building.

        Args:
            memory_context: Optional memory/RAG context to inject
            identity_context: Optional user identity context
            use_rich_prompt: Use rich prompt from file (default: True)

        Returns:
            System prompt string with all context properly structured
        """
        return self.prompt_manager.build_system_prompt(
            memory_context=memory_context,
            identity_context=identity_context,
            use_rich_prompt=use_rich_prompt,
        )

    def _validate_inputs(
        self,
        max_tokens: int | None = None,
        temperature: float | None = None,
        messages: list[dict[str, str]] | None = None,
    ) -> None:
        """
        Validate input parameters.

        Args:
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            messages: List of messages

        Raises:
            ValueError: If validation fails
        """
        if max_tokens is not None:
            if max_tokens < ZantaraAIClientConstants.MIN_MAX_TOKENS:
                raise ValueError(f"max_tokens must be >= {ZantaraAIClientConstants.MIN_MAX_TOKENS}")
            if max_tokens > ZantaraAIClientConstants.MAX_MAX_TOKENS:
                raise ValueError(f"max_tokens must be <= {ZantaraAIClientConstants.MAX_MAX_TOKENS}")

        if temperature is not None:
            if temperature < ZantaraAIClientConstants.MIN_TEMPERATURE:
                raise ValueError(
                    f"temperature must be >= {ZantaraAIClientConstants.MIN_TEMPERATURE}",
                )
            if temperature > ZantaraAIClientConstants.MAX_TEMPERATURE:
                raise ValueError(
                    f"temperature must be <= {ZantaraAIClientConstants.MAX_TEMPERATURE}",
                )

        if messages is not None:
            if not isinstance(messages, list):
                raise ValueError("messages must be a list")
            if len(messages) == 0:
                raise ValueError("messages must contain at least one message")
            for msg in messages:
                if not isinstance(msg, dict):
                    raise ValueError("Each message must be a dictionary")
                if "role" not in msg or "content" not in msg:
                    raise ValueError("Each message must have 'role' and 'content' keys")
                if not isinstance(msg["content"], str):
                    raise ValueError("Each message content must be a string")

    def _estimate_tokens(
        self,
        messages: list[dict[str, str]],
        response_text: str,
    ) -> dict[str, int]:
        """
        Estimate token counts for input and output.

        Args:
            messages: Input messages
            response_text: Output text

        Returns:
            Dictionary with 'input' and 'output' token counts
        """
        tokens_input = self.token_estimator.estimate_messages_tokens(messages)
        tokens_output = self.token_estimator.estimate_tokens(response_text)
        return {"input": tokens_input, "output": tokens_output}

    async def chat_async(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = ZantaraAIClientConstants.DEFAULT_MAX_TOKENS,
        temperature: float = ZantaraAIClientConstants.DEFAULT_TEMPERATURE,
        system: str | None = None,
        memory_context: str | None = None,
        identity_context: str | None = None,
        safety_settings: list[dict] | None = None,
    ) -> dict[str, Any]:
        """
        Generate chat response using ZANTARA AI.

        Args:
            messages: Chat messages [{"role": "user", "content": "..."}]
            max_tokens: Max tokens to generate
            temperature: Accepted for compatibility; the lane does not take one
            system: Optional system prompt override
            memory_context: Optional memory context to inject
            identity_context: Optional user identity context
            safety_settings: Accepted for compatibility; ignored

        Returns:
            Dictionary with 'text', 'model', 'provider', 'tokens', 'cost'

        Raises:
            ValueError: If input validation fails or the prompt cannot be sent
            ClaudeOAuthNotAvailable: If no Console seat is configured
            ClaudeOAuthError: If every Console seat failed
        """
        # Validate inputs
        self._validate_inputs(max_tokens=max_tokens, temperature=temperature, messages=messages)

        # Build system prompt with all contexts
        if system is None:
            system = self._build_system_prompt(
                memory_context=memory_context,
                identity_context=identity_context,
            )

        # Validate the assembled prompt BEFORE any routing: a prompt the CLI can
        # never accept is refused the same way whatever the seat state.
        prompt = _build_prompt(system, messages)
        logger.debug(
            "chat_async prompt assembled (system_chars=%d, messages=%d)", len(system), len(messages)
        )

        # Handle Mock Mode
        if self.mock_mode:
            return {
                "text": "This is a MOCK response from ZantaraAIClient (Mock Mode).",
                "model": self.model,
                "provider": "mock",
                "tokens": {"input": 10, "output": 10},
                "cost": 0.0,
            }

        if not collect_seats():
            raise ClaudeOAuthNotAvailable("No Claude Console seats configured")

        try:
            resp = await complete_async(
                prompt,
                model=self.model,
                endpoint=ZANTARA_ENDPOINT,
                max_output_tokens=max_tokens,
                timeout_s=HttpTimeoutConstants.MEDIUM_TIMEOUT,
                total_timeout_s=HttpTimeoutConstants.LONG_TIMEOUT,
            )
        except Exception as e:
            logger.error("❌ Claude Console call failed (%s)", type(e).__name__)
            raise

        answer = resp.text or ""
        tokens = self._estimate_tokens(messages, answer)
        return {
            "text": answer,
            "model": self.model,
            "provider": LEDGER_PROVIDER,
            "tokens": tokens,
            "cost": 0.0,
        }

    async def stream(
        self,
        message: str,
        user_id: str,
        conversation_history: list[dict[str, str]] | None = None,
        memory_context: str | None = None,
        identity_context: str | None = None,
        max_tokens: int = ZantaraAIClientConstants.DEFAULT_STREAM_MAX_TOKENS,
        language: str = "en",
    ):
        """
        Stream chat response for SSE.

        The Console lane answers whole: the text is yielded in one chunk once the
        completion returns. Retries, fallback messages and mock mode keep the
        contract the callers already rely on.

        Args:
            message: User message
            user_id: User identifier
            conversation_history: Optional chat history
            memory_context: Optional memory context
            identity_context: Optional user identity context
            max_tokens: Max tokens
            language: Language for fallback messages (default: 'en')

        Yields:
            str: Text chunks of the answer (or of a fallback message)
        """
        logger.info("🌊 [ZantaraAI] Starting stream")

        # Validate inputs
        self._validate_inputs(
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": message}],
        )

        # Build system prompt
        system = self._build_system_prompt(
            memory_context=memory_context,
            identity_context=identity_context,
        )

        all_messages: list[dict[str, str]] = []
        for msg in conversation_history or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", "")
            all_messages.append(
                {
                    "role": msg.get("role", "user"),
                    "content": content if isinstance(content, str) else str(content),
                }
            )
        all_messages.append({"role": "user", "content": message})

        # An invalid prompt raises here, once, before any retry or routing.
        prompt = _build_prompt(system, all_messages)
        logger.debug(
            "stream prompt assembled (system_chars=%d, messages=%d)", len(system), len(all_messages)
        )

        # Mock Mode
        if self.mock_mode:
            logger.info("🎭 [ZantaraAI] MOCK MODE streaming")
            response = (
                "This is a MOCK stream response. "
                "In production mode, this runs on the Claude Console lane."
            )
            for word in response.split():
                yield word + " "
                await asyncio.sleep(ZantaraAIClientConstants.MOCK_STREAM_DELAY)
            return

        if not collect_seats():
            logger.error("❌ No Claude Console seat available for streaming")
            for word in get_fallback_message("service_unavailable", language).split():
                yield word + " "
                await asyncio.sleep(ZantaraAIClientConstants.FALLBACK_STREAM_DELAY)
            return

        # Retry with the shared classification/delay logic of RetryHandler.
        last_exception: Exception | None = None
        for attempt in range(self.retry_handler.max_retries):
            try:
                resp = await complete_async(
                    prompt,
                    model=self.model,
                    endpoint=ZANTARA_ENDPOINT,
                    max_output_tokens=max_tokens,
                    timeout_s=HttpTimeoutConstants.MEDIUM_TIMEOUT,
                    total_timeout_s=HttpTimeoutConstants.LONG_TIMEOUT,
                )
                text = resp.text or ""
                if text.strip():
                    logger.info("✅ [ZantaraAI] Stream completed successfully")
                    yield text
                    return
                logger.warning("⚠️ [ZantaraAI] No content received from completion")
                last_exception = ValueError("No content received from completion")

            except ClaudeOAuthNotAvailable:
                logger.error("❌ [ZantaraAI] Claude Console unavailable")
                for word in get_fallback_message("service_unavailable", language).split():
                    yield word + " "
                    await asyncio.sleep(ZantaraAIClientConstants.FALLBACK_STREAM_DELAY)
                return

            except ClaudeOAuthError as e:
                if _is_terminal_auth_or_quota_error(e):
                    logger.critical("🚨 [ZantaraAI] Console seats refused (auth or quota)")
                    for word in get_fallback_message("api_key_error", language).split():
                        yield word + " "
                        await asyncio.sleep(ZantaraAIClientConstants.FALLBACK_STREAM_DELAY)
                    return
                last_exception = e

            except Exception as e:
                last_exception = e

            # Retry decision — shared classification/delay logic from RetryHandler
            if attempt < self.retry_handler.max_retries - 1:
                err_class = _classify_error(str(last_exception or ""))
                if err_class == "permanent":
                    logger.error(
                        "LLM stream permanent error",
                        extra={"attempt": attempt + 1, "error_type": type(last_exception).__name__},
                    )
                    break
                delay = _compute_delay(
                    err_class,
                    attempt,
                    self.retry_handler.base_delay,
                    self.retry_handler.backoff_factor,
                )
                logger.warning(
                    "LLM stream retry",
                    extra={
                        "attempt": attempt + 1,
                        "max_retries": self.retry_handler.max_retries,
                        "error_class": err_class,
                        "delay_s": round(delay, 2),
                        "error_type": type(last_exception).__name__,
                    },
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "LLM stream exhausted",
                    extra={"error_type": type(last_exception).__name__},
                )

        # If all retries failed, send fallback
        for word in get_fallback_message("connection_error", language).split():
            yield word + " "
            await asyncio.sleep(ZantaraAIClientConstants.FALLBACK_STREAM_DELAY)

    async def conversational(
        self,
        message: str,
        user_id: str,
        conversation_history: list[dict[str, str]] | None = None,
        memory_context: str | None = None,
        identity_context: str | None = None,
        max_tokens: int = ZantaraAIClientConstants.DEFAULT_STREAM_MAX_TOKENS,
    ) -> dict[str, Any]:
        """
        Compatible interface for IntelligentRouter - simple conversational response.

        Args:
            message: User message
            user_id: User identifier (unused, kept for compatibility)
            conversation_history: Optional chat history
            memory_context: Optional memory context
            identity_context: Optional user identity context
            max_tokens: Max tokens

        Returns:
            Dictionary with 'text', 'model', 'provider', 'ai_used', 'tokens'
        """
        messages = []
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": message})

        result = await self.chat_async(
            messages=messages,
            max_tokens=max_tokens,
            memory_context=memory_context,
            identity_context=identity_context,
        )

        return {
            "text": result["text"],
            "model": result["model"],
            "provider": result["provider"],
            "ai_used": "zantara-ai",
            "tokens": result["tokens"],
        }

    async def conversational_with_tools(
        self,
        message: str,
        user_id: str,
        conversation_history: list[dict[str, str]] | None = None,
        memory_context: str | None = None,
        identity_context: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        _tool_executor: Any | None = None,
        max_tokens: int = ZantaraAIClientConstants.DEFAULT_STREAM_MAX_TOKENS,
        _max_tool_iterations: int = 2,
    ) -> dict[str, Any]:
        """
        Compatible interface for IntelligentRouter - conversational WITH tool calling.

        Note: tool calling is not implemented on this client; it falls back to
        the standard conversational answer when tools are provided.

        Args:
            message: User message
            user_id: User identifier
            conversation_history: Optional chat history
            memory_context: Optional memory context
            identity_context: Optional user identity context
            tools: Optional list of tool definitions (not used)
            _tool_executor: Tool executor (unused, kept for compatibility)
            max_tokens: Max tokens
            _max_tool_iterations: Max tool iterations (unused)

        Returns:
            Dictionary with 'text', 'model', 'provider', 'ai_used', 'tokens', 'tools_called', 'used_tools'
        """
        if tools:
            logger.info(
                "🔧 [ZantaraAI] Tool use requested but not supported here, using standard conversational",
            )

        result = await self.conversational(
            message=message,
            user_id=user_id,
            conversation_history=conversation_history,
            memory_context=memory_context,
            identity_context=identity_context,
            max_tokens=max_tokens,
        )
        result["tools_called"] = []
        result["used_tools"] = False
        return result

    def is_available(self) -> bool:
        """
        Check if ZANTARA AI is configured and available.

        Returns:
            True in mock mode, else True when a Console seat is configured
        """
        if self.mock_mode:
            return True
        return bool(collect_seats())

    async def close(self) -> None:
        """Shut down the AI client (nothing is held open; kept for the lifecycle)."""
        logger.info("✅ ZantaraAIClient: shut down")
