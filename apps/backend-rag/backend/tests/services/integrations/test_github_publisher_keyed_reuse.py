"""A keyed publication answers success only with a publication it has verified.

With a key the publisher derives one branch name and reuses what it finds there. It trusted
too much of what it found: a branch that "already exists" was reused without reading its
commit, an existing pull request was accepted on its body alone, and the answer could carry a
commit that is not the one published. Two calls with the same key and different articles could
end with a pull request that describes one article on a branch that holds the other, and the
loser was told success.

The remote here is a stand-in that replaces ONLY the HTTP client: refs, commits with their
messages, pull requests with state, body and recorded head. Every publisher method runs for
real. A test holds a caller at a named point, so the order of two calls is forced, not hoped
for; a held caller keeps the answer that was computed when it asked. Every wait is bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.services.integrations.github_publisher import GitHubPublisher, GitHubPublisherError

OWNER = "zz-owner"
REPO = "zz-repo"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
KEY = "news_zz_story"
OTHER_KEY = "news_zz_other_story"
WAIT = 5.0

AFTER_FIRST_LOOKUP = "after the first lookup"
BRANCH_ANSWERED = "branch creation answered"
BEFORE_PULL_REQUEST = "before pull request creation"

BRANCH_MISMATCH = "Existing publication branch fingerprint does not match"
BODY_MISMATCH = "Existing publication pull request fingerprint does not match"
HEAD_MISMATCH = "Existing publication pull request head does not match its fingerprint"
CLOSED_UNMERGED = "Publication pull request was closed without merge; manual review required"
DISAPPEARED = "Publication branch disappeared before it could be verified"
_MARKER = re.compile(r"Publication-Fingerprint: [0-9a-f]{64}")


class _Answer:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or repr(payload)

    def json(self) -> Any:
        return self._payload


class _Gate:
    """A point where one caller stops until the test lets it go."""

    def __init__(self) -> None:
        self._reached = asyncio.Event()
        self._opened = asyncio.Event()

    async def hold(self) -> None:
        self._reached.set()
        await asyncio.wait_for(self._opened.wait(), WAIT)

    async def arrived(self) -> None:
        await asyncio.wait_for(self._reached.wait(), WAIT)

    def release(self) -> None:
        self._opened.set()


class _Caller:
    """The HTTP client of one publisher: every request goes to the shared remote under a name."""

    is_closed = False

    def __init__(self, remote: _Remote, name: str) -> None:
        self._remote = remote
        self._name = name

    async def get(self, url: str, **kwargs: Any) -> _Answer:
        return await self._remote.get(self._name, url, kwargs.get("params") or {})

    async def post(self, url: str, **kwargs: Any) -> _Answer:
        return await self._remote.post(self._name, url, kwargs.get("json") or {})


class _Remote:
    """What GitHub holds: refs, commits and pull requests. Each commit gets its own sha."""

    def __init__(self) -> None:
        self._ids = 0
        base = self._new_sha()
        self.refs: dict[str, str] = {"main": base}
        self.commits: dict[str, dict[str, Any]] = {
            base: {"message": "base", "tree": self._new_sha(), "by": "nobody"}
        }
        self.pulls: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.failing: set[str] = set()
        self.unreadable: set[str] = set()
        self._gates: dict[tuple[str, str], _Gate] = {}

    def _new_sha(self) -> str:
        # Distinct from the first character on: the unkeyed branch is named after 12 of them.
        self._ids += 1
        return f"{self._ids:012x}".ljust(40, "0")

    # ── what a test does to the remote ───────────────────────────────────────

    def hold(self, caller: str, point: str) -> _Gate:
        gate = self._gates[(caller, point)] = _Gate()
        return gate

    async def _pass(self, caller: str, point: str) -> None:
        gate = self._gates.pop((caller, point), None)
        if gate is not None:
            await gate.hold()

    def commits_by(self, caller: str) -> list[str]:
        return [sha for sha, commit in self.commits.items() if commit["by"] == caller]

    def commit_by(self, caller: str) -> str:
        (sha,) = self.commits_by(caller)
        return sha

    def publication_branches(self) -> list[str]:
        return [branch for branch in self.refs if branch != "main"]

    def lookups_by(self, caller: str) -> int:
        return self.requests.count((caller, "pull request lookup"))

    # ── what the publisher asks ──────────────────────────────────────────────

    async def get(self, caller: str, url: str, params: dict[str, Any]) -> _Answer:
        if url == f"{API}/pulls":
            self.requests.append((caller, "pull request lookup"))
            if "pull request lookup" in self.failing:
                return _Answer(500, text="lookup unavailable")
            head = str(params["head"]).split(":", 1)[1]
            found = [
                pull
                for pull in reversed(self.pulls)
                if pull["head"] == head and pull["base"] == params["base"]
            ]
            return _Answer(200, [self._pull_payload(pull) for pull in found])
        if url.startswith(f"{API}/git/ref/heads/"):
            branch = url.removeprefix(f"{API}/git/ref/heads/")
            if "branch" in self.failing:
                answer = _Answer(500, text="ref unavailable")
            elif branch in self.refs:
                answer = _Answer(200, {"object": {"sha": self.refs[branch]}})
            else:
                answer = _Answer(404, text="Not Found")
            await self._pass(caller, AFTER_FIRST_LOOKUP)
            return answer
        if url.startswith(f"{API}/git/refs/heads/"):
            branch = url.removeprefix(f"{API}/git/refs/heads/")
            return _Answer(200, {"object": {"sha": self.refs[branch]}})
        if url.startswith(f"{API}/git/commits/"):
            sha = url.removeprefix(f"{API}/git/commits/")
            if sha in self.unreadable:
                return _Answer(500, text="commit unavailable")
            if sha not in self.commits:
                return _Answer(404, text="Not Found")
            commit = self.commits[sha]
            return _Answer(
                200, {"sha": sha, "message": commit["message"], "tree": {"sha": commit["tree"]}}
            )
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, caller: str, url: str, body: dict[str, Any]) -> _Answer:
        if url in (f"{API}/git/blobs", f"{API}/git/trees"):
            return _Answer(201, {"sha": self._new_sha()})
        if url == f"{API}/git/commits":
            sha = self._new_sha()
            self.commits[sha] = {"message": body["message"], "tree": body["tree"], "by": caller}
            return _Answer(201, {"sha": sha})
        if url == f"{API}/git/refs":
            branch = str(body["ref"]).removeprefix("refs/heads/")
            if branch in self.refs:
                answer = _Answer(422, text="Reference already exists")
            else:
                self.refs[branch] = body["sha"]
                answer = _Answer(201, {"ref": body["ref"]})
            await self._pass(caller, BRANCH_ANSWERED)
            return answer
        if url == f"{API}/pulls":
            await self._pass(caller, BEFORE_PULL_REQUEST)
            if "pull request creation" in self.failing:
                return _Answer(500, text="pull requests unavailable")
            if body["head"] not in self.refs:
                return _Answer(422, text="Validation Failed: head is invalid")
            if any(
                pull["head"] == body["head"]
                and pull["base"] == body["base"]
                and pull["state"] == "open"
                for pull in self.pulls
            ):
                return _Answer(
                    422, text=f"A pull request already exists for {OWNER}:{body['head']}."
                )
            pull = {
                "number": len(self.pulls) + 1,
                "head": body["head"],
                "base": body["base"],
                "title": body["title"],
                "body": body["body"],
                "state": "open",
                "merged_at": None,
                "head_sha": self.refs[body["head"]],
                "by": caller,
            }
            self.pulls.append(pull)
            return _Answer(201, self._pull_payload(pull))
        raise AssertionError(f"unexpected POST {url}")

    @staticmethod
    def _pull_payload(pull: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": pull["number"],
            "node_id": f"PR_{pull['number']}",
            "html_url": f"https://github.test/{OWNER}/{REPO}/pull/{pull['number']}",
            "state": pull["state"],
            "merged_at": pull["merged_at"],
            "head": {"sha": pull["head_sha"]},
            "body": pull["body"],
        }


def _publisher(remote: _Remote, caller: str) -> GitHubPublisher:
    publisher = GitHubPublisher(token="zz-test", owner=OWNER, repo=REPO)
    publisher._client = _Caller(remote, caller)
    return publisher


def _bundle(story: str) -> list[dict[str, Any]]:
    return [{"path": "content/story.mdx", "content": f"story {story}"}]


def _marker(story: str) -> str:
    return GitHubPublisher._fingerprint_marker(
        GitHubPublisher._publication_fingerprint(_bundle(story), {})
    )


async def _publish(
    remote: _Remote, caller: str, story: str = "one", *, key: str | None = KEY
) -> dict[str, Any]:
    return await _publisher(remote, caller).create_commit_with_files(
        _bundle(story),
        f"feat(article): story {story}",
        pull_request=True,
        idempotency_key=key,
    )


def _start(remote: _Remote, caller: str, story: str = "one") -> asyncio.Task[dict[str, Any]]:
    return asyncio.create_task(_publish(remote, caller, story))


async def _finish(task: asyncio.Task[dict[str, Any]]) -> dict[str, Any]:
    return await asyncio.wait_for(task, WAIT)


async def _abandon(task: asyncio.Task[dict[str, Any]]) -> None:
    """The process behind a call dies where it stands."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _orphan_branch(remote: _Remote, story: str = "one") -> str:
    """A keyed branch whose call died before its pull request; returns the commit it holds."""
    gate = remote.hold("died", BEFORE_PULL_REQUEST)
    task = _start(remote, "died", story)
    await gate.arrived()
    await _abandon(task)
    assert remote.pulls == []
    return remote.commit_by("died")


