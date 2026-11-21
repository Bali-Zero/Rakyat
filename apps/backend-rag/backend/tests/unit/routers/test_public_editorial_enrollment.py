"""One approved public item can cross the projection boundary; private visa items cannot."""

from __future__ import annotations

import base64
import copy
import io
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.routers import intel, intel_scraper
from backend.services.intel.intel_public_editorial import (
    editorial_fingerprint,
    enroll_public_editorial,
    is_public_editorial,
    update_public_editorial,
)
from backend.services.intel.intel_staging_service import IntelStagingService


@pytest.fixture
def staged(tmp_path, monkeypatch):
    service = IntelStagingService.__new__(IntelStagingService)
    service.base_staging_dir = tmp_path
    service.visa_staging_dir = tmp_path / "visa"
    service.news_staging_dir = tmp_path / "news"
    service.visa_staging_dir.mkdir()
    service.news_staging_dir.mkdir()
    item = {
        "item_id": "visa_public",
        "intel_type": "visa",
        "category": "immigration",
        "title": "A public immigration article",
        "content": "Public reporting. " * 30,
        "source_url": "https://public.example/article",
        "source_name": "Public source",
        "status": "pending",
        "detection_type": "scraper_auto",
        "extraction_method": "editorial_manual",
        "enrichment": {},
    }
    service.save_staging_item("visa", "visa_public", item)
    service.save_staging_item(
        "visa",
        "visa_private",
        {
            **item,
            "item_id": "visa_private",
            "source_url": "https://public.example/private-other",
        },
    )
    monkeypatch.setattr(intel, "staging_service", service)
    monkeypatch.setattr(intel_scraper, "staging_service", service)
    return service, item


def _enroll(service, item):
    return enroll_public_editorial(
        service,
        item["item_id"],
        item["source_url"],
        editorial_fingerprint(item),
    )


@pytest.mark.asyncio
async def test_only_explicitly_enrolled_id_is_visible_without_reclassification(staged):
    service, item = staged
    with pytest.raises(HTTPException, match="404"):
        await intel.workspace_marketing_news_article("visa_public")
    assert _enroll(service, item) is True
    assert _enroll(service, item) is False
    result = await intel.workspace_marketing_news_article("visa_public")
    assert result["content"] == item["content"] and result["category"] == "immigration"
    assert "workspace_editorial" not in result
    listed = await intel.workspace_marketing_pending_news(limit=1)
    assert listed["total"] == 1 and listed["items"][0]["item_id"] == "visa_public"
    assert service.load_staging_item("news", "visa_public") is None
    assert service.check_duplicate("visa", item["source_url"])["item_id"] == "visa_public"
    with pytest.raises(HTTPException, match="404"):
        await intel.workspace_marketing_news_article("visa_private")


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "publishing"},
        {"published_url": "https://public.example/live"},
        {"extraction_method": "private_document"},
        {"detection_type": "client_upload"},
        {"intel_type": "news"},
        {"item_id": "other"},
    ],
)
def test_ineligible_records_cannot_be_enrolled(staged, changes):
    service, item = staged
    item.update(changes)
    service.save_staging_item("visa", "visa_public", item)
    with pytest.raises(ValueError):
        enroll_public_editorial(
            service, "visa_public", item["source_url"], editorial_fingerprint(item)
        )


def test_source_and_reviewed_digest_must_match(staged):
    service, item = staged
    for url, digest in [
        ("https://public.example/other", editorial_fingerprint(item)),
        (item["source_url"], "0" * 64),
    ]:
        with pytest.raises(ValueError):
            enroll_public_editorial(service, "visa_public", url, digest)
    assert not is_public_editorial(service.load_staging_item("visa", "visa_public"), "visa_public")


def test_http_enrollment_requires_admin_and_fixed_confirmation(staged):
    service, item = staged
    app = FastAPI()
    app.include_router(intel.router)
    client = TestClient(app)
    path = "/api/intel/staging/visa/visa_public/workspace-editorial"
    body = {
        "confirmation": "REVIEWED_PUBLIC_EDITORIAL",
        "expected_source_url": item["source_url"],
        "expected_fingerprint": editorial_fingerprint(item),
    }
    assert (
        client.post(path, json=body, headers={"X-Workspace-Marketing-Key": "workspace"}).status_code
        == 401
    )
    app.dependency_overrides[intel.get_current_user] = lambda: {"role": "user"}
    assert client.post(path, json=body).status_code == 403
    app.dependency_overrides[intel.get_current_user] = lambda: {"role": "admin"}
    assert client.post(path, json={**body, "confirmation": "yes"}).status_code == 422
    assert client.post(path, json=body).status_code == 200
    assert client.post(path, json=body).json()["idempotent"] is True


@pytest.mark.asyncio
async def test_source_edit_invalidates_get_list_and_publish(staged, monkeypatch):
    service, item = staged
    _enroll(service, item)
    current = service.load_staging_item("visa", "visa_public")
    current["content"] = "Unreviewed replacement"
    service.save_staging_item("visa", "visa_public", current)
    publish = AsyncMock()
    monkeypatch.setattr(intel_scraper, "publish_staging_item_internal", publish)
    with pytest.raises(HTTPException, match="404"):
        await intel.workspace_marketing_news_article("visa_public")
    assert (await intel.workspace_marketing_pending_news())["total"] == 0
    with pytest.raises(HTTPException, match="404"):
        await intel.workspace_marketing_publish_news(
            "visa_public",
            intel.WorkspaceNewsPublishRequest(confirmation="DAMAR_CONFIRMED", position="latest"),
        )
    publish.assert_not_awaited()


