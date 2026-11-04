"""Publish-time content guard (E33 Second-Home "5-10 years") on both hook points.

GUILT: a violating article gets HTTP 409 and Qdrant / news_items / commit / pings
are never reached. INNOCENCE: a clean article runs exactly the calls it ran before.
"""

from __future__ import annotations

import io
import json
import os
import random
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.routers import intel, intel_scraper
from backend.app.routers.article_composer import (
    BaliZeroTake,
    EnrichedArticle,
    NextSteps,
    PublishRequest,
    TLDRSection,
    publish_article,
    publish_article_internal,
)
from backend.services import content_guards
from backend.services.content_guards import (
    GUARD_IDS,
    ArticleGuardViolation,
    check_public_article,
)
from backend.services.visa_check import e33_article_guard

BAD = "The Second Home Visa is valid for 5-10 years."
CLEAN_BODY = (
    "## Summary\nTax update.\n## Facts\nDetails.\n## Bali Zero Take\nImpact.\n## Next Steps\nAct."
)


def _staging(content: str, title: str = "A clean article") -> dict[str, object]:
    return {"title": title, "content": content, "category": "business", "relevance_score": 80}


def _pool() -> tuple[MagicMock, MagicMock]:
    connection = MagicMock()
    connection.execute = AsyncMock()
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=connection)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquired
    return pool, connection


def _publish_response() -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        article_url="https://balizero.com/business/clean",
        commit_sha="abc123",
        mdx_path="apps/mouth/src/content/articles/business/clean.mdx",
        pull_request_number=1,
        auto_merge_enabled=True,
        image_path=None,
        error=None,
        message="ok",
    )


async def _run_staging(data: dict[str, object], pool: MagicMock):
    from backend.app.routers.intel_scraper import publish_staging_item_internal

    with (
        patch("backend.app.routers.intel_scraper.staging_service") as staging,
        patch("backend.app.routers.intel_scraper.intel_user_actions_total") as metric,
        patch(
            "backend.app.routers.intel_scraper.ingest_intel_to_qdrant",
            new=AsyncMock(return_value=True),
        ) as qdrant,
        patch("backend.app.routers.intel_scraper.invalidate_cache", new=AsyncMock()),
        patch.dict(os.environ, {"FIREWORKS_API_KEY": ""}),
        patch(
            "backend.app.routers.article_composer.publish_article_internal",
            new=AsyncMock(return_value=_publish_response()),
        ) as composer,
    ):
        staging.load_staging_item.return_value = data
        metric.labels.return_value.inc = MagicMock()
        try:
            result = await publish_staging_item_internal(
                "news", "news_guard_1", actor="test", allow_generated_cover=True, pool=pool
            )
            error = None
        except HTTPException as exc:
            result, error = None, exc
    return result, error, staging, qdrant, composer


class TestMovedRuleIsTheSameObject:
    def test_test_module_reimports_the_guard_objects(self) -> None:
        from backend.tests.services.visa_check import test_e33_forbidden_claims as t

        for name in (
            "_find_second_home_predicated_5_10_offenders",
            "_SECOND_HOME_DURATION_RE",
            "_GOLDEN_VISA_RE",
            "_SECOND_HOME_VISA_RE",
            "_nearest_marker_distance",
        ):
            assert getattr(t, name) is getattr(e33_article_guard, name)

    def test_guard_ids_are_the_published_set(self) -> None:
        assert GUARD_IDS == (content_guards.E33_SECOND_HOME_5_10_YEARS,)


class TestCheckPublicArticle:
    def test_guilt_names_guard_field_line_phrase(self) -> None:
        hits = check_public_article({"title": "ok", "content": "first line\n" + BAD})
        assert len(hits) == 1
        hit = hits[0]
        assert (hit.guard, hit.field, hit.line, hit.phrase) == (
            "e33_second_home_5_10_years",
            "content",
            2,
            "5-10 years",
        )
        assert hit.rule

    def test_innocence_golden_visa_range_and_unrelated_text(self) -> None:
        assert check_public_article({"content": "The Golden Visa runs 5-10 years."}) == []
        assert check_public_article({"content": "Document prep takes 5-10 days."}) == []
        assert check_public_article({"content": None, "n": 3}) == []  # type: ignore[dict-item]

    def test_fields_are_scanned_independently(self) -> None:
        split = {"a": "Second Home Visa", "b": "valid 5-10 years"}
        assert check_public_article(split) == []


