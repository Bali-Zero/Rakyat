"""
The AskZantara blog widget must not promise a callback to an anonymous visitor.

Red-team 2026-11-20 against the live `/api/blog/ask`: 8 of 36 answers told the
visitor the team would contact them ("entro un'ora lavorativa", "I'll connect
you with the team", "I will ensure they are aware"). Nothing on that path
alerts a person and the visitor left no contact, so each one was a promise
nobody could keep. Two halves are pinned here, as for the Instagram overlay:

1. A "blog" overlay exists, forbids the promise, overrides the persona's
   ESCALATION example by coming after it, and gives the visitor the contacts
   to reach the team themselves.
2. `ask_zantara` declares `channel="blog"` to the orchestrator. Without that
   kwarg the overlay is inert (superscar #2, exists but not armed).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.app.core.config import settings
from backend.prompts.channel_overlays import (
    CHANNEL_CONFIGS,
    apply_channel_overlay,
    build_channel_context,
)


def test_blog_entry_exists_and_is_plain_text() -> None:
    config = CHANNEL_CONFIGS.get("blog")
    assert config is not None, "blog missing from CHANNEL_CONFIGS"
    # The widget renders whitespace-pre-wrap: markdown reaches the reader raw.
    assert config.markdown is False


def test_blog_overlay_forbids_the_callback_promise() -> None:
    block = build_channel_context("blog")
    assert block, "blog overlay must not be empty, or the widget keeps the old prompt"
    lowered = block.lower()
    assert "anonymous" in lowered
    assert "never" in lowered
    for promise in ("contact", "reach out", "connecting them", "flagged", "within any time"):
        assert promise in lowered, f"the rule must name the forbidden promise: {promise!r}"
    assert "overrides" in lowered, "the rule must say it beats the ESCALATION example"


def test_blog_overlay_gives_the_visitor_the_way_to_reach_the_team() -> None:
    block = build_channel_context("blog")
    assert settings.CLIENT_CONTACT_WHATSAPP in block
    assert "info@balizero.com" in block


def test_blog_overlay_comes_after_the_escalation_example() -> None:
    persona = (
        "ESCALATION: Let me connect you with the team for this — they'll "
        "reach out via WhatsApp within one business hour."
    )
    prompt = apply_channel_overlay(persona, "blog")
    assert prompt.startswith(persona)
    assert prompt.index("<channel_context>") > prompt.index("within one business hour")


def test_other_channels_are_unchanged() -> None:
    # Innocence: the WhatsApp path really alerts a human, so its overlay must
    # not inherit the anonymous-visitor rule; unknown channels stay empty.
    assert "anonymous" not in build_channel_context("whatsapp").lower()
    assert "anonymous" not in build_channel_context("website").lower()
    assert build_channel_context("unknown") == ""
    assert apply_channel_overlay("BASE", None) == "BASE"


def test_ask_zantara_declares_the_blog_channel(monkeypatch) -> None:
    from backend.app.routers import blog_ask

    fake_orch = SimpleNamespace(
        process_query=AsyncMock(
            return_value=SimpleNamespace(answer="ok", sources=[], evidence_score=0.5)
        )
    )
    monkeypatch.setattr(blog_ask, "get_blog_orchestrator", AsyncMock(return_value=fake_orch))

    body = blog_ask.BlogAskRequest(question="Can someone call me back about my KITAS?")
    response = asyncio.run(blog_ask.ask_zantara(SimpleNamespace(), body))

    assert response.answer == "ok"
    kwargs = fake_orch.process_query.await_args.kwargs
    assert kwargs["channel"] == "blog"
    assert kwargs["user_id"] == "blog_visitor"
