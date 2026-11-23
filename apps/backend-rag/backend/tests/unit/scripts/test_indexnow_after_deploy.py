"""Unit tests for backend/scripts/indexnow_after_deploy.py. No network: HTTP and git are faked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.scripts.indexnow_after_deploy import (
    INDEXNOW,
    SITEMAP,
    indexnow_body,
    new_article_slugs,
    previous_production_sha,
    run,
    site_key,
    urls_from_sitemap,
)

KEY = "a1" * 16  # 32 hex characters, built so no key-shaped literal sits in the file
REPO = "Example/Repo"
API = f"https://api.github.com/repos/{REPO}"
NEW = "apps/mouth/src/content/articles/lifestyle/new-test-article.mdx"


def sitemap(*locs: str) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in locs)
    return f'<?xml version="1.0"?><urlset>{body}</urlset>'


class FakeHttp:
    """Answers by URL; records every GET and POST."""

    def __init__(self, routes: dict, post_code=202):
        self.routes = routes
        self.post_code = post_code
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, headers=None):
        self.gets.append(url)
        r = self.routes.get(url, (404, ""))
        if isinstance(r, list):
            return r.pop(0) if len(r) > 1 else r[0]
        return r

    def post_json(self, url, body):
        self.posts.append((url, body))
        return self.post_code


def deployments(*rows):
    return (200, json.dumps([{"id": i, "sha": s, "environment": e} for i, s, e in rows]))


def statuses(state):
    return (200, json.dumps([{"state": state}]))


@pytest.fixture
def public(tmp_path: Path) -> Path:
    (tmp_path / f"{KEY}.txt").write_text(KEY + "\n")
    (tmp_path / "llms.txt").write_text("not a key")
    (tmp_path / "ffffffffffffffffffffffffffffffff.txt").write_text("content is not the name")
    return tmp_path


def base_routes(**extra):
    routes = {
        f"{API}/deployments?environment=Production&per_page=50": deployments(
            (30, "c" * 40, "Production"),
            (29, "b" * 40, "Production"),
            (28, "a" * 40, "Production"),
        ),
        f"{API}/deployments/29/statuses?per_page=1": statuses("failure"),
        f"{API}/deployments/28/statuses?per_page=1": statuses("success"),
    }
    routes.update(extra)
    return routes


def go(http, public, added=None, **kw):
    seen = {}

    def fake_added(prev, sha):
        seen["range"] = (prev, sha)
        return added if added is not None else [NEW]

    code = run(
        sha="c" * 40,
        deployment_id=30,
        repo=REPO,
        token="t",
        http=http,
        added=fake_added,
        sleep=lambda s: None,
        public_dir=public,
        tries=3,
        **kw,
    )
    return code, seen


# --- the pieces ---------------------------------------------------------------------------


def test_previous_sha_is_the_newest_earlier_production_deployment_that_succeeded():
    rows = [
        {"id": 5, "sha": "e", "environment": "Production"},
        {"id": 4, "sha": "d", "environment": "Preview"},
        {"id": 3, "sha": "c", "environment": "Production"},
        {"id": 2, "sha": "b", "environment": "Production"},
    ]
    states = {3: "error", 2: "success", 4: "success"}
    assert previous_production_sha(rows, states.get, current_id=5) == "b"
    assert previous_production_sha(rows, {}.get, current_id=5) is None
    # a newer deployment that already succeeded is never the predecessor
    newer = [{"id": 6, "sha": "f", "environment": "Production"}, *rows]
    assert previous_production_sha(newer, {6: "success", 2: "success"}.get, current_id=5) == "b"


def test_only_english_articles_count_as_new():
    paths = [
        "apps/mouth/src/content/articles/business/a-new-rule.mdx",
        "apps/mouth/src/content/articles/business/a-new-rule.id.mdx",
        "apps/mouth/src/content/articles/business/a-new-rule.it.mdx",
        "apps/mouth/src/content/articles/business/a-new-rule.mdx",
        "apps/mouth/src/content/articles/README.md",
        "apps/mouth/src/content/articles/business/deep/nested.mdx",
        "apps/mouth/public/static/news/a-new-rule.jpg",
        "apps/mouth/src/content/articles/living/second-one.mdx",
    ]
    assert new_article_slugs(paths) == ["a-new-rule", "second-one"]


def test_urls_come_from_the_live_sitemap_by_last_path_segment():
    xml = sitemap(
        "https://balizero.com",
        "https://balizero.com/living/new-test-article",
        "https://evil.example/living/other-article",
        "http://balizero.com/business/other-article",
    )
    assert urls_from_sitemap(["new-test-article", "other-article", "absent"], xml) == {
        "new-test-article": "https://balizero.com/living/new-test-article"
    }


def test_the_key_is_the_public_file_named_by_its_own_content(public):
    assert site_key(public) == KEY
    assert indexnow_body(KEY, ["https://balizero.com/x"]) == {
        "host": "balizero.com",
        "key": KEY,
        "keyLocation": f"https://balizero.com/{KEY}.txt",
        "urlList": ["https://balizero.com/x"],
    }


# --- the run ------------------------------------------------------------------------------


def test_a_new_live_article_is_announced_once(public):
    url = "https://balizero.com/living/new-test-article"
    http = FakeHttp(base_routes(**{SITEMAP: (200, sitemap(url)), url: (200, "<html>")}))
    code, seen = go(http, public)
    assert code == 0
    assert seen["range"] == ("a" * 40, "c" * 40)  # skips the failed deployment 29
    assert http.posts == [(INDEXNOW, indexnow_body(KEY, [url]))]


def test_nothing_new_means_no_ping(public):
    http = FakeHttp(base_routes())
    code, _ = go(http, public, added=["apps/mouth/src/content/articles/x/y.id.mdx"])
    assert code == 0
    assert http.posts == []
    assert SITEMAP not in http.gets


def test_no_earlier_success_means_no_ping(public):
    routes = base_routes()
    routes[f"{API}/deployments/28/statuses?per_page=1"] = statuses("error")
    http = FakeHttp(routes)
    assert go(http, public)[0] == 0
    assert http.posts == []


def test_waits_for_the_sitemap_and_a_200_before_announcing(public):
    url = "https://balizero.com/living/new-test-article"
    http = FakeHttp(
        base_routes(
            **{
                SITEMAP: [(200, sitemap()), (200, sitemap(url)), (200, sitemap(url))],
                url: [(404, ""), (200, "<html>")],
            }
        )
    )
    code, _ = go(http, public)
    assert code == 0
    assert http.posts == [(INDEXNOW, indexnow_body(KEY, [url]))]
    assert http.gets.count(SITEMAP) == 3


def test_an_article_that_never_answers_200_is_not_announced_and_the_run_is_red(public):
    url = "https://balizero.com/living/new-test-article"
    http = FakeHttp(base_routes(**{SITEMAP: (200, sitemap(url)), url: (404, "")}))
    code, _ = go(http, public)
    assert code == 1
    assert http.posts == []


def test_the_live_ones_are_announced_even_when_another_is_missing(public):
    url = "https://balizero.com/living/new-test-article"
    later = "apps/mouth/src/content/articles/business/not-yet-live.mdx"
    http = FakeHttp(base_routes(**{SITEMAP: (200, sitemap(url)), url: (200, "<html>")}))
    code, _ = go(http, public, added=[NEW, later])
    assert code == 1
    assert http.posts == [(INDEXNOW, indexnow_body(KEY, [url]))]


def test_an_indexnow_refusal_turns_the_run_red(public):
    url = "https://balizero.com/living/new-test-article"
    http = FakeHttp(
        base_routes(**{SITEMAP: (200, sitemap(url)), url: (200, "<html>")}), post_code=403
    )
    assert go(http, public)[0] == 1


def test_a_deployments_api_failure_turns_the_run_red(public):
    http = FakeHttp({})
    assert go(http, public)[0] == 1
    assert http.posts == []