class TestStagingHook:
    @pytest.mark.asyncio
    async def test_guilt_blocks_before_anything_runs(self) -> None:
        pool, connection = _pool()
        result, error, staging, qdrant, composer = await _run_staging(
            _staging(CLEAN_BODY + "\n" + BAD), pool
        )

        assert result is None
        assert error is not None and error.status_code == 409
        assert error.detail["message"] == "Article blocked by content guard"
        assert error.detail["guard"] == "e33_second_home_5_10_years"
        assert error.detail["field"] == "content"
        assert error.detail["phrase"] == "5-10 years"
        assert {"line", "rule"} <= set(error.detail)
        qdrant.assert_not_awaited()
        composer.assert_not_awaited()
        connection.execute.assert_not_awaited()
        staging.save_staging_item.assert_not_called()

    @pytest.mark.asyncio
    async def test_innocence_clean_article_runs_todays_calls(self) -> None:
        pool, connection = _pool()
        result, error, staging, qdrant, composer = await _run_staging(_staging(CLEAN_BODY), pool)

        assert error is None and result is not None and result["success"] is True
        qdrant.assert_awaited_once()
        composer.assert_awaited_once()
        statements = [call.args[0] for call in connection.execute.await_args_list]
        assert any("INSERT INTO news_items" in s for s in statements)
        assert any("INSERT INTO post_publish_queue" in s for s in statements)
        staging.save_staging_item.assert_called_once()


def _article(facts: str) -> EnrichedArticle:
    return EnrichedArticle(
        title="Guard article",
        headline="Guard article headline",
        tldr=TLDRSection(should_worry="No", what="w", who="all", when="now", risk_level="Low"),
        facts=facts,
        bali_zero_take=BaliZeroTake(hidden_insight="h", our_analysis="a", our_advice="d"),
        next_steps=NextSteps(expat=["x"], investor=["y"]),
        category="business",
        priority="low",
        relevance_score=50,
        ai_summary="s",
        ai_tags=["t"],
        suggested_components=[],
        cover_image=None,
        source="src",
        source_url="https://example.com/n",
        enriched_at=datetime.now(timezone.utc).isoformat(),
    )


def _publisher() -> MagicMock:
    publisher = MagicMock()
    publisher.is_configured = True
    publisher.check_file_exists = AsyncMock(return_value=False)
    publisher.create_commit_with_files = AsyncMock(
        return_value={"commit_sha": "c0ffee1", "pull_request_number": 7, "auto_merge_enabled": True}
    )
    return publisher


class TestMdxHook:
    @pytest.mark.asyncio
    async def test_guilt_raises_before_commit_and_pings(self) -> None:
        publisher = _publisher()
        http = MagicMock()
        with (
            patch("backend.services.integrations.github_publisher.github_publisher", publisher),
            patch("httpx.AsyncClient", http),
        ):
            with pytest.raises(ArticleGuardViolation) as raised:
                await publish_article_internal(
                    PublishRequest(article=_article(BAD), position="latest", slug="guard-1")
                )
        assert raised.value.hits[0].field == "mdx_content"
        publisher.create_commit_with_files.assert_not_awaited()
        publisher.check_file_exists.assert_not_awaited()
        http.assert_not_called()

    @pytest.mark.asyncio
    async def test_public_route_maps_violation_to_409(self) -> None:
        publisher = _publisher()
        with (
            patch("backend.services.integrations.github_publisher.github_publisher", publisher),
            patch("backend.app.utils.crm_utils.is_crm_admin", return_value=True),
        ):
            with pytest.raises(HTTPException) as raised:
                await publish_article(
                    PublishRequest(article=_article(BAD), position="latest", slug="guard-2"),
                    current_user={"email": "a@b.c"},
                )
        assert raised.value.status_code == 409

    @pytest.mark.asyncio
    async def test_innocence_clean_article_still_commits(self) -> None:
        publisher = _publisher()
        with (
            patch("backend.services.integrations.github_publisher.github_publisher", publisher),
            patch("httpx.AsyncClient") as http,
        ):
            http.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(return_value=MagicMock(status_code=200)))
            )
            http.return_value.__aexit__ = AsyncMock(return_value=False)
            response = await publish_article_internal(
                PublishRequest(article=_article("Plain facts."), position="latest", slug="clean-1")
            )
        assert response.success is True
        publisher.create_commit_with_files.assert_awaited_once()