def _assert_published(remote: _Remote, *answers: dict[str, Any]) -> None:
    """A success names a pull request whose body and head carry one marker, and that head."""
    for answer in answers:
        assert answer["success"] is True
        (pull,) = [p for p in remote.pulls if p["number"] == answer["pull_request_number"]]
        in_body = _MARKER.findall(pull["body"])
        assert len(in_body) == 1
        assert in_body[0] in remote.commits[pull["head_sha"]]["message"]
        assert answer["commit_sha"] == pull["head_sha"]


# ── a branch that already exists is read before it is reused ─────────────────


async def test_a_different_article_that_meets_the_branch_is_refused_and_opens_nothing() -> None:
    # A and B, one key, two articles, both past the first lookup. A creates the branch; B is
    # told "already exists" and runs to its end BEFORE A opens its pull request.
    remote = _Remote()
    a_looked, b_looked = remote.hold("A", AFTER_FIRST_LOOKUP), remote.hold("B", AFTER_FIRST_LOOKUP)
    a_before_pull = remote.hold("A", BEFORE_PULL_REQUEST)
    task_a, task_b = _start(remote, "A", "one"), _start(remote, "B", "two")
    await a_looked.arrived()
    await b_looked.arrived()
    a_looked.release()
    await a_before_pull.arrived()
    b_looked.release()

    with pytest.raises(GitHubPublisherError, match=BRANCH_MISMATCH):
        await _finish(task_b)
    assert remote.pulls == []

    a_before_pull.release()
    answer = await _finish(task_a)

    assert len(remote.pulls) == 1
    assert _marker("one") in remote.pulls[0]["body"]
    _assert_published(remote, answer)
    (branch,) = remote.publication_branches()
    assert answer["commit_sha"] == remote.commit_by("A") == remote.refs[branch]
    assert "idempotent" not in answer  # A created both the branch and the pull request


