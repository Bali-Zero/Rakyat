"""
Regression & security tests for RT-01 (/api/blog/*, newsletter.py) and
RT-02 (/api/news*, news.py).

Verifies end-to-end through HybridAuthMiddleware + FastAPI routers:
  (a) Legitimate public reads/opt-ins remain accessible anonymously:
      - GET  /api/news (approved items only)
      - GET  /api/news/categories
      - GET  /api/news/{slug}
      - GET  /api/news/feed/rss (well-formed XML with CDATA `]]>` + XML tag escaping)
      - POST /api/blog/ask
      - POST /api/blog/newsletter/subscribe
      - GET  /api/blog/newsletter/confirm?token=...
      - POST /api/news/subscribe
  (b) Admin newsletter endpoints (/api/blog/newsletter/subscribers and /log)
      reject anonymous (401) and non-admin (403) callers, and allow admins (200).
  (c) /api/blog/newsletter/unsubscribe, /api/blog/newsletter/preferences, and
      /api/news/unsubscribe reject unauthenticated bare-email/id requests (401)
      and forged tokens (403), while succeeding with valid HMAC subscriber tokens.
  (d) POST /api/news, POST /api/news/bulk, POST /api/news/{id}/image, and
      PATCH /api/news/{id}/status reject anonymous (401) and non-admin (403)
      callers, allow admins (200), and GET /api/news?status=pending|rejected
      forces status='approved' for anonymous and non-admin callers.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test_jwt_secret_key_for_testing_only_min_32_chars_long")
os.environ.setdefault("API_KEYS", "test_api_key_1,test_api_key_2")
os.environ.setdefault("ENVIRONMENT", "test")

from backend.app.auth.public_endpoints import find_entry, is_public_path
from backend.app.core.config import settings
from backend.app.dependencies import get_database_pool
from backend.app.routers import blog_ask, news, newsletter
from backend.app.routers.news import generate_news_unsubscribe_token
from backend.app.routers.newsletter import generate_newsletter_token
from backend.middleware.hybrid_auth import HybridAuthMiddleware

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


def _mint_jwt(*, email: str, role: str) -> str:
    payload = {
        "sub": email,
        "email": email,
        "role": role,
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _make_news_row(
    *,
    id: str = "news-uuid-1",
    title: str = "Approved Policy Update",
    slug: str = "approved-policy-update",
    summary: str = "Summary body",
    category: str = "business",
    status: str = "approved",
) -> dict:
    return {
        "id": id,
        "title": title,
        "slug": slug,
        "summary": summary,
        "content": "Article content",
        "source": "editorial",
        "source_url": "https://example.org/source",
        "category": category,
        "priority": "medium",
        "status": status,
        "image_url": None,
        "view_count": 3,
        "published_at": NOW,
        "created_at": NOW,
        "ai_summary": None,
        "ai_tags": None,
    }


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 1")

    class _AcquireCtx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    pool.acquire = MagicMock(side_effect=lambda: _AcquireCtx())
    pool._conn = conn
    return pool


@pytest.fixture
def client(mock_pool):
    """Full FastAPI app with HybridAuthMiddleware + blog_ask + newsletter + news."""
    app = FastAPI()
    app.add_middleware(HybridAuthMiddleware)
    app.include_router(blog_ask.router)
    app.include_router(newsletter.router)
    app.include_router(news.router)
    app.dependency_overrides[get_database_pool] = lambda: mock_pool

    with (
        patch(
            "backend.middleware.hybrid_auth.is_session_revoked",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "backend.app.deps.auth.is_session_revoked_sync",
            return_value=False,
        ),
        patch(
            "backend.app.routers.newsletter.invalidate_cache",
            new=AsyncMock(),
        ),
        patch(
            "backend.app.routers.news.invalidate_cache",
            new=AsyncMock(),
        ),
        patch(
            "backend.app.routers.newsletter.send_confirmation_email",
            new=AsyncMock(),
        ),
    ):
        yield TestClient(app)


# ============================================================================
# (a) Legitimate Public Endpoints Remain Accessible Without Login
# ============================================================================


class TestPublicBlogAndNewsReadsRemainOpen:
    def test_public_registry_granularity_no_blanket_blog_or_news_prefix(self):
        assert find_entry("/api/blog/posts/anything") is None
        assert not is_public_path("/api/blog/newsletter/subscribers")
        assert not is_public_path("/api/blog/newsletter/log")
        assert not is_public_path("/api/blog/newsletter/preferences")
        assert not is_public_path("/api/news/bulk")
        assert not is_public_path("/api/news/news-uuid-1/image")
        assert not is_public_path("/api/news/news-uuid-1/status")

        assert is_public_path("/api/blog/ask", method="POST")
        assert is_public_path("/api/blog/newsletter/subscribe", method="POST")
        assert is_public_path("/api/blog/newsletter/confirm", method="GET")
        assert is_public_path("/api/blog/newsletter/confirm", method="POST")
        assert is_public_path("/api/blog/newsletter/unsubscribe", method="POST")
        assert is_public_path("/api/news", method="GET")
        assert not is_public_path("/api/news", method="POST")
        assert is_public_path("/api/news/categories", method="GET")
        assert is_public_path("/api/news/feed/rss", method="GET")
        assert is_public_path("/api/news/sample-slug", method="GET")

    def test_anonymous_get_news_categories_and_slug_succeed(self, client, mock_pool):
        conn = mock_pool._conn
        conn.fetchval = AsyncMock(return_value=1)
        conn.fetch = AsyncMock(return_value=[_make_news_row()])
        resp = client.get("/api/news")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["slug"] == "approved-policy-update"

        conn.fetch = AsyncMock(return_value=[{"category": "business", "count": 4}])
        cat_resp = client.get("/api/news/categories")
        assert cat_resp.status_code == 200
        assert cat_resp.json()["categories"] == [{"name": "business", "count": 4}]

        conn.fetchrow = AsyncMock(return_value=_make_news_row())
        slug_resp = client.get("/api/news/approved-policy-update")
        assert slug_resp.status_code == 200
        assert slug_resp.json()["data"]["slug"] == "approved-policy-update"

    def test_anonymous_blog_ask_and_newsletter_subscribe_confirm_succeed(self, client, mock_pool):
        fake_orch = MagicMock()
        fake_result = MagicMock()
        fake_result.answer = "Public answer"
        fake_result.sources = [{"title": "Guide", "content": "Snippet"}]
        fake_result.evidence_score = 0.85
        fake_orch.process_query = AsyncMock(return_value=fake_result)

        with patch(
            "backend.app.routers.blog_ask.get_blog_orchestrator",
            new=AsyncMock(return_value=fake_orch),
        ):
            ask_resp = client.post(
                "/api/blog/ask",
                json={"question": "What are the visa rules?", "article_category": "immigration"},
            )
        assert ask_resp.status_code == 200
        assert ask_resp.json()["answer"] == "Public answer"

        conn = mock_pool._conn
        conn.fetchrow = AsyncMock(side_effect=[None, {"id": "sub-1"}])
        sub_resp = client.post(
            "/api/blog/newsletter/subscribe",
            json={"email": "reader@example.com", "frequency": "weekly"},
        )
        assert sub_resp.status_code == 200
        assert sub_resp.json()["subscriberId"] == "sub-1"

        conn.fetchrow = AsyncMock(
            return_value={"id": "sub-1", "email": "reader@example.com", "confirmed": False}
        )
        conf_resp = client.get("/api/blog/newsletter/confirm?token=valid-confirm-tok")
        assert conf_resp.status_code == 200
        assert conf_resp.json()["success"] is True

    def test_rss_feed_escapes_cdata_breakout_and_xml_tags(self, client, mock_pool):
        conn = mock_pool._conn
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "title": "Breaking ]]><injected>evil</injected><script>x</script>",
                    "slug": "slug-with-</link><bad>&chars",
                    "summary": "Summary ]]><pwned/> & more",
                    "source": "editorial",
                    "category": "tax</category><evil>1</evil>&\"'",
                    "published_at": NOW,
                }
            ]
        )

        resp = client.get("/api/news/feed/rss")
        assert resp.status_code == 200
        assert "application/rss+xml" in resp.headers["content-type"]

        # Must parse as strictly well-formed XML with zero injected sibling tags.
        root = ET.fromstring(resp.text)
        channel = root.find("channel")
        assert channel is not None
        item = channel.find("item")
        assert item is not None

        assert item.find("injected") is None
        assert item.find("evil") is None
        assert item.find("pwned") is None
        assert item.find("bad") is None

        assert item.findtext("title") == "Breaking ]]><injected>evil</injected><script>x</script>"
        assert item.findtext("description") == "Summary ]]><pwned/> & more"
        assert item.findtext("category") == "tax</category><evil>1</evil>&\"'"
        assert "]]]]><![CDATA[>" in resp.text
        assert "&lt;/category&gt;" in resp.text


# ============================================================================
# (b) RT-01: Newsletter Admin Endpoints Block Anonymous & Non-Admin Callers
# ============================================================================


class TestRT01NewsletterAdminGates:
    def test_subscribers_blocks_anonymous_and_non_admin_allows_admin(self, client, mock_pool):
        # 1. Anonymous -> 401
        anon = client.get("/api/blog/newsletter/subscribers")
        assert anon.status_code == 401

        # 2. Non-admin team member -> 403
        staff_token = _mint_jwt(email="staff@example.com", role="member")
        non_admin = client.get(
            "/api/blog/newsletter/subscribers",
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert non_admin.status_code == 403

        # 3. Admin -> 200
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=0)
        admin_token = _mint_jwt(email="admin@example.com", role="admin")
        ok = client.get(
            "/api/blog/newsletter/subscribers",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert ok.status_code == 200
        assert ok.json()["total"] == 0

    def test_log_blocks_anonymous_and_non_admin_allows_admin(self, client, mock_pool):
        params = "article_id=art-1&recipient_count=10&sent_count=10&failed_count=0"

        anon = client.post(f"/api/blog/newsletter/log?{params}")
        assert anon.status_code == 401

        staff_token = _mint_jwt(email="staff@example.com", role="member")
        non_admin = client.post(
            f"/api/blog/newsletter/log?{params}",
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert non_admin.status_code == 403

        admin_token = _mint_jwt(email="admin@example.com", role="admin")
        ok = client.post(
            f"/api/blog/newsletter/log?{params}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert ok.status_code == 200
        assert ok.json()["success"] is True


# ============================================================================
# (c) RT-01 & RT-02: Unsubscribe & Preferences Enforce Token / Auth Ownership
# ============================================================================


class TestRT01RT02TokenizedOptOutAndPreferences:
    def test_newsletter_unsubscribe_rejects_missing_or_forged_token_accepts_valid(
        self, client, mock_pool
    ):
        conn = mock_pool._conn
        conn.execute.reset_mock()

        # Missing token -> 401 without DB mutation
        no_tok = client.post(
            "/api/blog/newsletter/unsubscribe",
            json={"email": "subscriber@example.com"},
        )
        assert no_tok.status_code == 401
        conn.execute.assert_not_awaited()

        # Forged token -> 403 without DB mutation
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "sub-42",
                "email": "subscriber@example.com",
                "confirmation_token": None,
            }
        )
        bad_tok = client.post(
            "/api/blog/newsletter/unsubscribe",
            json={"email": "subscriber@example.com", "token": "forged-token-value"},
        )
        assert bad_tok.status_code == 403
        conn.execute.assert_not_awaited()

        # Valid HMAC token -> 200
        valid_tok = generate_newsletter_token(email="subscriber@example.com")
        ok = client.post(
            "/api/blog/newsletter/unsubscribe",
            json={"email": "subscriber@example.com", "token": valid_tok},
        )
        assert ok.status_code == 200
        assert ok.json()["success"] is True
        conn.execute.assert_awaited_once()

    def test_newsletter_preferences_requires_auth_and_valid_token_or_owner(
        self, client, mock_pool
    ):
        conn = mock_pool._conn
        conn.execute.reset_mock()

        # Anonymous blocked at middleware (401) because /preferences left PUBLIC_ENDPOINTS
        anon = client.patch(
            "/api/blog/newsletter/preferences",
            json={"email": "subscriber@example.com", "frequency": "daily"},
        )
        assert anon.status_code == 401

        # Authenticated non-owner without valid token -> 403
        other_jwt = _mint_jwt(email="other@example.com", role="member")
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "sub-42",
                "email": "subscriber@example.com",
                "confirmation_token": None,
            }
        )
        spoof = client.patch(
            "/api/blog/newsletter/preferences",
            json={"email": "subscriber@example.com", "frequency": "daily"},
            headers={"Authorization": f"Bearer {other_jwt}"},
        )
        assert spoof.status_code == 403
        conn.execute.assert_not_awaited()

        # Valid HMAC subscriber token -> 200
        valid_tok = generate_newsletter_token(subscriber_id="sub-42")
        ok = client.patch(
            "/api/blog/newsletter/preferences",
            json={
                "subscriberId": "sub-42",
                "token": valid_tok,
                "frequency": "daily",
            },
            headers={"Authorization": f"Bearer {other_jwt}"},
        )
        assert ok.status_code == 200
        assert ok.json()["success"] is True

    def test_news_unsubscribe_requires_valid_hmac_token_or_auth(self, client, mock_pool):
        conn = mock_pool._conn
        conn.execute.reset_mock()

        no_tok = client.post("/api/news/unsubscribe?email=subscriber@example.com")
        assert no_tok.status_code == 401
        conn.execute.assert_not_awaited()

        bad_tok = client.post(
            "/api/news/unsubscribe?email=subscriber@example.com&token=forged"
        )
        assert bad_tok.status_code == 403
        conn.execute.assert_not_awaited()

        valid_tok = generate_news_unsubscribe_token("subscriber@example.com")
        ok = client.post(
            f"/api/news/unsubscribe?email=subscriber@example.com&token={valid_tok}"
        )
        assert ok.status_code == 200
        assert ok.json()["success"] is True
        conn.execute.assert_awaited_once()


# ============================================================================
# (d) RT-02: News Mutations & Draft Status Lockdown
# ============================================================================


class TestRT02NewsMutationsAndDraftLockdown:
    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("POST", "/api/news", {"title": "Unauthorized Article Injection", "source": "ext"}),
            (
                "POST",
                "/api/news/bulk",
                [{"title": "Unauthorized Bulk Injection", "source": "ext"}],
            ),
            ("POST", "/api/news/news-uuid-1/image", {"image_url": "https://example.org/x.jpg"}),
            ("PATCH", "/api/news/news-uuid-1/status?status=approved", None),
        ],
    )
    def test_news_mutations_block_anonymous_and_non_admin(
        self, client, method: str, path: str, payload
    ):
        # 1. Anonymous -> 401 enforced directly at HybridAuthMiddleware (NOT bypassed as public)
        anon = (
            client.request(method, path, json=payload)
            if payload is not None
            else client.request(method, path)
        )
        assert anon.status_code == 401
        assert anon.headers.get("X-Auth-Type") != "public"

        # 2. Non-admin -> 403
        staff_token = _mint_jwt(email="staff@example.com", role="member")
        headers = {"Authorization": f"Bearer {staff_token}"}
        non_admin = (
            client.request(method, path, json=payload, headers=headers)
            if payload is not None
            else client.request(method, path, headers=headers)
        )
        assert non_admin.status_code == 403

    def test_news_mutations_allow_admin(self, client, mock_pool):
        conn = mock_pool._conn
        conn.fetchval = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value={"id": "news-uuid-1", "slug": "admin-post"})
        conn.execute = AsyncMock(return_value="UPDATE 1")

        admin_token = _mint_jwt(email="admin@example.com", role="admin")
        headers = {"Authorization": f"Bearer {admin_token}"}

        r1 = client.post(
            "/api/news",
            json={"title": "Authorized Admin News Post", "source": "editorial"},
            headers=headers,
        )
        assert r1.status_code == 200

        r2 = client.post(
            "/api/news/bulk",
            json=[{"title": "Authorized Bulk News Post", "source": "editorial"}],
            headers=headers,
        )
        assert r2.status_code == 200

        r3 = client.post(
            "/api/news/news-uuid-1/image",
            json={"image_url": "https://example.org/cover.jpg"},
            headers=headers,
        )
        assert r3.status_code == 200

        r4 = client.patch(
            "/api/news/news-uuid-1/status?status=approved",
            headers=headers,
        )
        assert r4.status_code == 200

    def test_get_news_status_pending_forced_to_approved_for_anonymous_and_non_admin(
        self, client, mock_pool
    ):
        conn = mock_pool._conn
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])

        # Anonymous asking for ?status=pending -> SQL parameter forced to 'approved'
        resp_anon = client.get("/api/news?status=pending")
        assert resp_anon.status_code == 200
        assert conn.fetchval.await_args.args[1] == "approved"

        # Non-admin asking for ?status=rejected -> SQL parameter forced to 'approved'
        staff_token = _mint_jwt(email="staff@example.com", role="member")
        resp_staff = client.get(
            "/api/news?status=rejected",
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert resp_staff.status_code == 200
        assert conn.fetchval.await_args.args[1] == "approved"

        # Admin asking for ?status=pending -> SQL parameter honors 'pending'
        admin_token = _mint_jwt(email="admin@example.com", role="admin")
        resp_admin = client.get(
            "/api/news?status=pending",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp_admin.status_code == 200
        assert conn.fetchval.await_args.args[1] == "pending"
