"""
Client Portal Magic-Link (passwordless) Login Service — FASE 6.

Flow:
1. Client requests a login link by email (`request_magic_link`).
2. We mint a single-use, 15-minute token, store ONLY its hash, and email the raw
   token as a link via Brevo (`from=zantara@balizero.com`). The raw token names the
   login and the client it was minted for: `<login id>~<client id>~<secret>`, both
   ids as unpadded base64url; the stored hash covers the whole string.
3. Client clicks the link; `verify_magic_link` parses the token, re-hashes it, checks
   it is unused + unexpired, marks it used, and returns that same login only while it
   still holds the address the link was sent to and is still linked to that client,
   so the router can issue the same JWT + cookies as a PIN login.

Security posture:
- Raw token never stored (hash at rest — superscar #4 secret-in-the-clear).
- Single-use (`used_at`), short TTL, replay-proof (UNIQUE token_hash).
- Enumeration-safe: `request_magic_link` returns the SAME result whether or not
  the email maps to a registered portal client. The caller surfaces a generic
  "if the account exists, a link was sent" message.
- Rate-limited per email (max N live tokens / window) to blunt mailbox flooding.

Distinct from `InviteService` (registration / PIN-set, 72h, plaintext token).
"""

import base64
import binascii
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from backend.app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# Token: 32 bytes urlsafe ≈ 43 chars of entropy; sha256 → 64 hex chars at rest.
MAGIC_TOKEN_BYTES = 32
MAGIC_LINK_TTL_MINUTES = 15
# Anti-flood: at most this many UNUSED, still-valid tokens per email at once.
MAX_LIVE_TOKENS_PER_EMAIL = 3


# The stored hash is domain-separated: the code before the login/client binding hashed
# the raw token alone, so it can never find (and resolve by address) a token of this
# format, whichever machine serves the request during a rolling deploy.
_TOKEN_HASH_DOMAIN = b"portal-magic-v2\x00"
_TOKEN_PART = re.compile(r"[A-Za-z0-9_-]+")
_CLIENT_ID_TEXT = re.compile(r"[1-9][0-9]{0,9}")
# team_members.linked_client_id is INTEGER.
_MAX_CLIENT_ID = 2_147_483_647


def _hash_token(raw_token: str) -> str:
    """sha256 hex of the domain prefix and the whole raw token (what we persist + match on)."""
    return hashlib.sha256(_TOKEN_HASH_DOMAIN + raw_token.encode("utf-8")).hexdigest()


def _b64u(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")


def _b64u_decode(part: str) -> bytes | None:
    """The bytes of a strict unpadded base64url part, or None if it is not their one spelling."""
    if not _TOKEN_PART.fullmatch(part) or len(part) % 4 == 1:
        return None
    try:
        decoded = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError):
        return None
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != part:
        return None
    return decoded


def _parse_token(raw_token: object) -> tuple[str, str, int] | None:
    """(the stripped token, its login id, its client id), or None. No database access.

    The stripped token is the one string both parsed and hashed. A token minted before
    the binding has no `~` and is refused here.
    """
    if not isinstance(raw_token, str):
        return None
    token = raw_token.strip()
    parts = token.split("~")
    if len(parts) != 3 or not all(parts):
        return None
    login_part, client_part, secret = parts
    if not _TOKEN_PART.fullmatch(secret):
        return None
    login_bytes = _b64u_decode(login_part)
    client_bytes = _b64u_decode(client_part)
    if login_bytes is None or client_bytes is None:
        return None
    try:
        login_id = login_bytes.decode("utf-8")
        client_text = client_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not login_id or "\x00" in login_id or not _CLIENT_ID_TEXT.fullmatch(client_text):
        return None
    client_id = int(client_text)
    if client_id > _MAX_CLIENT_ID:
        return None
    return token, login_id, client_id


