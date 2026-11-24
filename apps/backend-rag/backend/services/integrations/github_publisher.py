"""
GitHub Publisher Service

Handles publishing articles to the GitHub repository via the GitHub API.
Creates MDX files and commits them to trigger Vercel auto-deploy.

Usage:
    from backend.services.integrations.github_publisher import github_publisher

    # Check if configured
    if github_publisher.is_configured:
        result = await github_publisher.upload_file(
            path="path/to/file.mdx",
            content="# Content",
            message="Add article",
        )

    # Or atomic multi-file commit
    result = await github_publisher.create_commit_with_files(
        files=[
            {"path": "file1.mdx", "content": "content1"},
            {"path": "file2.jpg", "content": image_bytes},
        ],
        message="Add article with image",
    )
"""

import base64
import hashlib
import json
import logging
import re
import time
from typing import Any

import httpx

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


class GitHubPublisherError(Exception):
    """Custom exception for GitHub Publisher errors"""

    pass


class GitHubPublisher:
    """
    Service for publishing files to GitHub repository.

    Uses the GitHub Contents API to create/update files and commits.
    """

    BASE_URL = "https://api.github.com"

    def __init__(
        self,
        token: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
    ) -> None:
        """
        Initialize GitHub Publisher.

        Args:
            token: GitHub Personal Access Token (defaults to settings)
            owner: Repository owner (defaults to settings)
            repo: Repository name (defaults to settings)
        """
        self.token = token or settings.github_token
        self.owner = owner or settings.github_owner
        self.repo = repo or settings.github_repo
        self._client: httpx.AsyncClient | None = None

        if not self.token:
            logger.warning("GitHub token not configured - publishing will fail")

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared async client."""
        if self._client is None or self._client.is_closed:
            # A renamed or transferred repository answers 301 on its old
            # path; without following it every publish fails as a bare
            # "GitHub publish failed" (2026-08-28, 2026-09-04).
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def close(self) -> None:
        """Close the internal async client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        logger.info("GitHubPublisher HTTP client closed.")

    @property
    def is_configured(self) -> bool:
        """Check if GitHub API is configured."""
        return bool(self.token and self.owner and self.repo)

    def _get_headers(self) -> dict[str, str]:
        """Get headers for GitHub API requests."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _validate_path(path: str) -> str:
        """
        Validate and normalize a repository-relative file path.

        Prevents path traversal attacks (e.g. ``../../etc/passwd``) by
        rejecting paths that contain ``..`` segments or an absolute leading
        slash after normalization.

        Args:
            path: Repository-relative file path supplied by the caller.

        Returns:
            The original path, stripped of any leading slash.

        Raises:
            ValueError: If the path contains traversal sequences or invalid
                characters that could be exploited in the GitHub API URL.
        """
        # Strip accidental leading slash
        normalized = path.lstrip("/")

        # Reject path traversal sequences
        if re.search(r"(^|/)\.\.(\/|$)", normalized):
            raise ValueError(f"Invalid repository path (traversal detected): {path!r}")

        # Reject null bytes or other control characters
        if any(ord(c) < 0x20 for c in normalized):
            raise ValueError(f"Invalid repository path (control characters): {path!r}")

        if not normalized:
            raise ValueError("Repository path must not be empty.")

        return normalized

    async def check_file_exists(self, path: str, branch: str = "main") -> bool:
        """
        Check if a file exists in the repository.

        Args:
            path: File path relative to repository root
            branch: Branch name (default: main)

        Returns:
            True if file exists, False otherwise
        """
        if not self.is_configured:
            raise GitHubPublisherError("GitHub API not configured")

        path = self._validate_path(path)
        url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/contents/{path}"
        params = {"ref": branch}

        logger.debug("Checking file exists: %s (branch: %s)", path, branch)

        client = self._get_client()
        response = await client.get(
            url,
            headers=self._get_headers(),
            params=params,
            timeout=30.0,
        )

        exists = response.status_code == 200
        logger.debug("File exists check: %s → %s", path, exists)
        return exists

    async def get_file_content(self, path: str, branch: str = "main") -> str:
        """Read one UTF-8 repository file from a named branch."""

        if not self.is_configured:
            raise GitHubPublisherError("GitHub API not configured")

        path = self._validate_path(path)
        url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/contents/{path}"
        response = await self._get_client().get(
            url,
            headers=self._get_headers(),
            params={"ref": branch},
            timeout=30.0,
        )
        if response.status_code != 200:
            raise GitHubPublisherError(
                f"Failed to read repository file {path}: {response.text}"
            )
        payload = response.json()
        encoded = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(encoded, str):
            raise GitHubPublisherError(f"Repository file {path} has no content")
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubPublisherError(
                f"Repository file {path} is not valid UTF-8 base64"
            ) from exc

    async def get_file_sha(self, path: str, branch: str = "main") -> str | None:
        """
        Get the SHA of an existing file (needed for updates).

        Args:
            path: File path relative to repository root
            branch: Branch name (default: main)

        Returns:
            File SHA if exists, None otherwise
        """
        if not self.is_configured:
            raise GitHubPublisherError("GitHub API not configured")

        path = self._validate_path(path)
        url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/contents/{path}"
        params = {"ref": branch}

        logger.debug("Getting file SHA: %s", path)

        client = self._get_client()
        response = await client.get(
            url,
            headers=self._get_headers(),
            params=params,
            timeout=30.0,
        )

        if response.status_code == 200:
            sha = response.json().get("sha")
            logger.debug(f"File SHA found: {path} → {sha[:7] if sha else 'N/A'}")
            return sha

        logger.debug("File not found (no SHA): %s", path)
        return None

    async def upload_file(
        self,
        path: str,
        content: bytes | str,
        message: str,
        branch: str = "main",
    ) -> dict[str, Any]:
        """
        Upload/create a single file to the repository.

        Args:
            path: File path relative to repository root
            content: File content (bytes or string)
            message: Commit message
            branch: Branch name (default: main)

        Returns:
            GitHub API response with commit info
        """
        if not self.is_configured:
            raise GitHubPublisherError("GitHub API not configured")

        path = self._validate_path(path)
        start_time = time.time()
        url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/contents/{path}"

        # Encode content to base64
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        content_base64 = base64.b64encode(content_bytes).decode("utf-8")
        content_size_kb = len(content_bytes) / 1024

        logger.info(f"Uploading file: {path} ({content_size_kb:.1f} KB)")

        # Check if file exists (need SHA for update)
        existing_sha = await self.get_file_sha(path, branch)
        is_update = existing_sha is not None

        payload = {
            "message": message,
            "content": content_base64,
            "branch": branch,
        }

        if existing_sha:
            payload["sha"] = existing_sha
            logger.debug(f"Updating existing file (SHA: {existing_sha[:7]})")

        client = self._get_client()
        response = await client.put(
            url,
            headers=self._get_headers(),
            json=payload,
            timeout=60.0,
        )

        elapsed_ms = (time.time() - start_time) * 1000

        if response.status_code not in (200, 201):
            error_detail = response.json().get("message", response.text)
            logger.error(
                "GitHub API error uploading %s: %s",
                path,
                error_detail,
                extra={
                    "path": path,
                    "status_code": response.status_code,
                    "elapsed_ms": elapsed_ms,
                },
            )
            raise GitHubPublisherError(f"Failed to upload file: {error_detail}")

        result = response.json()
        commit_sha = result["commit"]["sha"]

        logger.info(
            f"File {'updated' if is_update else 'created'}: {path}",
            extra={
                "path": path,
                "commit_sha": commit_sha[:7],
                "size_kb": content_size_kb,
                "elapsed_ms": elapsed_ms,
                "is_update": is_update,
            },
        )

        return {
            "success": True,
            "path": path,
            "sha": result["content"]["sha"],
            "commit_sha": commit_sha,
            "commit_url": result["commit"]["html_url"],
        }

    async def _create_branch_ref(self, branch: str, from_sha: str) -> bool:
        """
        Create a new branch ref pointing at ``from_sha``.

        Used by the pull-request publish path so the commit lands on a
        fresh, unprotected branch instead of attempting a direct push to a
        protected branch (which GitHub rejects with HTTP 422).

        Args:
            branch: New branch name (without ``refs/heads/`` prefix).
            from_sha: Commit SHA the new branch should point at.

        Returns:
            ``True`` when this call created the ref. ``False`` when a ref of
            that name was already there: it points where its creator left
            it, which need not be ``from_sha``.

        Raises:
            GitHubPublisherError: If the branch ref cannot be created.
        """
        client = self._get_client()
        create_ref_url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/git/refs"
        response = await client.post(
            create_ref_url,
            headers=self._get_headers(),
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
            timeout=30.0,
        )
        # 201 Created on success. 422 means the ref already exists: the caller
        # decides what that is worth.
        if response.status_code == 422 and "already exists" in response.text:
            logger.warning("Branch '%s' already exists", branch)
            return False
        if response.status_code != 201:
            logger.error("Failed to create branch ref '%s': %s", branch, response.text)
            raise GitHubPublisherError(
                f"Failed to create branch ref '{branch}': {response.text}",
            )
        return True

    async def _create_pull_request(
        self,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        """
        Open a pull request from ``head_branch`` into ``base_branch``.

        Args:
            head_branch: Source branch carrying the new commit.
            base_branch: Target (protected) branch, e.g. ``main``.
            title: PR title.
            body: PR description.

        Returns:
            Dict with ``number`` (PR number), ``node_id`` (GraphQL id) and
            ``html_url``.

        Raises:
            GitHubPublisherError: If the PR cannot be created.
        """
        client = self._get_client()
        pulls_url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/pulls"
        response = await client.post(
            pulls_url,
            headers=self._get_headers(),
            json={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
            },
            timeout=30.0,
        )
        if response.status_code != 201:
            logger.error("Failed to open pull request: %s", response.text)
            raise GitHubPublisherError(f"Failed to open pull request: {response.text}")
        data = response.json()
        return {
            "number": data["number"],
            "node_id": data["node_id"],
            "html_url": data["html_url"],
        }

    async def _find_pull_request(
        self,
        head_branch: str,
        base_branch: str,
    ) -> dict[str, Any] | None:
        """Return an existing PR for one deterministic publication branch."""

        response = await self._get_client().get(
            f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/pulls",
            headers=self._get_headers(),
            params={
                "head": f"{self.owner}:{head_branch}",
                "base": base_branch,
                "state": "all",
                "per_page": 10,
            },
            timeout=30.0,
        )
        if response.status_code != 200:
            raise GitHubPublisherError(
                f"Failed to look up publication pull request: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            return None
        data = payload[0]
        if not isinstance(data, dict):
            return None
        return {
            "number": data.get("number"),
            "node_id": data.get("node_id"),
            "html_url": data.get("html_url"),
            "state": data.get("state"),
            "merged_at": data.get("merged_at"),
            "commit_sha": (data.get("head") or {}).get("sha"),
            "body": data.get("body") or "",
        }

    async def _get_branch_commit_sha(self, branch: str) -> str | None:
        """Return the current commit for a branch, or ``None`` when absent."""

        response = await self._get_client().get(
            f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/git/ref/heads/{branch}",
            headers=self._get_headers(),
            timeout=30.0,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GitHubPublisherError(
                f"Failed to inspect publication branch '{branch}': {response.text}"
            )
        payload = response.json()
        return (payload.get("object") or {}).get("sha")

    async def _get_commit_message(self, commit_sha: str) -> str:
        """Read the message used to bind a recovery branch to its bundle."""

        response = await self._get_client().get(
            f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/git/commits/{commit_sha}",
            headers=self._get_headers(),
            timeout=30.0,
        )
        if response.status_code != 200:
            raise GitHubPublisherError(
                f"Failed to inspect publication commit {commit_sha}: {response.text}"
            )
        payload = response.json()
        return str(payload.get("message") or "")

    @staticmethod
    def _publication_fingerprint(
        files: list[dict[str, Any]],
        json_object_updates: dict[str, dict[str, Any]],
    ) -> str:
        """Hash every path, byte, and same-parent JSON mutation in a bundle."""

        digest = hashlib.sha256()
        for file_info in sorted(files, key=lambda item: str(item["path"])):
            content = file_info["content"]
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content
            digest.update(str(file_info["path"]).encode("utf-8"))
            digest.update(b"\0")
            digest.update(content_bytes)
            digest.update(b"\0")
        digest.update(
            json.dumps(
                json_object_updates,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return digest.hexdigest()

    @staticmethod
    def _fingerprint_marker(publication_fingerprint: str) -> str:
        return f"Publication-Fingerprint: {publication_fingerprint}"

    def _pull_request_text(self, message: str, publication_fingerprint: str) -> tuple[str, str]:
        """Title and body of a publication pull request; the body carries the marker."""
        title = message.split("\n", 1)[0]
        body = (
            f"{message}\n\n"
            f"{self._fingerprint_marker(publication_fingerprint)}\n\n"
            "Auto-opened by Article Composer (News Room publish). "
            "Merged by pro.rakyat_editorial_merger once its required checks pass."
        )
        return title, body

    async def _existing_publication(
        self,
        *,
        head_branch: str,
        base_branch: str,
        publication_fingerprint: str,
    ) -> dict[str, Any] | None:
        """What GitHub holds for one keyed publication, verified. Opens and creates nothing.

        ``None`` when there is neither a pull request nor a branch. Otherwise
        ``{"commit_sha", "pull_request"}``: a commit whose message carries this
        bundle's marker, and the pull request for it (``None`` for a branch
        nobody has opened yet). Something that exists without the marker is
        refused, and so is anything that cannot be read: only a 404 on the
        branch ref means "absent".
        """
        marker = self._fingerprint_marker(publication_fingerprint)
        existing_pr = await self._find_pull_request(head_branch, base_branch)
        if existing_pr:
            if marker not in str(existing_pr.get("body") or ""):
                raise GitHubPublisherError(
                    "Existing publication pull request fingerprint does not match"
                )
            if existing_pr.get("state") == "closed" and not existing_pr.get("merged_at"):
                raise GitHubPublisherError(
                    "Publication pull request was closed without merge; manual review required"
                )
            # The body is what its opener wrote; the head is what the pull request
            # holds. It is read by sha, never by branch name, so a merged pull
            # request whose branch was deleted still verifies.
            head_sha = existing_pr.get("commit_sha")
            if not head_sha or marker not in await self._get_commit_message(str(head_sha)):
                raise GitHubPublisherError(
                    "Existing publication pull request head does not match its fingerprint"
                )
            return {"commit_sha": str(head_sha), "pull_request": existing_pr}

        branch_sha = await self._get_branch_commit_sha(head_branch)
        if not branch_sha:
            return None
        if marker not in await self._get_commit_message(branch_sha):
            raise GitHubPublisherError("Existing publication branch fingerprint does not match")
        return {"commit_sha": branch_sha, "pull_request": None}

    async def _open_publication_pull_request(
        self,
        *,
        head_branch: str,
        base_branch: str,
        message: str,
        publication_fingerprint: str,
        commit_sha: str,
    ) -> tuple[dict[str, Any], bool]:
        """Open the pull request of a keyed head; say whether this call opened it.

        Another call with the same bundle can open it first. When the creation
        fails, what exists is read ONCE more: a verified pull request is the
        answer, anything else leaves the creation's own error. Never a loop.
        """
        title, body = self._pull_request_text(message, publication_fingerprint)
        try:
            pull_request = await self._create_pull_request(
                head_branch=head_branch,
                base_branch=base_branch,
                title=title,
                body=body,
            )
        except GitHubPublisherError as creation_error:
            try:
                existing = await self._existing_publication(
                    head_branch=head_branch,
                    base_branch=base_branch,
                    publication_fingerprint=publication_fingerprint,
                )
            except GitHubPublisherError as unverified:
                raise creation_error from unverified
            if existing is None or existing["pull_request"] is None:
                raise
            return existing, False
        return {"commit_sha": commit_sha, "pull_request": pull_request}, True

    @staticmethod
    def _resumed_answer(
        publication: dict[str, Any],
        *,
        head_branch: str,
        files_count: int,
    ) -> dict[str, Any]:
        """The answer for a publication this call found: its verified head, its pull request."""
        pull_request = publication["pull_request"]
        return {
            "success": True,
            "commit_sha": publication["commit_sha"],
            "files_count": files_count,
            "branch": head_branch,
            "pull_request_url": pull_request.get("html_url"),
            "pull_request_number": pull_request.get("number"),
            "auto_merge_enabled": False,
            "idempotent": True,
        }

    async def _answer_with_existing(
        self,
        existing: dict[str, Any],
        *,
        head_branch: str,
        base_branch: str,
        message: str,
        files_count: int,
        publication_fingerprint: str,
    ) -> dict[str, Any]:
        """Answer with a verified publication, opening its pull request if it has none."""
        if existing["pull_request"] is None:
            existing, _ = await self._open_publication_pull_request(
                head_branch=head_branch,
                base_branch=base_branch,
                message=message,
                publication_fingerprint=publication_fingerprint,
                commit_sha=existing["commit_sha"],
            )
        return self._resumed_answer(existing, head_branch=head_branch, files_count=files_count)

    async def _resume_publication_pull_request(
        self,
        *,
        head_branch: str,
        base_branch: str,
        message: str,
        files_count: int,
        publication_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Resume an existing idempotent PR/branch after a process interruption."""

        existing = await self._existing_publication(
            head_branch=head_branch,
            base_branch=base_branch,
            publication_fingerprint=publication_fingerprint,
        )
        if existing is None:
            return None
        return await self._answer_with_existing(
            existing,
            head_branch=head_branch,
            base_branch=base_branch,
            message=message,
            files_count=files_count,
            publication_fingerprint=publication_fingerprint,
        )

    async def create_commit_with_files(
        self,
        files: list[dict[str, Any]],
        message: str,
        branch: str = "main",
        pull_request: bool = False,
        pr_branch_prefix: str = "auto-publish",
        idempotency_key: str | None = None,
        must_not_exist_paths: list[str] | None = None,
        json_object_updates: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Create a single commit with multiple files.

        Uses the Git Data API for atomic commits with multiple files.

        Two publish modes:

        - ``pull_request=False`` (default): commit is pushed directly to
          ``branch`` by updating its ref. Works only when ``branch`` is
          unprotected.
        - ``pull_request=True``: the commit is placed on a fresh branch
          (``{pr_branch_prefix}/<short-sha>``), a PR into ``branch`` is
          opened and left for ``pro.rakyat_editorial_merger``, the only merger
          of these PRs. GitHub auto-merge is never requested: Rakyat ``main``
          has no required check, so it would merge before the checks finish.

        Args:
            files: List of dicts with 'path' and 'content' keys
            message: Commit message
            branch: Target branch (default: main)
            pull_request: If True, publish via branch + PR.
            pr_branch_prefix: Prefix for the temporary PR branch name.
            idempotency_key: Stable publication identity used to resume one PR.
                With it, a success names a publication whose commit carries
                this bundle's fingerprint, and ``commit_sha`` is that commit.
            must_not_exist_paths: Paths that must be absent from the exact base
                commit used for this operation.
            json_object_updates: Object fields to update after reading the JSON
                file from the exact immutable parent commit.

        Returns:
            Commit info. When ``pull_request=True`` also includes
            ``pull_request_url``, ``pull_request_number`` and
            ``auto_merge_enabled``, always False, kept for the routers' responses.
        """
        if not self.is_configured:
            raise GitHubPublisherError("GitHub API not configured")

        # Validate all paths up front before touching the GitHub API.
        for file_info in files:
            file_info["path"] = self._validate_path(file_info["path"])
        absent_paths = [
            self._validate_path(path) for path in (must_not_exist_paths or [])
        ]
        normalized_json_updates = {
            self._validate_path(path): updates
            for path, updates in (json_object_updates or {}).items()
        }
        if any(not isinstance(updates, dict) for updates in normalized_json_updates.values()):
            raise ValueError("JSON object updates must contain object values")
        explicit_paths = {str(file_info["path"]) for file_info in files}
        if explicit_paths.intersection(normalized_json_updates):
            raise ValueError("A repository path cannot be both a file and a JSON update")
        publication_fingerprint = self._publication_fingerprint(
            files, normalized_json_updates
        )

        deterministic_branch: str | None = None
        if pull_request and idempotency_key:
            safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", idempotency_key).strip("-.")
            if not safe_key:
                raise ValueError("Invalid publication idempotency key")
            key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            deterministic_branch = (
                f"{pr_branch_prefix}/{safe_key[:48]}-{key_digest[:24]}"
            )
            resumed = await self._resume_publication_pull_request(
                head_branch=deterministic_branch,
                base_branch=branch,
                message=message,
                files_count=len(files) + len(normalized_json_updates),
                publication_fingerprint=publication_fingerprint,
            )
            if resumed is not None:
                return resumed

        start_time = time.time()
        file_paths = [f["path"] for f in files]
        total_size_kb = (
            sum(
                len(f["content"].encode("utf-8") if isinstance(f["content"], str) else f["content"])
                for f in files
            )
            / 1024
        )

        logger.info(
            f"Creating atomic commit with {len(files)} files ({total_size_kb:.1f} KB total)",
            extra={
                "files": file_paths,
                "branch": branch,
                "total_size_kb": total_size_kb,
            },
        )

        client = self._get_client()
        # Step 1: Get the current commit SHA of the branch
        logger.debug("Step 1/6: Getting branch ref for '%s'", branch)
        ref_url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/git/refs/heads/{branch}"
        ref_response = await client.get(
            ref_url,
            headers=self._get_headers(),
            timeout=30.0,
        )

        if ref_response.status_code != 200:
            logger.error(f"Failed to get branch ref: {ref_response.text}")
            raise GitHubPublisherError(f"Failed to get branch ref: {ref_response.text}")

        current_commit_sha = ref_response.json()["object"]["sha"]
        logger.debug(f"Current commit SHA: {current_commit_sha[:7]}")

        # Step 2: Get the current tree SHA
        logger.debug("Step 2/6: Getting current tree SHA")
        commit_url = (
            f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/git/commits/{current_commit_sha}"
        )
        commit_response = await client.get(
            commit_url,
            headers=self._get_headers(),
            timeout=30.0,
        )

        if commit_response.status_code != 200:
            logger.error(f"Failed to get commit: {commit_response.text}")
            raise GitHubPublisherError(f"Failed to get commit: {commit_response.text}")

        current_tree_sha = commit_response.json()["tree"]["sha"]
        logger.debug(f"Current tree SHA: {current_tree_sha[:7]}")

        # The absence assertion is bound to the SAME immutable commit SHA used
        # as this commit's parent. If main advances later, GitHub reports a
        # merge conflict instead of silently replacing an article at that path.
        for protected_path in absent_paths:
            collision_response = await client.get(
                f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/contents/{protected_path}",
                headers=self._get_headers(),
                params={"ref": current_commit_sha},
                timeout=30.0,
            )
            if collision_response.status_code == 200:
                raise GitHubPublisherError(
                    f"Protected repository path already exists: {protected_path}"
                )
            if collision_response.status_code != 404:
                raise GitHubPublisherError(
                    f"Failed to verify protected repository path {protected_path}: "
                    f"{collision_response.text}"
                )

        # Resolve mutable JSON documents only after the immutable base commit
        # has been selected. Concurrent homepage updates therefore conflict at
        # merge instead of silently restoring an older layout snapshot.
        for update_path, updates in normalized_json_updates.items():
            update_response = await client.get(
                f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/contents/{update_path}",
                headers=self._get_headers(),
                params={"ref": current_commit_sha},
                timeout=30.0,
            )
            if update_response.status_code != 200:
                raise GitHubPublisherError(
                    f"Failed to read same-parent JSON file {update_path}: "
                    f"{update_response.text}"
                )
            encoded = update_response.json().get("content")
            try:
                document = json.loads(base64.b64decode(encoded).decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubPublisherError(
                    f"Same-parent JSON file {update_path} is invalid"
                ) from exc
            if not isinstance(document, dict):
                raise GitHubPublisherError(
                    f"Same-parent JSON file {update_path} is not an object"
                )
            document.update(updates)
            files.append(
                {
                    "path": update_path,
                    "content": json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                }
            )
        # Step 3: Create blobs for each file
        logger.debug(f"Step 3/6: Creating {len(files)} blobs")
        tree_items = []
        for idx, file_info in enumerate(files):
            path = file_info["path"]
            content = file_info["content"]

            # Encode content to base64
            if isinstance(content, str):
                content = content.encode("utf-8")
            content_base64 = base64.b64encode(content).decode("utf-8")

            # Create blob
            blob_url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/git/blobs"
            blob_response = await client.post(
                blob_url,
                headers=self._get_headers(),
                json={"content": content_base64, "encoding": "base64"},
                timeout=30.0,
            )

            if blob_response.status_code != 201:
                logger.error(f"Failed to create blob for {path}: {blob_response.text}")
                raise GitHubPublisherError(
                    f"Failed to create blob for {path}: {blob_response.text}",
                )

            blob_sha = blob_response.json()["sha"]
            logger.debug(f"  Blob {idx + 1}/{len(files)}: {path} → {blob_sha[:7]}")

            tree_items.append(
                {
                    "path": path,
                    "mode": "100644",  # Regular file
                    "type": "blob",
                    "sha": blob_sha,
                },
            )

        # Step 4: Create new tree
        logger.debug("Step 4/6: Creating new tree")
        tree_url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/git/trees"
        tree_response = await client.post(
            tree_url,
            headers=self._get_headers(),
            json={"base_tree": current_tree_sha, "tree": tree_items},
            timeout=30.0,
        )

        if tree_response.status_code != 201:
            logger.error(f"Failed to create tree: {tree_response.text}")
            raise GitHubPublisherError(f"Failed to create tree: {tree_response.text}")

        new_tree_sha = tree_response.json()["sha"]
        logger.debug(f"New tree SHA: {new_tree_sha[:7]}")

        # Step 5: Create commit
        logger.debug("Step 5/6: Creating commit")
        create_commit_url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/git/commits"
        commit_create_response = await client.post(
            create_commit_url,
            headers=self._get_headers(),
            json={
                "message": (
                    f"{message}\n\n{self._fingerprint_marker(publication_fingerprint)}"
                    if deterministic_branch
                    else message
                ),
                "tree": new_tree_sha,
                "parents": [current_commit_sha],
            },
            timeout=30.0,
        )

        if commit_create_response.status_code != 201:
            logger.error(f"Failed to create commit: {commit_create_response.text}")
            raise GitHubPublisherError(f"Failed to create commit: {commit_create_response.text}")

        new_commit_sha = commit_create_response.json()["sha"]
        logger.debug(f"New commit SHA: {new_commit_sha[:7]}")

        if pull_request:
            # Step 6 (PR path): land the commit on a fresh branch, then open a
            # PR into `branch`, which pro.rakyat_editorial_merger merges.
            pr_branch = deterministic_branch or f"{pr_branch_prefix}/{new_commit_sha[:12]}"
            logger.debug("Step 6/6 (PR): creating branch '%s' + pull request", pr_branch)
            created = await self._create_branch_ref(pr_branch, new_commit_sha)
            if deterministic_branch and created is not True:
                # The keyed branch was there before this call, so it holds another
                # call's commit. This call's own commit is left without a ref, and
                # the answer comes from what the branch holds, once verified.
                # Anything but an exact True comes here: an answer nobody gave
                # is not "I created it".
                existing = await self._existing_publication(
                    head_branch=deterministic_branch,
                    base_branch=branch,
                    publication_fingerprint=publication_fingerprint,
                )
                if existing is None:
                    raise GitHubPublisherError(
                        "Publication branch disappeared before it could be verified"
                    )
                return await self._answer_with_existing(
                    existing,
                    head_branch=deterministic_branch,
                    base_branch=branch,
                    message=message,
                    files_count=len(files),
                    publication_fingerprint=publication_fingerprint,
                )
            if deterministic_branch:
                # This call created the branch, so its head is this call's commit.
                publication, opened_here = await self._open_publication_pull_request(
                    head_branch=deterministic_branch,
                    base_branch=branch,
                    message=message,
                    publication_fingerprint=publication_fingerprint,
                    commit_sha=new_commit_sha,
                )
                if not opened_here:
                    return self._resumed_answer(
                        publication,
                        head_branch=deterministic_branch,
                        files_count=len(files),
                    )
                pr = publication["pull_request"]
            else:
                # Unkeyed: the branch is named after the commit itself, nothing to verify.
                pr_title, pr_body = self._pull_request_text(message, publication_fingerprint)
                pr = await self._create_pull_request(
                    head_branch=pr_branch,
                    base_branch=branch,
                    title=pr_title,
                    body=pr_body,
                )

            elapsed_ms = (time.time() - start_time) * 1000
            logger.info(
                f"Article PR opened: #{pr['number']} {new_commit_sha[:7]} "
                f"({len(files)} files, {elapsed_ms:.0f}ms)",
                extra={
                    "commit_sha": new_commit_sha,
                    "pull_request_number": pr["number"],
                    "pr_branch": pr_branch,
                    "files_count": len(files),
                    "elapsed_ms": elapsed_ms,
                },
            )
            return {
                "success": True,
                "commit_sha": new_commit_sha,
                "files_count": len(files),
                "branch": pr_branch,
                "pull_request_url": pr["html_url"],
                "pull_request_number": pr["number"],
                "auto_merge_enabled": False,
            }

        # Step 6 (direct path): update branch reference (unprotected branches).
        logger.debug("Step 6/6: Updating branch reference")
        update_ref_response = await client.patch(
            ref_url,
            headers=self._get_headers(),
            json={"sha": new_commit_sha, "force": True},
            timeout=30.0,
        )

        if update_ref_response.status_code != 200:
            logger.error(f"Failed to update branch ref: {update_ref_response.text}")
            raise GitHubPublisherError(f"Failed to update branch ref: {update_ref_response.text}")

        elapsed_ms = (time.time() - start_time) * 1000

        logger.info(
            f"Atomic commit created: {new_commit_sha[:7]} ({len(files)} files, {elapsed_ms:.0f}ms)",
            extra={
                "commit_sha": new_commit_sha,
                "files_count": len(files),
                "branch": branch,
                "elapsed_ms": elapsed_ms,
                "total_size_kb": total_size_kb,
            },
        )

        return {
            "success": True,
            "commit_sha": new_commit_sha,
            "files_count": len(files),
            "branch": branch,
        }


# Singleton instance
github_publisher = GitHubPublisher()