async def test_a_different_article_that_meets_the_pull_request_is_refused() -> None:
    # The reverse order: A has opened its pull request before B goes on.
    remote = _Remote()
    b_looked = remote.hold("B", AFTER_FIRST_LOOKUP)
    task_b = _start(remote, "B", "two")
    await b_looked.arrived()
    answer = await _publish(remote, "A", "one")
    b_looked.release()

    with pytest.raises(GitHubPublisherError, match=BODY_MISMATCH):
        await _finish(task_b)

    assert len(remote.pulls) == 1
    _assert_published(remote, answer)


async def test_the_same_article_twice_answers_one_pull_request_and_the_commit_it_holds() -> None:
    # One key, ONE article, two commits. B is told "already exists" and finishes before A opens
    # its pull request: B opens the pull request of A's commit, and A then finds it there.
    remote = _Remote()
    a_looked, b_looked = remote.hold("A", AFTER_FIRST_LOOKUP), remote.hold("B", AFTER_FIRST_LOOKUP)
    a_before_pull = remote.hold("A", BEFORE_PULL_REQUEST)
    task_a, task_b = _start(remote, "A"), _start(remote, "B")
    await a_looked.arrived()
    await b_looked.arrived()
    a_looked.release()
    await a_before_pull.arrived()
    b_looked.release()

    answer_b = await _finish(task_b)
    a_before_pull.release()
    answer_a = await _finish(task_a)

    assert len(remote.pulls) == 1
    assert remote.commit_by("A") != remote.commit_by("B")  # B built a commit and left it
    assert answer_a["commit_sha"] == answer_b["commit_sha"] == remote.commit_by("A")
    assert answer_a["pull_request_number"] == answer_b["pull_request_number"] == 1
    assert answer_a["idempotent"] is True and answer_b["idempotent"] is True
    _assert_published(remote, answer_a, answer_b)
    (branch,) = remote.publication_branches()
    assert remote.refs[branch] == remote.commit_by("A")
    assert remote.lookups_by("A") == 2  # the first one, and one more after its creation failed