SECOND_HOME_TITLE = "Second Home Visa update"
SECOND_HOME_CLAIM = "The permit is valid for 5-10 years."

HIT_SHAPES: dict[str, dict[str, object]] = {
    "content": _staging(CLEAN_BODY + "\n" + BAD),
    "title": _staging(CLEAN_BODY, title="Second Home Visa valid 5-10 years"),
    "cross_field_title_and_content": _staging(
        CLEAN_BODY.replace("Details.", SECOND_HOME_CLAIM), title=SECOND_HOME_TITLE
    ),
    "seo_description": {**_staging(CLEAN_BODY), "seo_description": BAD},
    "cover_image_alt": {
        **_staging(CLEAN_BODY, title=SECOND_HOME_TITLE),
        "cover_image_alt": "Visa valid 5-10 years",
    },
    "seo_title": {**_staging(CLEAN_BODY), "seo_title": BAD},
}

CLEAN_SHAPES: dict[str, dict[str, object]] = {
    "plain": _staging(CLEAN_BODY),
    "golden_visa_range": _staging(CLEAN_BODY + "\nThe Golden Visa runs 5-10 years."),
    "second_home_title_clean_body": _staging(CLEAN_BODY, title=SECOND_HOME_TITLE),
}


async def _mdx_hook_blocks(data: dict[str, object]) -> bool:
    """Would ``publish_article_internal`` block the article staging would publish?"""
    from backend.app.routers.intel_scraper import _build_enriched_article

    publisher = _publisher()
    with (
        patch("backend.services.integrations.github_publisher.github_publisher", publisher),
        patch("httpx.AsyncClient") as http,
    ):
        http.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(post=AsyncMock(return_value=MagicMock(status_code=200)))
        )
        http.return_value.__aexit__ = AsyncMock(return_value=False)
        try:
            await publish_article_internal(
                PublishRequest(
                    article=_build_enriched_article(data), position="latest", slug="prop-1"
                )
            )
        except ArticleGuardViolation:
            publisher.create_commit_with_files.assert_not_awaited()
            return True
    return False


class TestStagingEvaluatesWhatTheMdxHookEvaluates:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", sorted(HIT_SHAPES))
    async def test_guilt_blocks_at_staging_with_zero_side_effects(self, shape: str) -> None:
        pool, connection = _pool()
        result, error, staging, qdrant, composer = await _run_staging(HIT_SHAPES[shape], pool)

        assert result is None
        assert error is not None and error.status_code == 409
        assert error.detail["message"] == "Article blocked by content guard"
        assert error.detail["guard"] == "e33_second_home_5_10_years"
        assert {"field", "line", "phrase", "rule"} <= set(error.detail)
        qdrant.assert_not_awaited()
        composer.assert_not_awaited()
        connection.execute.assert_not_awaited()
        staging.save_staging_item.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", sorted(HIT_SHAPES) + sorted(CLEAN_SHAPES))
    async def test_staging_blocks_iff_the_mdx_hook_blocks(self, shape: str) -> None:
        data = {**HIT_SHAPES, **CLEAN_SHAPES}[shape]
        pool, _ = _pool()
        _, error, _, qdrant, _ = await _run_staging(data, pool)

        staging_blocks = error is not None and error.status_code == 409
        assert staging_blocks == await _mdx_hook_blocks(data)
        assert (qdrant.await_count == 0) == staging_blocks

    @pytest.mark.asyncio
    async def test_embed_text_only_hit_is_blocked_before_the_ingest(self) -> None:
        data = _staging(CLEAN_BODY + "\n" + SECOND_HOME_CLAIM, title=SECOND_HOME_TITLE)
        _, error, _, qdrant, _ = await _run_staging(data, _pool()[0])

        assert error is not None and error.status_code == 409
        assert error.detail["field"] == "qdrant_embed_text"
        qdrant.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cross_field_hit_is_the_mdx_rendering_not_a_single_field(self) -> None:
        data = HIT_SHAPES["cross_field_title_and_content"]
        assert check_public_article({"title": data["title"]}) == []
        assert check_public_article({"content": data["content"]}) == []
        _, error, *_ = await _run_staging(data, _pool()[0])
        assert error is not None and error.status_code == 409


