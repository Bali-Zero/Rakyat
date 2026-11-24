"""Tests for IGGraphSensor — own account metrics pull via Graph API v20."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.measurer.ig_graph_sensor import (
    IGGraphError,
    IGGraphSensor,
    IGPostMetrics,
)


@pytest.mark.asyncio
async def test_read_returns_followers_and_post_count():
    sensor = IGGraphSensor(token="tok", ig_user_id="123", http_client=None)
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {
        "followers_count": 5123,
        "media_count": 245,
        "biography": "Bali Zero",
    }
    with patch.object(sensor, "_get", AsyncMock(return_value=mock_resp.json())):
        result = await sensor.read_account_summary()
    assert result["followers_count"] == 5123
    assert result["media_count"] == 245


@pytest.mark.asyncio
async def test_read_posts_returns_all_healthy_posts_with_insights():
    sensor = IGGraphSensor(token="tok", ig_user_id="123", http_client=None)
    media_page = {
        "data": [
            {
                "id": "m1",
                "caption": "Hook line one\nBody",
                "media_type": "CAROUSEL_ALBUM",
                "timestamp": "2026-04-20T03:00:00+0000",
                "permalink": "https://instagram.com/p/ABC",
            },
            {"id": "m2", "media_type": "IMAGE"},
            {"id": "m3", "media_type": "IMAGE"},
        ]
    }
    insights = {
        "data": [
            {"name": "likes", "values": [{"value": 120}]},
            {"name": "comments", "values": [{"value": 8}]},
            {"name": "saved", "values": [{"value": 34}]},
            {"name": "reach", "values": [{"value": 2100}]},
        ]
    }

    async def fake_get(path: str, **kw):
        if "/media" in path and "/insights" not in path:
            return media_page
        if "/insights" in path:
            return insights
        raise AssertionError(f"unexpected path {path}")

    with patch.object(sensor, "_get", side_effect=fake_get):
        posts = await sensor.read_posts(limit=3)

    assert len(posts) == 3
    p = posts[0]
    assert isinstance(p, IGPostMetrics)
    assert p.post_id == "m1"
    assert p.format == "CAROUSEL_ALBUM"
    assert p.likes == 120
    assert p.saves == 34
    assert p.reach == 2100
    assert "Hook line one" in p.caption
    assert [post.post_id for post in posts] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_read_posts_skips_per_post_insights_error_and_logs_media_id_and_code(caplog):
    sensor = IGGraphSensor(token="tok", ig_user_id="123", http_client=None)
    media_page = {
        "data": [
            {"id": "m1", "media_type": "IMAGE"},
            {"id": "m2", "media_type": "IMAGE"},
            {"id": "m3", "media_type": "IMAGE"},
        ]
    }
    insights = {
        "data": [
            {"name": "likes", "values": [{"value": 12}]},
            {"name": "comments", "values": [{"value": 3}]},
            {"name": "saved", "values": [{"value": 4}]},
            {"name": "reach", "values": [{"value": 56}]},
        ]
    }

    async def fake_get(path: str, **kw):
        if "/media" in path and "/insights" not in path:
            return media_page
        if "/insights" in path:
            if path == "/m2/insights":
                return {"error": {"message": "unsupported metric", "code": 100}}
            return insights
        raise AssertionError(f"unexpected path {path}")

    with patch.object(sensor, "_get", side_effect=fake_get):
        with caplog.at_level(logging.WARNING):
            posts = await sensor.read_posts(limit=3)

    assert [post.post_id for post in posts] == ["m1", "m3"]
    assert [(post.likes, post.comments, post.saves, post.reach) for post in posts] == [
        (12, 3, 4, 56),
        (12, 3, 4, 56),
    ]
    assert len(caplog.records) == 1
    assert "m2" in caplog.messages[0]
    assert "100" in caplog.messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        {"message": "synthetic token expired", "code": 190, "type": "OAuthException"},
        {"message": "synthetic rate limit", "code": 4},
    ],
)
async def test_read_posts_raises_account_insights_error(error):
    sensor = IGGraphSensor(token="tok", ig_user_id="123", http_client=None)
    media_page = {"data": [{"id": "m1", "media_type": "IMAGE"}]}

    async def fake_get(path: str, **kw):
        if "/media" in path and "/insights" not in path:
            return media_page
        if "/insights" in path:
            return {"error": error}
        raise AssertionError(f"unexpected path {path}")

    with patch.object(sensor, "_get", side_effect=fake_get):
        with pytest.raises(IGGraphError, match=error["message"]) as caught:
            await sensor.read_posts(limit=1)

    assert caught.value.code == error["code"]
    assert caught.value.error_type == error.get("type")


@pytest.mark.asyncio
async def test_read_posts_keeps_real_zero_insights():
    sensor = IGGraphSensor(token="tok", ig_user_id="123", http_client=None)
    media_page = {"data": [{"id": "m-zero", "media_type": "IMAGE"}]}
    insights = {
        "data": [
            {"name": "likes", "values": [{"value": 0}]},
            {"name": "comments", "values": [{"value": 0}]},
            {"name": "saved", "values": [{"value": 0}]},
            {"name": "reach", "values": [{"value": 0}]},
        ]
    }

    async def fake_get(path: str, **kw):
        if "/media" in path and "/insights" not in path:
            return media_page
        if "/insights" in path:
            return insights
        raise AssertionError(f"unexpected path {path}")

    with patch.object(sensor, "_get", side_effect=fake_get):
        posts = await sensor.read_posts(limit=1)

    assert len(posts) == 1
    assert posts[0].likes == posts[0].comments == posts[0].saves == posts[0].reach == 0


@pytest.mark.asyncio
async def test_raises_on_graph_api_error():
    sensor = IGGraphSensor(token="tok", ig_user_id="123", http_client=None)

    async def fake_get(path, **kw):
        return {"error": {"message": "rate limit", "code": 4}}

    with patch.object(sensor, "_get", side_effect=fake_get):
        with pytest.raises(IGGraphError, match="rate limit"):
            await sensor.read_account_summary()