class MagicLinkService:
    """Passwordless login for already-registered portal clients."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def request_magic_link(
        self,
        email: str,
        *,
        created_ip: str | None = None,
    ) -> dict[str, Any]:
        """
        Mint a magic-link token for `email` IF it is a registered portal client.

        Returns a dict that is identical in shape regardless of whether the email
        exists (enumeration-safe). When the email IS a portal client, the dict
        carries the raw `token` so the router can build + send the email; when it
        is not, `token` is None and no row is written.

        The caller MUST NOT leak the difference to the HTTP response body.
        """
        normalized = (email or "").strip().lower()
        if not normalized:
            return {"email": email, "token": None, "is_client": False}

        async with self.pool.acquire() as conn:
            # Only registered, active portal clients may receive a magic link.
            # The client record is read too, as the PIN login reads it: an
            # archived client keeps an active portal account row.
            user = await conn.fetchrow(
                """
                SELECT tm.id, tm.email, tm.full_name, tm.role, tm.portal_access, tm.active,
                       tm.linked_client_id
                FROM team_members tm
                JOIN clients c ON c.id = tm.linked_client_id
                WHERE LOWER(tm.email) = $1
                  AND tm.role = 'client'
                  AND tm.active = true
                  AND tm.portal_access = true
                  AND tm.linked_client_id IS NOT NULL
                  AND c.deleted_at IS NULL
                """,
                normalized,
            )
            if not user:
                # Enumeration-safe: same external behaviour as the happy path.
                logger.info("Magic-link requested for non-portal email (no-op)")
                return {"email": email, "token": None, "is_client": False}

            # Anti-flood: count live (unused, unexpired) tokens for this email.
            live = await conn.fetchval(
                """
                SELECT count(*) FROM magic_link_tokens
                WHERE LOWER(email) = $1
                  AND used_at IS NULL
                  AND expires_at > NOW()
                """,
                normalized,
            )
            if live and live >= MAX_LIVE_TOKENS_PER_EMAIL:
                logger.warning("Magic-link rate limit hit for a portal client (suppressed)")
                # Still enumeration-safe: report success without minting more.
                return {
                    "email": user["email"],
                    "token": None,
                    "is_client": True,
                    "rate_limited": True,
                    "name": user["full_name"],
                }

            raw_token = "~".join(
                (
                    _b64u(str(user["id"])),
                    _b64u(str(user["linked_client_id"])),
                    secrets.token_urlsafe(MAGIC_TOKEN_BYTES),
                )
            )
            token_hash = _hash_token(raw_token)
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)

            await conn.execute(
                """
                INSERT INTO magic_link_tokens (email, token_hash, expires_at, created_ip)
                VALUES ($1, $2, $3, $4)
                """,
                user["email"],
                token_hash,
                expires_at,
                created_ip,
            )

            logger.info("Magic-link minted for a portal client (ttl=%dm)", MAGIC_LINK_TTL_MINUTES)
            return {
                "email": user["email"],
                "token": raw_token,
                "is_client": True,
                "name": user["full_name"],
                "expires_at": expires_at.isoformat(),
            }

    async def verify_magic_link(self, raw_token: str) -> dict[str, Any] | None:
        """
        Validate + consume a magic-link token.

        Returns the login the token was minted for and its client binding on
        success, or None if the token is malformed, unknown, already used or
        expired, or its login no longer holds the address the link was sent to,
        is no longer linked to the client named in the token, or is no longer
        eligible. The match + the single-use mark happen in one transaction with
        `FOR UPDATE` to prevent a double-spend race.
        """
        parsed = _parse_token(raw_token)
        if parsed is None:
            logger.warning("Magic-link verify: malformed token")
            return None
        token, login_id, client_id = parsed
        token_hash = _hash_token(token)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, email, expires_at, used_at
                    FROM magic_link_tokens
                    WHERE token_hash = $1
                    FOR UPDATE
                    """,
                    token_hash,
                )
                if not row:
                    logger.warning("Magic-link verify: unknown token")
                    return None
                if row["used_at"] is not None:
                    logger.warning("Magic-link verify: token already used")
                    return None
                if row["expires_at"] < datetime.now(timezone.utc):
                    logger.warning("Magic-link verify: token expired")
                    return None

                # Consume the token (single-use).
                await conn.execute(
                    "UPDATE magic_link_tokens SET used_at = NOW() WHERE id = $1",
                    row["id"],
                )

                # Resolve the login the token names, only while it still holds the
                # address the link was sent to and the client it was minted for, and
                # is still eligible (access may have been revoked, or the client
                # archived, since the request).
                user = await conn.fetchrow(
                    """
                    SELECT tm.id, tm.email, COALESCE(tm.full_name, tm.name) AS name, tm.role,
                           tm.linked_client_id, tm.portal_access
                    FROM team_members tm
                    JOIN clients c ON c.id = tm.linked_client_id
                    WHERE tm.id = $1
                      AND LOWER(tm.email) = LOWER($2)
                      AND tm.linked_client_id = $3
                      AND tm.role = 'client'
                      AND tm.active = true
                      AND tm.portal_access = true
                      AND tm.linked_client_id IS NOT NULL
                      AND c.deleted_at IS NULL
                    """,
                    login_id,
                    row["email"],
                    client_id,
                )
                if not user:
                    logger.warning("Magic-link verify: user no longer eligible")
                    return None

                logger.info("Magic-link consumed — portal login granted")
                return {
                    "id": str(user["id"]),
                    "email": user["email"],
                    "name": user["name"],
                    "role": user["role"],
                    "client_id": user["linked_client_id"],
                    "portal_access": user["portal_access"],
                }
