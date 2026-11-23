#!/usr/bin/env python3
"""Announce the articles a production deployment made live to IndexNow, and only those.

Run by .github/workflows/indexnow-after-deploy.yml when Vercel reports a Production deployment of
balizero.com as successful. Publishing an article only opens its pull request; the page exists
once that PR is merged and deployed, which took from minutes to a day. So the announcement waits
for the deployment, and for each new article to answer 200 on the live site.

Steps:
1. the previous successful Production deployment (GitHub deployments API);
2. the English articles added between the two commits
   (apps/mouth/src/content/articles/<folder>/<slug>.mdx; translations <slug>.<lang>.mdx are not
   announced: the sitemap does not list them);
3. their URLs, read from the live sitemap (the site decides its routes: a folder is not always the
   path, e.g. lifestyle -> /living/);
4. each URL must answer 200;
5. one POST to IndexNow with the site key (public by design: served at balizero.com/<key>.txt).

Standard library only: the workflow runs it with the runner's python3, nothing installed.
Exit 0 when everything new was announced or nothing was new; 1 when IndexNow refused or a new
article never appeared on the live site.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

SITE = "https://balizero.com"
SITEMAP = f"{SITE}/sitemap.xml"
INDEXNOW = "https://api.indexnow.org/indexnow"
ARTICLES = "apps/mouth/src/content/articles/"
PUBLIC = Path("apps/mouth/public")
USER_AGENT = "balizero-indexnow-after-deploy"

_ARTICLE = re.compile(r"^apps/mouth/src/content/articles/[^/]+/(?P<slug>[^/.]+)\.mdx$")
_KEY = re.compile(r"^[0-9a-f]{32}$")
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


MAX_PAGES = 20  # of 100 deployments each


class GitHubError(Exception):
    """The GitHub API did not answer as expected."""


def _say(line: str) -> None:
    """The run's report, read in the workflow log."""
    sys.stdout.write(line + "\n")


class Http:
    """The two calls the script makes; tests replace it."""

    def get(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def post_json(self, url: str, body: dict) -> int:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


def previous_production_sha(
    deployments: list[dict], latest_state: Callable[[int], str | None], current_id: int
) -> str | None:
    """Commit of the newest Production deployment before this one whose latest status is success.

    Deployment ids grow with time: one created after this one is never its predecessor, even if
    it finished first.
    """
    for d in deployments:
        if d.get("id", 0) >= current_id or d.get("environment") != "Production":
            continue
        if latest_state(d["id"]) == "success":
            return d.get("sha")
    return None


def new_article_slugs(added_paths: list[str]) -> list[str]:
    """Slugs of the English articles among the added paths, in order, without repeats."""
    out: list[str] = []
    for p in added_paths:
        m = _ARTICLE.match(p.strip())
        if m and m["slug"] not in out:
            out.append(m["slug"])
    return out


def urls_from_sitemap(slugs: list[str], sitemap_xml: str) -> dict[str, str]:
    """The live URL of each slug: the sitemap entry on balizero.com whose last path segment is it."""
    by_last: dict[str, str] = {}
    for loc in _LOC.findall(sitemap_xml):
        u = urlparse(loc)
        if u.scheme == "https" and u.hostname == "balizero.com":
            by_last.setdefault(u.path.rstrip("/").rsplit("/", 1)[-1], loc)
    return {s: by_last[s] for s in slugs if s in by_last}


def site_key(public_dir: Path) -> str:
    """The IndexNow key: the public .txt whose name is 32 hex characters and whose content is it."""
    for f in sorted(public_dir.glob("*.txt")):
        if _KEY.match(f.stem) and f.read_text().strip() == f.stem:
            return f.stem
    raise SystemExit(f"no IndexNow key file in {public_dir}")


def indexnow_body(key: str, urls: list[str]) -> dict:
    return {
        "host": "balizero.com",
        "key": key,
        "keyLocation": f"{SITE}/{key}.txt",
        "urlList": urls,
    }


def git_added(prev: str, sha: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", prev, sha, "--", ARTICLES],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def run(
    *,
    sha: str,
    deployment_id: int,
    repo: str,
    token: str,
    http: Http,
    added: Callable[[str, str], list[str]] = git_added,
    sleep: Callable[[float], None] = time.sleep,
    public_dir: Path = PUBLIC,
    tries: int = 20,
    wait_s: float = 30.0,
) -> int:
    api = f"https://api.github.com/repos/{repo}"
    gh = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def latest_state(dep_id: int) -> str | None:
        st, body = http.get(f"{api}/deployments/{dep_id}/statuses?per_page=1", gh)
        if st != 200:
            raise GitHubError(f"statuses of deployment {dep_id}: HTTP {st}")
        rows = json.loads(body) if body else []
        return rows[0].get("state") if rows else None

    # Walk every page until the predecessor is found: a failed or unknown answer is an error
    # (red), never «nothing to compare», so no article is silently skipped (gate r1 on #710).
    prev = None
    try:
        for page in range(1, MAX_PAGES + 1):
            st, text = http.get(
                f"{api}/deployments?environment=Production&per_page=100&page={page}", gh
            )
            if st != 200:
                raise GitHubError(f"deployments page {page}: HTTP {st}")
            rows = json.loads(text) if text else []
            prev = previous_production_sha(rows, latest_state, deployment_id)
            if prev or len(rows) < 100:
                break
        else:
            raise GitHubError(f"no successful predecessor within {MAX_PAGES} pages")
    except GitHubError as e:
        _say(f"cannot find the previous Production deployment: {e}")
        return 1
    if not prev:
        _say("no earlier successful Production deployment: nothing to compare")
        return 0
    slugs = new_article_slugs(added(prev, sha))
    _say(f"{prev[:8]}..{sha[:8]}: {len(slugs)} new article(s) {slugs}")
    if not slugs:
        return 0

    live: dict[str, str] = {}
    for attempt in range(tries):
        st, xml = http.get(SITEMAP)
        if st == 200:
            for slug, url in urls_from_sitemap(slugs, xml).items():
                if slug not in live and http.get(url)[0] == 200:
                    live[slug] = url
        if len(live) == len(slugs):
            break
        if attempt < tries - 1:
            sleep(wait_s)

    missing = [s for s in slugs if s not in live]
    if live:
        urls = [live[s] for s in slugs if s in live]
        code = http.post_json(INDEXNOW, indexnow_body(site_key(public_dir), urls))
        _say(f"IndexNow {code} for {urls}")
        if code not in (200, 202):
            return 1
    if missing:
        _say(f"never live on {SITE} (not announced): {missing}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Announce newly live articles to IndexNow.")
    ap.add_argument("--sha", required=True)
    ap.add_argument("--deployment-id", type=int, required=True)
    a = ap.parse_args(argv)
    return run(
        sha=a.sha,
        deployment_id=a.deployment_id,
        repo=os.environ["GITHUB_REPOSITORY"],
        token=os.environ["GITHUB_TOKEN"],
        http=Http(),
    )


if __name__ == "__main__":
    sys.exit(main())