async def test_the_same_article_after_the_pull_request_exists_answers_that_one() -> None:
    remote = _Remote()
    b_looked = remote.hold("B", AFTER_FIRST_LOOKUP)
    task_b = _start(remote, "B")
    await b_looked.arrived()
    answer_a = await _publish(remote, "A")
    b_looked.release()

    answer_b = await _finish(task_b)

    assert len(remote.pulls) == 1
    assert answer_b["commit_sha"] == answer_a["commit_sha"] == remote.commit_by("A")
    _assert_published(remote, answer_a, answer_b)


async def test_a_branch_that_is_gone_when_it_is_read_is_refused() -> None:
    # B is told "already exists"; the call that created the branch dies and the branch is
    # deleted before B reads it.
    remote = _Remote()
    b_looked, b_answered = remote.hold("B", AFTER_FIRST_LOOKUP), remote.hold("B", BRANCH_ANSWERED)
    task_b = _start(remote, "B")
    await b_looked.arrived()
    await _orphan_branch(remote)
    b_looked.release()
    await b_answered.arrived()
    (branch,) = remote.publication_branches()
    del remote.refs[branch]
    b_answered.release()

    with pytest.raises(GitHubPublisherError, match=DISAPPEARED):
        await _finish(task_b)

    assert remote.pulls == []
    assert remote.publication_branches() == []


async def test_a_read_that_fails_after_already_exists_is_an_error_not_a_reuse() -> None:
    remote = _Remote()
    b_looked = remote.hold("B", AFTER_FIRST_LOOKUP)
    task_b = _start(remote, "B")
    await b_looked.arrived()
    await _orphan_branch(remote)
    remote.failing.add("pull request lookup")
    b_looked.release()

    with pytest.raises(GitHubPublisherError, match="Failed to look up publication pull request"):
        await _finish(task_b)

    assert remote.pulls == []


