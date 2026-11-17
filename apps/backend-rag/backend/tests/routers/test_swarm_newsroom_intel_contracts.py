"""Contract tests for the Newsroom / Blog / Intel vertical (swarm API parity audit).

Each test pins one back-end side of a front-to-back contract fix:

* DEFECT-3C-02  newsletter ``frequency="monthly"`` is accepted (UI offers it)
* DEFECT-3A-08  ``POST /api/blog/newsletter/log`` accepts the legacy snake_case
  query string AND a JSON body in camelCase or snake_case
* DEFECT-3C-03  legacy UI categories (visas/living/trends/taxes) normalise instead of 422
* DEFECT-3D-02  an upstream LLM credential failure is a 503, never a 401
  (a 401 makes the mouth client log the staff user out)
* DEFECT-3D-01  main_api registers the slowapi limiter + RateLimitExceeded handler
* DEFECT-3C-05  ``POST /api/blog/ask`` accepts the widget's ``context`` field and
  returns sources with an optional ``url``
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_database_pool
from backend.app.routers import blog_ask, newsletter
from backend.llm.deepseek_client import DeepSeekAuthError
from backend.services.article_composer.error_handler import (
    ErrorCode,
    handle_anthropic_error,
)
from backend.services.article_composer.validators import (
    ComposeRequestValidator,
    validate_category,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Newsletter
# --------------------------------------------------------------------------- #


def _pool_with_conn(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()

    class _AcquireCtx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    pool.acquire = MagicMock(side_effect=lambda: _AcquireCtx())
    return pool


@pytest.mark.parametrize("frequency", ["instant", "daily", "weekly", "monthly"])
def test_subscribe_frequency_accepts_every_ui_choice(frequency: str) -> None:
    assert newsletter.SubscribeRequest.validate_frequency(frequency) == frequency


def test_subscribe_frequency_still_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        newsletter.SubscribeRequest.validate_frequency("yearly")


@pytest.fixture
def newsletter_client():
    app = FastAPI()
    app.include_router(newsletter.router)
    conn = AsyncMock()
    conn.execute = AsyncMock()
    app.dependency_overrides[get_database_pool] = lambda: _pool_with_conn(conn)
    app.dependency_overrides[newsletter.get_admin_user] = lambda: {"role": "admin"}
    with patch.object(newsletter, "invalidate_cache", new=AsyncMock()):
        yield TestClient(app), conn
    app.dependency_overrides.clear()


def _insert_args(conn: AsyncMock) -> tuple:
    assert conn.execute.await_count == 1
    return conn.execute.await_args.args[1:]


def test_log_accepts_legacy_snake_case_query_params(newsletter_client) -> None:
    client, conn = newsletter_client
    response = client.post(
        "/api/blog/newsletter/log",
        params={
            "article_id": "art-1",
            "recipient_count": 10,
            "sent_count": 9,
            "failed_count": 1,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert _insert_args(conn) == ("art-1", 10, 9, 1)


def test_log_accepts_camel_case_json_body(newsletter_client) -> None:
    client, conn = newsletter_client
    response = client.post(
        "/api/blog/newsletter/log",
        json={
            "articleId": "art-2",
            "recipientCount": 5,
            "sentCount": 4,
            "failedCount": 1,
        },
    )
    assert response.status_code == 200
    assert _insert_args(conn) == ("art-2", 5, 4, 1)


def test_log_accepts_snake_case_json_body(newsletter_client) -> None:
    client, conn = newsletter_client
    response = client.post(
        "/api/blog/newsletter/log",
        json={
            "article_id": "art-3",
            "recipient_count": 3,
            "sent_count": 3,
            "failed_count": 0,
        },
    )
    assert response.status_code == 200
    assert _insert_args(conn) == ("art-3", 3, 3, 0)


def test_log_rejects_when_fields_missing_from_both_sources(newsletter_client) -> None:
    client, conn = newsletter_client
    response = client.post("/api/blog/newsletter/log", json={"articleId": "art-4"})
    assert response.status_code == 422
    assert "recipient_count" in response.json()["detail"]
    conn.execute.assert_not_awaited()


def test_log_keeps_zero_counts_valid(newsletter_client) -> None:
    client, conn = newsletter_client
    response = client.post(
        "/api/blog/newsletter/log",
        json={
            "articleId": "art-5",
            "recipientCount": 0,
            "sentCount": 0,
            "failedCount": 0,
        },
    )
    assert response.status_code == 200
    assert _insert_args(conn) == ("art-5", 0, 0, 0)


# --------------------------------------------------------------------------- #
# Article composer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("visas", "immigration"),
        ("living", "lifestyle"),
        ("trends", "tech"),
        ("taxes", "tax"),
        ("Visas ", "immigration"),
        # Canonical values are untouched.
        ("immigration", "immigration"),
        ("business", "business"),
        ("tax", "tax"),
        ("legal", "tax"),
        ("tax-legal", "tax"),
    ],
)
def test_validate_category_normalises_legacy_ui_values(raw: str, expected: str) -> None:
    assert validate_category(raw) == expected


def test_validate_category_still_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Invalid category"):
        validate_category("sports")


def test_compose_request_accepts_legacy_ui_category() -> None:
    request = ComposeRequestValidator(
        title="Legacy dropdown category title",
        content="Indonesia business and immigration update. " * 4,
        category="visas",
    )
    assert request.category == "immigration"


def test_llm_credential_failure_is_503_not_401() -> None:
    exc = handle_anthropic_error(
        DeepSeekAuthError("provider credential rejected"),
        article_title="Title",
        category="business",
        request_id="req-1",
    )
    assert exc.status_code == 503
    assert exc.status_code != 401
    assert exc.detail["code"] == ErrorCode.API_KEY_NOT_CONFIGURED
    assert exc.detail["request_id"] == "req-1"


def test_main_api_registers_article_composer_rate_limiter() -> None:
    """The light `api` process serves /api/articles/compose; without the slowapi
    handler a tripped 10/minute limit surfaced as a 500."""
    source = (BACKEND_ROOT / "app" / "main_api.py").read_text(encoding="utf-8")
    assert "api.state.limiter = article_composer.limiter" in source
    assert "api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)" in source
    # Registered after the routers and before the catch-all proxy router.
    assert source.index("include_light_routers(api)") < source.index(
        "api.state.limiter = article_composer.limiter"
    )
    assert source.index("api.state.limiter = article_composer.limiter") < source.index(
        "api.include_router(create_proxy_router())"
    )


def test_publish_position_still_accepts_homepage_slots_and_legacy_values() -> None:
    from backend.app.routers.article_composer import (
        HOMEPAGE_LAYOUT_SLOTS,
        UNPINNED_POSITIONS,
    )

    assert {"hero_main", "insight_1", "insight_3"} <= HOMEPAGE_LAYOUT_SLOTS
    assert {"normal", "latest", "main_featured", "secondary"} <= UNPINNED_POSITIONS


def test_legacy_category_publishes_into_canonical_folder() -> None:
    from backend.app.routers.article_composer import (
        BaliZeroTake,
        EnrichedArticle,
        NextSteps,
        TLDRSection,
        generate_mdx_content,
    )

    article = EnrichedArticle(
        title="Legacy category article",
        headline="Legacy category headline",
        tldr=TLDRSection(what="A change"),
        facts="Facts. " * 20,
        bali_zero_take=BaliZeroTake(hidden_insight="", our_analysis="", our_advice=""),
        next_steps=NextSteps(),
        category="visas",
        priority="medium",
        relevance_score=50,
        ai_summary="Summary",
        ai_tags=["tag"],
        suggested_components=[],
        source="Marketing Team",
        source_url=None,
        enriched_at="2026-01-01T00:00:00+00:00",
    )
    mdx = generate_mdx_content(article, "legacy-category", None)
    assert 'category: "immigration"' in mdx


# --------------------------------------------------------------------------- #
# Blog ask (AskZantara)
# --------------------------------------------------------------------------- #


def test_blog_ask_request_accepts_widget_context_string() -> None:
    body = blog_ask.BlogAskRequest(question="What changed?", context="KITAS renewal")
    assert body.article_title == "KITAS renewal"


def test_blog_ask_request_context_dict_fills_missing_article_fields() -> None:
    body = blog_ask.BlogAskRequest(
        question="What changed?",
        context={"title": "Tax update", "category": "tax"},
    )
    assert body.article_title == "Tax update"
    assert body.article_category == "tax"


def test_blog_ask_explicit_fields_win_over_context() -> None:
    body = blog_ask.BlogAskRequest(
        question="q",
        article_title="Explicit",
        context={"title": "From context"},
    )
    assert body.article_title == "Explicit"


def test_blog_ask_returns_sources_with_optional_url() -> None:
    app = FastAPI()
    app.include_router(blog_ask.router)
    orchestrator = SimpleNamespace(
        process_query=AsyncMock(
            return_value=SimpleNamespace(
                answer="Answer",
                sources=[
                    {"title": "With url", "content": "body", "url": "https://example.invalid/a"},
                    {"title": "No url", "content": "snippet text"},
                ],
            )
        )
    )
    with patch.object(blog_ask, "get_blog_orchestrator", new=AsyncMock(return_value=orchestrator)):
        response = TestClient(app).post(
            "/api/blog/ask",
            json={"question": "What changed?", "context": "Visa article"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["answer"] == "Answer"
    assert data["sources"][0] == {
        "title": "With url",
        "snippet": "body",
        "url": "https://example.invalid/a",
    }
    assert data["sources"][1] == {"title": "No url", "snippet": "snippet text", "url": None}
    # The widget context was folded into the RAG query.
    query = orchestrator.process_query.await_args.kwargs["query"]
    assert "Visa article" in query