def test_workspace_edit_renews_binding_and_stale_edit_or_lease_loses(staged):
    service, item = staged
    _enroll(service, item)
    old = editorial_fingerprint(item)
    update_public_editorial(service, "visa_public", old, {"title": "Reviewed revised title"})
    current = service.load_staging_item("visa", "visa_public")
    assert is_public_editorial(current, "visa_public")
    with pytest.raises(ValueError):
        update_public_editorial(service, "visa_public", old, {"content": "Stale edit"})
    acquired, _ = service.compare_and_set_status(
        "visa",
        "visa_public",
        expected={"pending"},
        new_status="publishing",
        expected_editorial_fingerprint=old,
    )
    assert acquired is False
    assert service.load_staging_item("visa", "visa_public")["status"] == "pending"


@pytest.mark.asyncio
async def test_cover_and_publish_keep_canonical_visa_identity(staged, monkeypatch):
    service, item = staged
    _enroll(service, item)
    from backend.services.integrations.github_publisher import github_publisher

    monkeypatch.setattr(github_publisher, "token", "test-only")
    monkeypatch.setattr(github_publisher, "check_file_exists", AsyncMock(return_value=False))
    await intel.workspace_marketing_update_news(
        "visa_public",
        intel.WorkspaceNewsUpdateRequest(
            title=item["title"],
            content=item["content"],
            category="immigration",
            seo_title="Public immigration update",
            seo_description="Public editorial description. " * 2,
            slug="public-immigration-update",
            cover_image_alt="Illustration for public immigration update",
        ),
    )
    image = Image.frombytes("RGB", (1200, 630), random.Random(7).randbytes(1200 * 630 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    await intel.workspace_marketing_attach_cover(
        "visa_public",
        intel.WorkspaceNewsCoverRequest(
            cover_image_base64=base64.b64encode(image_bytes).decode(),
            cover_image_filename="cover.png",
        ),
    )
    current = service.load_staging_item("visa", "visa_public")
    original_path = service.visa_staging_dir / current["cover_image"]
    (service.visa_staging_dir / "covers/visa_public.png").write_bytes(b"old generic writer")
    assert original_path.read_bytes() == image_bytes
    assert is_public_editorial(current, "visa_public")
    publish = AsyncMock(return_value={"success": True, "github_published": True})
    monkeypatch.setattr(intel_scraper, "publish_staging_item_internal", publish)
    await intel.workspace_marketing_publish_news(
        "visa_public",
        intel.WorkspaceNewsPublishRequest(confirmation="DAMAR_CONFIRMED", position="latest"),
    )
    assert publish.await_args.args == ("visa", "visa_public")
    assert publish.await_args.kwargs["require_public_editorial"] is True
    assert publish.await_args.kwargs["allow_generated_cover"] is False
    service.compare_and_set_status(
        "visa",
        "visa_public",
        expected={"publishing"},
        new_status="publication_pending",
        updates={"published_url": "https://public.example/live"},
    )
    assert (await intel.workspace_marketing_publication_status("visa_public"))[
        "status"
    ] == "publication_pending"
    confirmed = await intel.workspace_marketing_confirm_live(
        "visa_public", intel.WorkspacePublicationConfirmedRequest(confirmation="LIVE_VERIFIED")
    )
    assert confirmed["status"] == "published"
    assert (await intel.workspace_marketing_publication_status("visa_public"))[
        "status"
    ] == "published"


@pytest.mark.asyncio
async def test_internal_publish_rechecks_enrollment_before_any_publisher(staged):
    service, item = staged
    _enroll(service, item)
    current = copy.deepcopy(service.load_staging_item("visa", "visa_public"))
    current["enrichment"] = {"the_facts": "Unreviewed later content"}
    service.save_staging_item("visa", "visa_public", current)
    with pytest.raises(HTTPException, match="409"):
        await intel_scraper.publish_staging_item_internal(
            "visa", "visa_public", actor="workspace-agent:damar", require_public_editorial=True
        )


@pytest.mark.asyncio
async def test_ingestion_uses_reviewed_snapshot_when_source_changes_after_check(
    staged, monkeypatch
):
    service, item = staged
    _enroll(service, item)

    def mutate_source(_data, _item_id):
        changed = service.load_staging_item("visa", "visa_public")
        changed["content"] = "Unreviewed concurrent source replacement"
        service.save_staging_item("visa", "visa_public", changed)

    embed = AsyncMock(return_value=[0.1])
    # Stop this test before the later GitHub publishing steps, with no network IO.
    upsert = AsyncMock(side_effect=RuntimeError("test stops after embedding"))
    monkeypatch.setattr(intel_scraper, "enforce_staging_guards", mutate_source)
    monkeypatch.setattr(
        intel_scraper, "get_embedder", lambda: SimpleNamespace(generate_single_embedding=embed)
    )
    monkeypatch.setattr(
        intel_scraper, "QdrantClient", lambda **_: SimpleNamespace(upsert_documents=upsert)
    )
    with pytest.raises(HTTPException):
        await intel_scraper.publish_staging_item_internal(
            "visa", "visa_public", actor="workspace-agent:damar", require_public_editorial=True
        )
    assert embed.await_args.args == (item["title"] + "\n\n" + item["content"],)
    assert upsert.await_args.kwargs["chunks"] == [item["title"] + "\n\n" + item["content"]]