async def test_a_branch_creation_that_does_not_say_created_is_verified() -> None:
    # Only an exact True means "this call created the branch". A wrapper that creates it and
    # forgets to say so sends the call through the same reading as any other branch.
    remote = _Remote()
    publisher = _publisher(remote, "A")
    really_create = publisher._create_branch_ref

    async def forgets_its_answer(branch: str, from_sha: str) -> None:
        await really_create(branch, from_sha)

    publisher._create_branch_ref = forgets_its_answer

    answer = await publisher.create_commit_with_files(
        _bundle("one"), "feat(article): story one", pull_request=True, idempotency_key=KEY
    )

    assert remote.lookups_by("A") == 2  # the first lookup, and the reading of the branch
    assert answer["commit_sha"] == remote.commit_by("A")
    _assert_published(remote, answer)


# ── a branch without a pull request: two calls recover it together ───────────


async def test_two_calls_recovering_one_branch_answer_one_pull_request() -> None:
    # Both find no pull request and both read the head before either opens one.
    remote = _Remote()
    held = await _orphan_branch(remote)
    first_before, second_before = (
        remote.hold("first", BEFORE_PULL_REQUEST),
        remote.hold("second", BEFORE_PULL_REQUEST),
    )
    task_first, task_second = _start(remote, "first"), _start(remote, "second")
    await first_before.arrived()
    await second_before.arrived()
    first_before.release()
    answer_first = await _finish(task_first)
    second_before.release()

    answer_second = await _finish(task_second)

    assert len(remote.pulls) == 1
    assert answer_first["commit_sha"] == answer_second["commit_sha"] == held
    assert answer_first["pull_request_number"] == answer_second["pull_request_number"]
    assert remote.commits_by("first") == remote.commits_by("second") == []
    assert remote.lookups_by("second") == 2
    _assert_published(remote, answer_first, answer_second)


async def test_a_pull_request_nobody_could_open_leaves_its_own_error_after_one_more_look() -> None:
    remote = _Remote()
    await _orphan_branch(remote)
    remote.failing.add("pull request creation")

    with pytest.raises(GitHubPublisherError, match="Failed to open pull request"):
        await _publish(remote, "again")

    assert remote.pulls == []
    assert remote.lookups_by("again") == 2  # once more, never a loop


# ── an existing pull request is read by its recorded head ────────────────────


async def _published_and_another(remote: _Remote) -> tuple[dict[str, Any], str]:
    """A publication under KEY, and the commit of ANOTHER article published under another key."""
    answer = await _publish(remote, "A", "one")
    await _publish(remote, "X", "two", key=OTHER_KEY)
    return answer, remote.commit_by("X")


def _merge_and_delete_branch(remote: _Remote, pull: dict[str, Any]) -> None:
    pull.update(state="closed", merged_at="2026-11-23T00:00:00Z")
    del remote.refs[pull["head"]]


@pytest.mark.parametrize(
    "corruption",
    ["another article's commit", "merged, branch deleted", "no head"],
)
async def test_a_pull_request_whose_head_is_not_this_article_is_refused(corruption: str) -> None:
    remote = _Remote()
    _, other_commit = await _published_and_another(remote)
    pull = remote.pulls[0]
    pull["head_sha"] = None if corruption == "no head" else other_commit
    if corruption == "merged, branch deleted":
        _merge_and_delete_branch(remote, pull)

    with pytest.raises(GitHubPublisherError, match=HEAD_MISMATCH):
        await _publish(remote, "again", "one")

    assert len(remote.pulls) == 2  # the two that were there
    assert remote.commits_by("again") == []
    assert _marker("one") in pull["body"]  # the refusal leaves the disagreement as it found it


async def test_a_merged_pull_request_whose_branch_is_gone_answers_its_recorded_head() -> None:
    remote = _Remote()
    answer = await _publish(remote, "A", "one")
    _merge_and_delete_branch(remote, remote.pulls[0])

    again = await _publish(remote, "again", "one")

    assert again["commit_sha"] == answer["commit_sha"] == remote.commit_by("A")
    assert again["pull_request_number"] == 1
    assert remote.commits_by("again") == [] and remote.publication_branches() == []


