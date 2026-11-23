"""Sampler graph host must match the token family (measurer arming, 2026-07-13).

GUILT context: the only known-valid production token is INSTAGRAM-Login
family — probed live: it answers 190 OAuthException on graph.facebook.com.
MetaGraphSampler's default graph_base is the facebook host, so building it
bare (the pre-fix scheduler_cli did) starves the measurer even WITH a valid
token in env. Mini's dead token failing 190 on BOTH hosts is a separate
problem (operator[secret]); this guards the code half.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from backend.services.measurer import scheduler_cli
from backend.services.measurer.meta_graph_sampler import DEFAULT_GRAPH_BASE, MetaGraphSampler
from backend.services.war_room.models import Platform, WarRoomPost

INSTAGRAM_BASE = "https://graph.instagram.com/v21.0"


def _clear_ig_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "IG_LONG_LIVED_TOKEN",
        "INSTAGRAM_ACCESS_TOKEN",
        "IG_GRAPH_BASE",
        "IG_TOKEN_FAMILY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_default_family_builds_instagram_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """GUILT: bare build used the facebook default → 190 for the live token."""
    _clear_ig_env(monkeypatch)
    monkeypatch.setenv("IG_LONG_LIVED_TOKEN", "token-under-test")

    samplers = scheduler_cli._build_samplers()

    assert len(samplers) == 1
    assert samplers[0].graph_base == INSTAGRAM_BASE


def test_facebook_family_keeps_sampler_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """INNOCENCE: an explicit facebook-family token keeps the historic host."""
    _clear_ig_env(monkeypatch)
    monkeypatch.setenv("IG_LONG_LIVED_TOKEN", "token-under-test")
    monkeypatch.setenv("IG_TOKEN_FAMILY", "facebook")

    samplers = scheduler_cli._build_samplers()

    assert len(samplers) == 1
    assert samplers[0].graph_base == DEFAULT_GRAPH_BASE.rstrip("/")


def test_explicit_graph_base_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ig_env(monkeypatch)
    monkeypatch.setenv("IG_LONG_LIVED_TOKEN", "token-under-test")
    monkeypatch.setenv("IG_TOKEN_FAMILY", "facebook")
    monkeypatch.setenv("IG_GRAPH_BASE", "https://graph.example.test/v99.0")

    samplers = scheduler_cli._build_samplers()

    assert samplers[0].graph_base == "https://graph.example.test/v99.0"


def test_no_token_builds_no_samplers(monkeypatch: pytest.MonkeyPatch) -> None:
    """INNOCENCE: token-tolerant — starving stays a warning, not a crash."""
    _clear_ig_env(monkeypatch)

    assert scheduler_cli._build_samplers() == []


@pytest.mark.asyncio
async def test_configured_logging_redacts_sampler_and_warning_tokens(caplog) -> None:
    token = "SYNTHETIC_TOKEN_VALUE_123"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sampler = MetaGraphSampler(access_token=token, http_client=client)
        post = WarRoomPost(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            draft_id=UUID("00000000-0000-0000-0000-000000000002"),
            platform=Platform.INSTAGRAM,
            post_external_id="synthetic-post",
            tone_register=None,
            published_at=datetime.now(timezone.utc),
        )
        with caplog.at_level(logging.INFO):
            scheduler_cli._configure_logging()
            await sampler.sample(post)
            logging.getLogger("synthetic.warning").warning(
                "request https://graph.example.test/?access_token=%s", token
            )

    assert token not in caplog.text
    assert "access_token=<redacted>" in caplog.text


def test_configured_logging_keeps_measurer_info(caplog) -> None:
    with caplog.at_level(logging.INFO):
        scheduler_cli._configure_logging()
        logging.getLogger("measurer.scheduler.cli").info("ordinary measurer sweep")

    assert "ordinary measurer sweep" in caplog.text
