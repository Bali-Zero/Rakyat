"""Editorial destination is independent of topic; real staging stays deduplicated."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.routers.intel_scraper as router_module
from backend.app.utils.internal_api_auth import api_key_auth
from backend.services.intel.intel_staging_service import IntelStagingService

HEADERS = {"X-API-Key": "test-editorial-intake-key"}


def payload(**updates: object) -> dict[str, object]:
    return {
        "title": "Public immigration report",
        "content": "Public reporting about visa, KITAS and stay permit compliance.",
        "category": "immigration",
        "source_url": "https://example.test/public-report",
        "source_name": "Synthetic public source",
        "published_at": "2026-11-19",
        "relevance_score": 50,
        **updates,
    }


@pytest.fixture
def intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, IntelStagingService]]:
    service = IntelStagingService.__new__(IntelStagingService)
    service.base_staging_dir = tmp_path
    service.news_staging_dir = tmp_path / "news"
    service.visa_staging_dir = tmp_path / "visa"
    service.news_staging_dir.mkdir()
    service.visa_staging_dir.mkdir()
    monkeypatch.setattr(router_module, "staging_service", service)
    monkeypatch.setattr(router_module, "invalidate_cache", AsyncMock())
    monkeypatch.setattr(
        api_key_auth,
        "validate_api_key",
        lambda key: {"role": "internal"} if key == HEADERS["X-API-Key"] else None,
    )
    app = FastAPI()
    app.include_router(router_module.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def snapshot(service: IntelStagingService) -> dict[str, bytes]:
    return {
        str(path.relative_to(service.base_staging_dir)): path.read_bytes()
        for path in service.base_staging_dir.glob("*/*.json")
    }


@pytest.mark.parametrize("category", ["immigration", "business"])
def test_explicit_editorial_destination_preserves_topic_and_copy(intake, category: str) -> None:
    client, service = intake
    submitted = payload(category=category, destination="editorial_news")
    response = client.post("/api/intel/scraper/submit", json=submitted, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["intel_type"] == "news"
    assert result["duplicate"] is False
    stored = service.load_staging_item("news", result["item_id"])
    assert stored["status"] == "pending"
    for field in ("title", "content", "category", "source_url", "source_name", "published_at"):
        assert stored[field] == submitted[field]
    assert len(snapshot(service)) == 1
    assert not list(service.visa_staging_dir.glob("*.json"))


@pytest.mark.parametrize(
    ("category", "content", "expected_type"),
    [("immigration", "Public report", "visa"), ("business", "Public report", "news")],
)
def test_omitted_destination_keeps_automatic_routing(
    intake, category: str, content: str, expected_type: str
) -> None:
    client, service = intake
    response = client.post(
        "/api/intel/scraper/submit",
        json=payload(category=category, content=content, title="Public report"),
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["intel_type"] == expected_type
    assert service.load_staging_item(expected_type, result["item_id"])["category"] == category


def test_repeated_editorial_source_reuses_existing_item_without_rewriting(intake) -> None:
    client, service = intake
    submitted = payload(destination="editorial_news")
    first = client.post("/api/intel/scraper/submit", json=submitted, headers=HEADERS)
    before = snapshot(service)
    again = client.post(
        "/api/intel/scraper/submit", json={**submitted, "content": "Different copy"}, headers=HEADERS
    )
    assert first.status_code == again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["item_id"] == first.json()["item_id"]
    assert snapshot(service) == before


@pytest.mark.parametrize("editorial_first", [False, True])
def test_existing_source_in_other_destination_refuses_without_moving(
    intake, editorial_first: bool
) -> None:
    client, service = intake
    editorial = payload(destination="editorial_news")
    automatic = payload()
    first = client.post(
        "/api/intel/scraper/submit",
        json=editorial if editorial_first else automatic,
        headers=HEADERS,
    )
    assert first.status_code == 200
    before = snapshot(service)
    conflict = client.post(
        "/api/intel/scraper/submit",
        json=automatic if editorial_first else editorial,
        headers=HEADERS,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "source_destination_conflict",
        "message": "Source URL already exists in another staging destination",
        "item_id": first.json()["item_id"],
        "intel_type": first.json()["intel_type"],
    }
    assert snapshot(service) == before


@pytest.mark.parametrize("destination", ["visa", "news", "../news", ""])
def test_invalid_destination_never_writes_staging(intake, destination: str) -> None:
    client, service = intake
    response = client.post(
        "/api/intel/scraper/submit", json=payload(destination=destination), headers=HEADERS
    )
    assert response.status_code == 422
    assert snapshot(service) == {}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "invalid-test-key"}])
def test_editorial_destination_does_not_bypass_authentication(intake, headers: dict) -> None:
    client, service = intake
    response = client.post(
        "/api/intel/scraper/submit", json=payload(destination="editorial_news"), headers=headers
    )
    assert response.status_code == 401
    assert snapshot(service) == {}