async def test_a_pull_request_closed_without_a_merge_is_refused_as_before() -> None:
    remote = _Remote()
    await _publish(remote, "A", "one")
    remote.pulls[0]["state"] = "closed"

    with pytest.raises(GitHubPublisherError, match=CLOSED_UNMERGED):
        await _publish(remote, "again", "one")

    assert len(remote.pulls) == 1


# ── a read that fails is an error, never "absent" ────────────────────────────


@pytest.mark.parametrize(
    ("what", "said"),
    [
        ("pull request lookup", "Failed to look up publication pull request"),
        ("branch", "Failed to inspect publication branch"),
    ],
)
async def test_a_failed_lookup_or_branch_read_publishes_nothing(what: str, said: str) -> None:
    remote = _Remote()
    remote.failing.add(what)

    with pytest.raises(GitHubPublisherError, match=said):
        await _publish(remote, "A")

    assert remote.pulls == [] and remote.publication_branches() == []
    assert remote.commits_by("A") == []


async def test_an_unreadable_commit_on_a_branch_without_a_pull_request_opens_nothing() -> None:
    remote = _Remote()
    remote.unreadable.add(await _orphan_branch(remote))

    with pytest.raises(GitHubPublisherError, match="Failed to inspect publication commit"):
        await _publish(remote, "again")

    assert remote.pulls == []


async def test_an_unreadable_head_of_an_existing_pull_request_is_not_a_success() -> None:
    remote = _Remote()
    answer = await _publish(remote, "A")
    remote.unreadable.add(answer["commit_sha"])

    with pytest.raises(GitHubPublisherError, match="Failed to inspect publication commit"):
        await _publish(remote, "again")

    assert len(remote.pulls) == 1


# ── what does not change ─────────────────────────────────────────────────────


async def test_a_second_call_with_the_same_article_builds_nothing_and_answers_the_first() -> None:
    remote = _Remote()
    first = await _publish(remote, "A")

    again = await _publish(remote, "again")

    assert again["commit_sha"] == first["commit_sha"]
    assert again["pull_request_number"] == first["pull_request_number"] == 1
    assert again["idempotent"] is True and "idempotent" not in first
    assert remote.commits_by("again") == [] and len(remote.pulls) == 1
    _assert_published(remote, first, again)


async def test_a_different_article_under_a_published_key_is_refused_as_before() -> None:
    remote = _Remote()
    await _publish(remote, "A", "one")

    with pytest.raises(GitHubPublisherError, match=BODY_MISMATCH):
        await _publish(remote, "again", "two")

    assert len(remote.pulls) == 1 and remote.commits_by("again") == []


async def test_without_a_key_every_call_is_its_own_publication() -> None:
    remote = _Remote()

    first = await _publish(remote, "A", key=None)
    second = await _publish(remote, "B", key=None)

    assert len(remote.pulls) == 2
    assert first["commit_sha"] == remote.commit_by("A") != second["commit_sha"]
    assert first["branch"] == f"auto-publish/{first['commit_sha'][:12]}"
    assert "idempotent" not in first and "idempotent" not in second
    assert remote.lookups_by("A") == remote.lookups_by("B") == 0
    assert [pull["head_sha"] for pull in remote.pulls] == [
        first["commit_sha"],
        second["commit_sha"],
    ]


async def test_without_a_key_a_branch_that_already_exists_is_reused_unread() -> None:
    # The unkeyed branch is named after the commit itself: there is nothing to verify.
    remote = _Remote()
    publisher = _publisher(remote, "A")

    async def already_there(branch: str, from_sha: str) -> bool:
        remote.refs[branch] = from_sha
        return False

    publisher._create_branch_ref = AsyncMock(side_effect=already_there)

    answer = await publisher.create_commit_with_files(
        _bundle("one"), "feat(article): story one", pull_request=True
    )

    assert answer["commit_sha"] == remote.commit_by("A") == remote.pulls[0]["head_sha"]
    assert remote.lookups_by("A") == 0