@pytest.mark.asyncio
async def test_capabilities_list_the_guard_ids(monkeypatch) -> None:
    monkeypatch.setattr(intel.os, "access", lambda *_args: True)
    result = await intel.workspace_marketing_capabilities()
    assert result["content_guards"] == list(GUARD_IDS)


GUARD_409_KEYS = {"message", "guard", "field", "line", "phrase", "rule"}
SIDE_EFFECTS = (
    "qdrant.ingest",
    "validator.add",
    "github.check_file_exists",
    "github.create_commit",
    "http.post",
    "db.execute",
    "staging.save",
    "fireworks.urlopen",
)
LONG_TITLE = (
    "Immigration circular update what the new rules change for retirees remote workers "
    "and long stay families in Bali"
)
DOOR_BODY = (
    "## Summary\nThe Second Home route gets a new circular.\n"
    "## Facts\nCompanies with turnover under the threshold file quarterly. "
    * 4
    + "\n## Bali Zero Take\nPlan early.\n## Next Steps\nCheck your NIB."
)


def _door_item(cover: str | None, **over: object) -> dict[str, object]:
    item: dict[str, object] = {
        "title": "Quarterly filing for small PT PMA",
        "content": DOOR_BODY.replace("Second Home route", "tax route"),
        "category": "business",
        "relevance_score": 80,
        "status": "pending",
        "source_url": "https://example.org/news/1",
        "source_name": "Example",
        "seo_title": "Quarterly filing rule",
        "seo_description": "What changes for small companies",
        "cover_image_alt": "Office desk",
        "brief": {"what": "Quarterly filing", "who": "PT PMA"},
        "tags": ["tax"],
    }
    if cover:
        item["cover_image"] = cover
    item.update(over)
    return item


def _noise_jpeg() -> bytes:
    from PIL import Image

    rnd = random.Random(7)
    img = Image.frombytes(
        "RGB", (320, 180), bytes(rnd.getrandbits(8) for _ in range(320 * 180 * 3))
    )
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def edge(tmp_path, monkeypatch):
    """Real staging store and real publish doors; fakes only at the outside edges."""
    events: list[tuple[str, object]] = []
    svc = intel.staging_service
    monkeypatch.setattr(svc, "base_staging_dir", tmp_path)
    monkeypatch.setattr(svc, "news_staging_dir", tmp_path / "news")
    monkeypatch.setattr(svc, "visa_staging_dir", tmp_path / "visa")
    (tmp_path / "news").mkdir()
    (tmp_path / "visa").mkdir()
    real_save, real_cas = svc.save_staging_item, svc.compare_and_set_status
    in_cas = [False]

    def save(kind, item_id, data, *args, **kwargs):
        if not in_cas[0]:
            events.append(("staging.save", item_id))
        return real_save(kind, item_id, data, *args, **kwargs)

    def cas(kind, item_id, **kwargs):
        events.append(("staging.cas", kwargs.get("new_status")))
        in_cas[0] = True
        try:
            return real_cas(kind, item_id, **kwargs)
        finally:
            in_cas[0] = False

    monkeypatch.setattr(svc, "save_staging_item", save)
    monkeypatch.setattr(svc, "compare_and_set_status", cas)

    async def qdrant(item_id, kind):
        events.append(("qdrant.ingest", item_id))
        return True

    async def invalidate(pattern):
        events.append(("cache.invalidate", pattern))

    monkeypatch.setattr(intel_scraper, "ingest_intel_to_qdrant", qdrant)
    monkeypatch.setattr(intel_scraper, "invalidate_cache", invalidate)

    validator = types.ModuleType("claude_validator")

    class _Validator:
        @staticmethod
        def add_published_article(**kwargs):
            events.append(("validator.add", kwargs.get("title")))

    validator.ClaudeValidator = _Validator
    monkeypatch.setitem(sys.modules, "claude_validator", validator)

    publisher = MagicMock()
    publisher.is_configured = True

    async def exists(path, branch="main"):
        events.append(("github.check_file_exists", path))
        return False

    async def commit(files, message, **kwargs):
        events.append(("github.create_commit", [f["path"] for f in files]))
        return {"commit_sha": "c0ffee1234", "pull_request_number": 7, "auto_merge_enabled": True}

    publisher.check_file_exists = exists
    publisher.create_commit_with_files = commit
    monkeypatch.setattr(
        "backend.services.integrations.github_publisher.github_publisher", publisher
    )

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            events.append(("http.post", url))
            return MagicMock(status_code=200)

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    monkeypatch.setenv("GOOGLE_INDEXING_CREDENTIALS", json.dumps({"type": "service_account"}))
    credentials = MagicMock()
    credentials.token = "tok"
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        lambda *args, **kwargs: credentials,
    )
    jpeg = _noise_jpeg()

    def urlopen(req, data=None, timeout=None):
        events.append(("fireworks.urlopen", req.full_url))
        response = MagicMock()
        response.read = lambda: jpeg
        return response

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    connection = MagicMock()

    async def execute(sql, *args):
        events.append(("db.execute", " ".join(sql.split())[:40]))
        return "INSERT 0 1"

    connection.execute = execute
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=connection)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquired
    request = MagicMock()
    request.app.state.db_pool = pool
    cover = tmp_path / "news" / "cover.jpg"
    cover.write_bytes(_noise_jpeg())
    return types.SimpleNamespace(
        events=events,
        svc=svc,
        pool=pool,
        request=request,
        cover=str(cover),
        tmp=tmp_path,
        monkeypatch=monkeypatch,
    )


