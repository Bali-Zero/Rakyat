"""An intel article is public only after its GitHub publish succeeded.

be-warroom-intel-1 (Steps 4/4b guard): when the GitHub publish fails,
``_publish_staging_item`` must write NOTHING public: zero ``news_items``
inserts, zero ``post_publish_queue`` inserts, and the response must report
the failure. Pre-fix both inserts ran under the ``item_id`` fallback slug, so
the retry (Step 5 keeps the item retryable) created a second public row under
the real slug: the NOT EXISTS guard is on slug.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.routers import intel_scraper as intel_scraper_module

ITEM_ID = "news_20261126_120000_1a2b3c4d"
REAL_SLUG = "contoh-article"

STAGING_DATA = {
    "title": "Contoh Article Title",
    "content": (
        "## Summary\nContoh summary.\n"
        "## Facts\nContoh facts.\n"
        "## Bali Zero Take\nContoh take.\n"
        "## Next Steps\n- Contoh step."
    ),
    "category": "news",
    "relevance_score": 80,
    "source_url": "https://example.test/contoh-article",
    "source_name": "Contoh Scraper",
}


def _fake_pool() -> tuple[MagicMock, MagicMock]:
    connection = MagicMock()
    connection.execute = AsyncMock()
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=connection)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquired
    return pool, connection


def _publish_result_success() -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        article_url=f"https://balizero.example.test/news/{REAL_SLUG}",
        commit_sha="contoh-commit-sha",
        mdx_path=f"apps/mouth/src/content/articles/news/{REAL_SLUG}.mdx",
        pull_request_number=1,
        auto_merge_enabled=True,
        image_path=None,
    )


def _publish_result_failure() -> SimpleNamespace:
    return SimpleNamespace(
        success=False,
        message="GitHub publish failed",
        error="Contoh GitHub write failure",
    )


def _staging_mock() -> MagicMock:
    mock_staging = MagicMock()
    mock_staging.load_staging_item.return_value = dict(STAGING_DATA)
    mock_staging.save_staging_item.return_value = None
    return mock_staging


# ---------------------------------------------------------------------------
# be-warroom-intel-1: public rows only after the GitHub publish succeeded
# ---------------------------------------------------------------------------


class TestPublishIsPublicOnlyAfterGithubSuccess:
    async def test_failed_github_publish_writes_no_public_rows(self, monkeypatch) -> None:
        """GUILT: GitHub publish failure -> zero news_items inserts, zero
        post_publish_queue inserts, response reports failure, and the staging
        item stays retryable (Step 5 semantics preserved)."""
        monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
        pool, connection = _fake_pool()
        mock_staging = _staging_mock()

        with (
            patch("backend.app.routers.intel_scraper.staging_service", mock_staging),
            patch("backend.app.routers.intel_scraper.intel_user_actions_total") as mock_metric,
            patch(
                "backend.app.routers.intel_scraper.ingest_intel_to_qdrant",
                new=AsyncMock(return_value=True),
            ),
            patch("backend.app.routers.intel_scraper.invalidate_cache", new=AsyncMock()),
            patch(
                "backend.app.routers.article_composer.publish_article_internal",
                new=AsyncMock(return_value=_publish_result_failure()),
            ),
        ):
            mock_metric.labels.return_value.inc = MagicMock()

            result = await intel_scraper_module.publish_staging_item_internal(
                "news", ITEM_ID, actor="test", pool=pool
            )

        assert result["success"] is False
        assert result["github_published"] is False
        assert "NOT published" in result["message"]
        statements = [call.args[0] for call in connection.execute.await_args_list]
        assert not any("INSERT INTO news_items" in statement for statement in statements)
        assert not any("INSERT INTO post_publish_queue" in statement for statement in statements)
        connection.execute.assert_not_awaited()
        # Retry semantics preserved: the item goes back to pending, so the
        # next attempt re-runs the publish under the real slug.
        persisted = mock_staging.save_staging_item.call_args[0][2]
        assert persisted["status"] == "pending"

    async def test_successful_github_publish_writes_one_public_row_each(self, monkeypatch) -> None:
        """INNOCENCE: GitHub publish success -> exactly one news_items insert
        and one post_publish_queue insert, both under the real slug."""
        monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
        pool, connection = _fake_pool()
        mock_staging = _staging_mock()

        with (
            patch("backend.app.routers.intel_scraper.staging_service", mock_staging),
            patch("backend.app.routers.intel_scraper.intel_user_actions_total") as mock_metric,
            patch(
                "backend.app.routers.intel_scraper.ingest_intel_to_qdrant",
                new=AsyncMock(return_value=True),
            ),
            patch("backend.app.routers.intel_scraper.invalidate_cache", new=AsyncMock()),
            patch(
                "backend.app.routers.article_composer.publish_article_internal",
                new=AsyncMock(return_value=_publish_result_success()),
            ),
        ):
            mock_metric.labels.return_value.inc = MagicMock()

            result = await intel_scraper_module.publish_staging_item_internal(
                "news", ITEM_ID, actor="test", pool=pool
            )

        assert result["success"] is True
        calls_by_statement = {
            call.args[0]: call.args for call in connection.execute.await_args_list
        }
        news_statements = [
            statement for statement in calls_by_statement if "INSERT INTO news_items" in statement
        ]
        queue_statements = [
            statement
            for statement in calls_by_statement
            if "INSERT INTO post_publish_queue" in statement
        ]
        assert len(news_statements) == 1
        assert len(queue_statements) == 1
        news_args = calls_by_statement[news_statements[0]]
        queue_args = calls_by_statement[queue_statements[0]]
        # args: (statement, title, slug, ..., external_id)
        assert news_args[2] == REAL_SLUG
        assert news_args[12] == ITEM_ID
        assert queue_args[1] == REAL_SLUG