async def _publish_via(edge, door: str, item_id: str, over: dict[str, object]):
    """Run one REAL publish door; returns (result, error, item as left on disk)."""
    with_cover = door in ("damar", "damar_hero")
    edge.svc.save_staging_item(
        "news", item_id, _door_item(edge.cover if with_cover else None, **over)
    )
    edge.events.clear()
    edge.monkeypatch.setenv("FIREWORKS_API_KEY", "fk-test" if door == "internal_fireworks" else "")
    result = error = None
    try:
        if with_cover:
            body = intel.WorkspaceNewsPublishRequest(
                confirmation="DAMAR_CONFIRMED",
                position="hero_2" if door == "damar_hero" else "latest",
            )
            result = await intel.workspace_marketing_publish_news(item_id, body, edge.request)
        else:
            result = await intel_scraper.publish_staging_item_internal(
                "news", item_id, actor="test", allow_generated_cover=True, pool=edge.pool
            )
    except HTTPException as exc:
        error = exc
    disk = json.loads((edge.tmp / "news" / f"{item_id}.json").read_text())
    return result, error, disk


def _names(edge) -> list[str]:
    return [name for name, _ in edge.events]


class TestDamarRouteBriefAndCas:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("brief_key", ["what", "why"])
    async def test_brief_only_hit_is_blocked_with_zero_side_effects(self, edge, brief_key) -> None:
        brief = {
            "what": "Quarterly filing",
            "who": "PT PMA",
            brief_key: "Second Home Visa 5-10 years",
        }
        _, error, _ = await _publish_via(edge, "damar", "news_brief_1", {"brief": brief})

        assert error is not None and error.status_code == 409
        assert error.detail["message"] == "Article blocked by content guard"
        assert error.detail["field"] == f"brief.{brief_key}"
        assert not [n for n in _names(edge) if n in SIDE_EFFECTS], edge.events

    @pytest.mark.asyncio
    async def test_innocence_clean_brief_publishes(self, edge) -> None:
        brief = {"what": "Second Home Visa applications", "who": "all"}
        result, error, _ = await _publish_via(edge, "damar", "news_brief_2", {"brief": brief})

        assert error is None and result["success"] is True
        assert "qdrant.ingest" in _names(edge) and "github.create_commit" in _names(edge)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("door", ["damar", "damar_hero"])
    async def test_guard_409_releases_the_publishing_claim(self, edge, door) -> None:
        item = {"content": DOOR_BODY + "\nThe Second Home Visa is valid for 5-10 years."}
        _, error, disk = await _publish_via(edge, door, f"news_cas_{door}", item)

        assert error is not None and error.status_code == 409
        cas = [new_status for name, new_status in edge.events if name == "staging.cas"]
        assert cas == ["publishing", "pending"]
        assert disk["status"] == "pending"
        assert disk["last_publication_failed_at"]
        assert not [n for n in _names(edge) if n in SIDE_EFFECTS], edge.events


class TestGeneratedCoverPathIsRendered:
    @pytest.mark.asyncio
    async def test_fireworks_door_blocks_through_the_item_id_cover_path(self, edge) -> None:
        over = {
            "title": LONG_TITLE,
            "content": DOOR_BODY.replace("## Facts", "## Facts\nStructured update."),
            "cover_image_alt": "Valid 5-10 years",
        }
        _, error, disk = await _publish_via(edge, "internal_fireworks", "n1", over)

        assert error is not None and error.status_code == 409
        assert error.detail["guard"] == "e33_second_home_5_10_years"
        assert not [n for n in _names(edge) if n in SIDE_EFFECTS], edge.events
        assert disk["status"] == "pending"

    @pytest.mark.asyncio
    async def test_innocence_same_door_with_clean_alt_publishes(self, edge) -> None:
        over = {"title": LONG_TITLE, "content": DOOR_BODY, "cover_image_alt": "Valid for years"}
        result, error, _ = await _publish_via(edge, "internal_fireworks", "n1", over)

        assert error is None and result["success"] is True
        assert "fireworks.urlopen" in _names(edge)


class TestArticlesRoute409Shape:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "facts,extra",
        [
            ("The Second Home Visa is valid for 5-10 years.", {}),
            (
                "Plain facts.",
                {
                    "title": SECOND_HOME_TITLE,
                    "headline": SECOND_HOME_TITLE,
                    "cover_image_alt": "Valid 5-10 years",
                },
            ),
        ],
    )
    async def test_409_body_is_the_exact_guard_detail(self, facts, extra) -> None:
        publisher = _publisher()
        article = _article(facts).model_copy(update=extra)
        with (
            patch("backend.services.integrations.github_publisher.github_publisher", publisher),
            patch("backend.app.utils.crm_utils.is_crm_admin", return_value=True),
        ):
            with pytest.raises(HTTPException) as raised:
                await publish_article(
                    PublishRequest(article=article, position="latest", slug="guard-shape"),
                    current_user={"email": "a@b.c"},
                )

        detail = raised.value.detail
        assert raised.value.status_code == 409
        assert isinstance(detail, dict) and set(detail) == GUARD_409_KEYS
        assert detail["message"] == "Article blocked by content guard"
        assert detail["guard"] == "e33_second_home_5_10_years"
        assert isinstance(detail["field"], str) and isinstance(detail["phrase"], str)
        assert isinstance(detail["line"], int) and isinstance(detail["rule"], str)
        publisher.create_commit_with_files.assert_not_awaited()


class TestStaging409NamesTheHitField:
    SHAPES: dict[str, dict[str, object]] = {
        **HIT_SHAPES,
        "cover_image_alt_alone": {
            **_staging(CLEAN_BODY),
            "cover_image_alt": "Second Home Visa valid 5-10 years",
        },
        "brief_what": {**_staging(CLEAN_BODY), "brief": {"what": BAD}},
    }
    FIELD_OF = {
        "content": "content",
        "title": "title",
        "seo_description": "seo_description",
        "seo_title": "seo_title",
        "cover_image_alt_alone": "cover_image_alt",
        "brief_what": "brief.what",
        "cover_image_alt": "mdx_content",
        "cross_field_title_and_content": "qdrant_embed_text",
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", sorted(FIELD_OF))
    async def test_field_names_the_field_carrying_the_hit(self, shape) -> None:
        _, error, *_ = await _run_staging(self.SHAPES[shape], _pool()[0])

        assert error is not None and error.status_code == 409
        assert set(error.detail) == GUARD_409_KEYS
        assert error.detail["field"] == self.FIELD_OF[shape]
