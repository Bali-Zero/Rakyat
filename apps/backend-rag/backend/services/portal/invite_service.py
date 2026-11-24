"""
Client Portal Invite Service

Handles client invitation flow:
1. Team sends invite to client email
2. Client clicks link, validates token
3. Client creates PIN to complete registration
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import bcrypt
from pydantic.networks import validate_email
from pydantic_core import PydanticCustomError

from backend.app.utils.logging_utils import get_logger
from backend.app.utils.service_accounts import is_group_staff
from backend.services.common.cache import cache_invalidating
from backend.services.portal.portal_profile_service import PLACEHOLDER_PIN_HASH

logger = get_logger(__name__)

# Constants
INVITE_TOKEN_LENGTH = 64
INVITE_EXPIRY_HOURS = 72  # 3 days

# The path an invitation link points at, relative to `settings.frontend_portal_url`.
# A constant rather than an inline f-string because the ROUTER concatenates the two
# halves and the router's tests used to hand-write the other half — they asserted a
# `/portal/invite?token=` link that this service has not produced for a long time, so
# a test could stay green while the real link rotted (cross-family review, 2026-09-10).
INVITE_PATH_TEMPLATE = "/portal/register?token={token}"

# A stored invitation token is a sha256 hex digest: 64 lowercase hex chars.
# A RAW token is `secrets.token_urlsafe(64)` — ~86 base64url chars — so the
# two shapes can never be confused, which is what makes the legacy read path
# below safe (see `_LEGACY_PLAINTEXT_PREDICATE`).
_HASHED_TOKEN_RE = "^[0-9a-f]{64}$"


def _hash_token(raw_token: str) -> str:
    """sha256 hex of the raw token — what we persist and match on.

    Mirrors `magic_link_service._hash_token`, whose module docstring already
    contrasted itself against this service ("72h, plaintext token"). A raw
    `client_invitations.token` is a live, unused, 72h-valid registration
    credential: anyone who can read that table — a DB console, a future
    export endpoint, a logging accident — could redeem it without ever
    touching the client's mailbox (portal audit finding, authz F1).
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# Rows written BEFORE this change still hold a plaintext token, so the two
# read paths accept either. The predicate is not cosmetic: without it, the
# value stored in the row would itself be a working credential again — an
# attacker reading the table could submit the stored hash AS the token and
# the plaintext branch would match it. Restricting the plaintext branch to
# rows that do NOT look like a digest closes that door.
#
# Those legacy rows are not scrubbed here. Measured on production
# 2026-09-11: 245 invitation rows, ZERO of them live (`used_at IS NULL AND
# expires_at > NOW()`), so every plaintext token still on disk is already
# spent or expired and cannot be redeemed. Rewriting them is a one-line
# UPDATE against a hot-zone migration path (deterministic floor 3) for no
# change in exposure, so it is left as an operator-run follow-up:
#   UPDATE client_invitations
#      SET token = encode(sha256(token::bytea), 'hex')
#    WHERE token !~ '^[0-9a-f]{64}$';
# (verified equal to Python's sha256 for the same input). Once that has
# run, this branch matches nothing and can be deleted.
_LEGACY_PLAINTEXT_PREDICATE = (
    "(i.token = $1 OR (i.token = $2 AND i.token !~ '" + _HASHED_TOKEN_RE + "'))"
)


#: The token lookup of both doors. A legacy plaintext row and a digest row can
#: both match one token; the digest row wins, then the lowest id, so the two
#: doors always read the same invitation. `complete_registration` appends its
#: row lock to this same text.
_INVITATION_BY_TOKEN_SQL = (
    """
    SELECT i.id, i.client_id, i.email, i.expires_at, i.used_at,
           c.full_name AS client_name, c.email AS client_email
    FROM client_invitations i
    JOIN clients c ON c.id = i.client_id AND c.deleted_at IS NULL
    WHERE """
    + _LEGACY_PLAINTEXT_PREDICATE
    + """
    ORDER BY (i.token = $1) DESC, i.id
    LIMIT 1
    """
)

#: The registration state of both doors, one statement and always one row: the
#: login already linked to the client (a client-role row first, then the
#: oldest, then the lowest id) and another login holding the address.
#:
#: `$2` is the canonical address computed in Python, never recomputed here, so
#: Unicode whitespace and letter case are settled by one rule only; the stored
#: `team_members.email` is lowered as before and not trimmed. "Another" means
#: any row but the linked one, or any row when none is linked. With no
#: invitation both parameters are NULL and the row is all NULL: the check runs
#: the same two statements whatever the token turns out to be.
_REGISTRATION_STATE_SQL = """
    SELECT l.id AS linked_id, l.active AS linked_active,
           l.pin_hash AS linked_pin_hash, l.role AS linked_role,
           h.id AS holder_id
    FROM (SELECT 1) AS one
    LEFT JOIN LATERAL (
        SELECT id, active, pin_hash, role FROM team_members
        WHERE linked_client_id = $1
        ORDER BY (role = 'client') DESC, created_at, id
        LIMIT 1
    ) l ON TRUE
    LEFT JOIN LATERAL (
        SELECT id FROM team_members
        WHERE $2 <> '' AND LOWER(email) = LOWER($2) AND id IS DISTINCT FROM l.id
        LIMIT 1
    ) h ON TRUE
"""

#: Why an invitation that is unused and in date still cannot register, as
#: (the code `/invite/validate` answers, the sentence `complete_registration`
#: raises). The first and the third are never answered to the token holder:
#: `validate_token` treats them as an unknown token (see there).
_NOT_REGISTRABLE = ("not_registrable", "This account cannot be registered")
_ALREADY_REGISTERED = ("already_registered", "This client already has an active portal account")
_EMAIL_IN_USE = ("email_in_use", "Another account already uses this email address")
_ADDRESS_REQUIRED = ("address_required", "Invitation address is required")


def _canonical_address(invitation: Any) -> str:
    """The address the login will hold, in its one canonical spelling ("" when blank)."""
    return (invitation["email"] or "").strip().lower()


class InvitationAddressNotOnRecord(ValueError):
    """The address an invitation would be minted for is not the client's
    address on record at the moment of minting."""


def comparable_address(text: str | None) -> str:
    """The form in which two spellings of one mailbox compare equal.

    A request address arrives through pydantic's EmailStr, which turns an
    IDNA domain into Unicode, composes Unicode (NFC) and lowercases the
    domain. A stored address is given the same treatment, then both are
    lowercased whole. A stored value that does not parse as an address is
    compared trimmed and lowercased, so it can never match a valid request.
    """
    stripped = (text or "").strip()
    try:
        stripped = validate_email(stripped)[1]
    except PydanticCustomError:
        pass
    return stripped.lower()


def _invitation_is_on_record(invitation: Any) -> bool:
    """Whether the invitation's address is still the client's address on record.

    Both doors ask it: an invitation whose client has since moved to another
    address, or has none, is answered as expired and can never be redeemed.
    """
    on_record = comparable_address(invitation["client_email"])
    return bool(on_record) and comparable_address(invitation["email"]) == on_record


async def _registration_state(
    conn: Any, client_id: int | None, canonical: str | None
) -> tuple[dict[str, Any] | None, int | None]:
    """The login linked to the client (or None) and the id of another login
    holding `canonical` (or None), read in one statement."""
    row = await conn.fetchrow(_REGISTRATION_STATE_SQL, client_id, canonical)
    if row is None:
        return None, None
    linked = None
    if row["linked_id"] is not None:
        linked = {
            "id": row["linked_id"],
            "active": row["linked_active"],
            "pin_hash": row["linked_pin_hash"],
            "role": row["linked_role"],
        }
    return linked, row["holder_id"]


def _refusal_of(
    linked: dict[str, Any] | None, canonical: str, holder_id: int | None
) -> tuple[str, str] | None:
    """Why registering through an invitation that would write `canonical` is
    refused, or None. One decision for both doors: `validate_token` answers it
    before the client chooses a PIN and `complete_registration` enforces it, so
    the invitation page never offers a form the registration is certain to
    refuse.
    """
    if linked is not None and is_group_staff(linked["role"]):
        # Group staff hold an HR card, never an app login.
        return _NOT_REGISTRABLE

    if linked is not None:
        # Consuming an invitation is not proof the consumer controls the
        # client's mailbox: it must never work as a password reset on a LIVE
        # account. `active` alone cannot say "live", because
        # `ensure_portal_profile` provisions every CRM client as active with
        # PLACEHOLDER_PIN_HASH; the credential is the honest signal. A row
        # still holding the placeholder has never been registered, so writing
        # the first real PIN over it is onboarding, and a deactivated account
        # may be re-onboarded.
        if linked["pin_hash"] != PLACEHOLDER_PIN_HASH and linked["active"]:
            return _ALREADY_REGISTERED
        # team_members.email is unique: another row already holding the
        # address is a real identity collision, not something to overwrite.
        if canonical and holder_id is not None:
            return _EMAIL_IN_USE
        return None

    # No row is linked to this client yet, but the address can still belong
    # to another row, a staff member or another client's login. The unique
    # index is on the exact text while login and every lookup lower it, so an
    # address differing only in letter case would become a second login for
    # one mailbox; it is refused on the address the row would be written with.
    if not canonical:
        return _ADDRESS_REQUIRED
    if holder_id is not None:
        return _EMAIL_IN_USE
    return None


#: Refusals `validate_token` answers as an unknown token. The link holder is not
#: authenticated: that another login holds the address, or that the client is
#: a staff member, is not theirs to learn, and a distinct answer would tell it.
_ANSWERED_AS_UNKNOWN = frozenset({_EMAIL_IN_USE, _NOT_REGISTRABLE})


class InviteService:
    """Service for managing client portal invitations."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @cache_invalidating(
        [
            lambda self, client_id, *a, **k: f"zantara:portal_invitations:{client_id}:*",
            "zantara:portal_invitations:*",
        ]
    )
    async def create_invitation(
        self,
        client_id: int,
        email: str,
        created_by: str,
    ) -> dict[str, Any]:
        """
        Create a new invitation for a client.

        Args:
            client_id: ID of the client in clients table
            email: Client's email address
            created_by: Email of team member sending invite

        Returns:
            Dict with invitation details including token
        """
        # The address is a login identity: one canonical spelling from here on.
        email = (email or "").strip().lower()
        if not email:
            raise ValueError("Invitation address is required")
        # Generate secure token
        token = secrets.token_urlsafe(INVITE_TOKEN_LENGTH)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=INVITE_EXPIRY_HOURS)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Check if client exists and is not archived — an archived client
                # must never be (re-)invited into an active portal account.
                #
                # FOR UPDATE holds the client row until this mint commits: an
                # address edit either waits for the mint or is read by it. The
                # mint then waits on nothing else: the retirement below skips a
                # row another transaction holds, and the insert's foreign-key
                # check is on this same client. Registration holds an invitation
                # and then waits for the client, so no lock cycle exists. A row
                # the retirement skips can be redeemed only while its address is
                # the one on record (`_invitation_is_on_record`).
                client = await conn.fetchrow(
                    "SELECT id, full_name, email FROM clients WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
                    client_id,
                )
                if not client:
                    raise ValueError(f"Client with ID {client_id} not found")

                # Every caller passes the address it read from the client record,
                # but that read and this one are separate: a record edited in
                # between would mint a link for the old address. Checked here,
                # on the row this mint reads, before anything is written.
                if not client["email"] or comparable_address(client["email"]) != comparable_address(
                    email
                ):
                    raise InvitationAddressNotOnRecord(
                        "The invitation address is not the e-mail on the client record"
                    )

                # A re-invite to the SAME address must not kill the link already
                # sitting in that inbox. It used to: every send expired the
                # previous live invitation, so a client who opened the first mail
                # of a thread — the oldest, top of the Gmail conversation — met
                # "This invitation has expired" on a link minutes old. Measured
                # on production 2026-09-21: 16 invitations superseded unused in
                # 30 days across 14 clients, one of them three times in 30
                # minutes. Both mails reach the same mailbox, so a second live
                # token grants nothing the first did not; `complete_registration`
                # retires the siblings once one of them is used.
                #
                # A live invitation to a DIFFERENT address is still retired: the
                # consultant changed where the client is reached, and the old
                # mailbox must not keep a registration credential.
                await conn.execute(
                    """
                UPDATE client_invitations SET expires_at = NOW()
                WHERE id IN (
                    SELECT id FROM client_invitations
                    WHERE client_id = $1 AND used_at IS NULL AND expires_at > NOW()
                      AND LOWER(email) <> LOWER($2)
                    FOR UPDATE SKIP LOCKED
                )
                """,
                    client_id,
                    email,
                )

                # Create new invitation
                invitation = await conn.fetchrow(
                    """
                INSERT INTO client_invitations (client_id, email, token, expires_at, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, expires_at, created_at
                """,
                    client_id,
                    email,
                    _hash_token(token),
                    expires_at,
                    created_by,
                )

                # The address is deliberately NOT interpolated here. This line pre-dates
                # the outbox: it was written for the human-triggered
                # `/api/portal/invite/send` endpoint, where a staff member already had
                # the address on screen. It now also fires UNATTENDED on every paid
                # GARUDA order, which turns it into a steady stream of client PII into
                # a persisted log (SYMBIOSIS Law 2 / UU PDP) that no Sentry scrubber
                # touches — `_before_send` filters events, not log sinks. `client_id`
                # identifies the row for anyone debugging; the address adds nothing.
                logger.info("Created invitation for client %s by %s", client_id, created_by)

                return {
                    "invitation_id": invitation["id"],
                    "client_id": client_id,
                    "client_name": client["full_name"],
                    "email": email,
                    "token": token,
                    "expires_at": invitation["expires_at"].isoformat(),
                    "invite_url": INVITE_PATH_TEMPLATE.format(token=token),
                }

    async def validate_token(self, token: str) -> dict[str, Any] | None:
        """
        Validate an invitation token.

        Every token costs the same two statements (the token lookup, then the
        registration state, with NULL parameters when no invitation matched)
        and writes the same one log line, whatever the outcome. A refusal that
        would reveal another login holding the address, or a staff login, is
        answered as an unknown token.

        Args:
            token: Invitation token from URL

        Returns:
            Dict with invitation details if valid, None otherwise
        """
        digest = _hash_token(token)
        async with self.pool.acquire() as conn:
            invitation = await conn.fetchrow(_INVITATION_BY_TOKEN_SQL, digest, token)
            canonical = _canonical_address(invitation) if invitation else None
            linked, holder_id = await _registration_state(
                conn, invitation["client_id"] if invitation else None, canonical
            )

        outcome, answer = self._validate_outcome(invitation, canonical, linked, holder_id)
        # The DIGEST prefix, never the raw token's: a log line is storage too.
        logger.info("invitation validate: %s %s...", outcome, digest[:8])
        return answer

    @staticmethod
    def _validate_outcome(
        invitation: Any,
        canonical: str | None,
        linked: dict[str, Any] | None,
        holder_id: int | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """(the outcome logged, the answer returned) for one validation."""
        if not invitation:
            return "invalid_token", None

        if invitation["used_at"] is not None:
            return "already_used", {
                "error": "already_used",
                "message": "This invitation has already been used",
            }

        if invitation["expires_at"] < datetime.now(timezone.utc):
            return "expired", {"error": "expired", "message": "This invitation has expired"}

        if not _invitation_is_on_record(invitation):
            return "address_changed", {"error": "expired", "message": "This invitation has expired"}

        refusal = _refusal_of(linked, canonical or "", holder_id)
        if refusal in _ANSWERED_AS_UNKNOWN:
            return refusal[0], None
        if refusal is not None:
            return refusal[0], {"error": refusal[0], "message": refusal[1]}

        return "valid", {
            "valid": True,
            "invitation_id": invitation["id"],
            "client_id": invitation["client_id"],
            "client_name": invitation["client_name"],
            "email": invitation["email"],
        }

    @cache_invalidating(
        [
            "zantara:portal_invitations:*",
            "zantara:crm_clients:*",
        ]
    )
    async def complete_registration(
        self,
        token: str,
        pin: str,
    ) -> dict[str, Any]:
        """
        Complete client registration by setting PIN.

        Args:
            token: Invitation token
            pin: Client's chosen PIN (4-6 digits)

        Returns:
            Dict with success status and user info
        """
        # Validate PIN format
        if not pin.isdigit() or len(pin) < 4 or len(pin) > 6:
            raise ValueError("PIN must be 4-6 digits")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Get and validate invitation
                invitation = await conn.fetchrow(
                    _INVITATION_BY_TOKEN_SQL + "FOR UPDATE OF i, c\n",
                    _hash_token(token),
                    token,
                )

                if not invitation:
                    raise ValueError("Invalid invitation token")

                if invitation["used_at"] is not None:
                    raise ValueError("Invitation already used")

                if invitation["expires_at"] < datetime.now(timezone.utc):
                    raise ValueError("Invitation expired")

                if not _invitation_is_on_record(invitation):
                    raise ValueError("Invitation expired")

                # The address the login will hold, in its one canonical spelling:
                # every check below and every write use this same value.
                canonical = _canonical_address(invitation)

                # The same statement `validate_token` runs. It takes no row
                # lock: the invitation and the client row locked above are the
                # whole lock set, in the order the sibling retirement below
                # relies on.
                existing_user, holder_id = await _registration_state(
                    conn, invitation["client_id"], canonical
                )
                refusal = _refusal_of(existing_user, canonical, holder_id)
                if refusal is not None:
                    raise ValueError(refusal[1])

                # Hash PIN
                pin_hash = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()

                if existing_user:
                    # The login identity must be the address the invitation
                    # was SENT to, or registration succeeds and login fails.
                    #
                    # Measured 2026-09-11 (portal audit F1): `update_client`
                    # lets a consultant change `clients.email` freely, the
                    # re-invite correctly goes to the NEW address, the client
                    # sets a PIN through it — and then cannot sign in, because
                    # `auth.py`'s login matches `team_members.email` only and
                    # this UPDATE never touched it. `ensure_portal_profile`
                    # cannot repair it either: `email` IS its ON CONFLICT key.
                    # Nothing surfaced the failure to either side; the CRM
                    # panel kept reporting "Portal active — <dead address>".
                    # Invitations stored before addresses were normalised may still
                    # hold mixed case; the login row is always written lower-case.
                    invite_email = canonical or None

                    # Update existing (never-registered or deactivated) user
                    await conn.execute(
                        """
                        UPDATE team_members
                        SET pin_hash = $1,
                            active = true,
                            portal_access = true,
                            email = COALESCE($3, email)
                        WHERE id = $2
                        """,
                        pin_hash,
                        existing_user["id"],
                        invite_email,
                    )
                    user_id = existing_user["id"]
                else:
                    # Create new team_member with role='client'
                    # Note: team_members has both 'name' (required) and 'full_name' columns
                    user = await conn.fetchrow(
                        """
                        INSERT INTO team_members (
                            name, email, full_name, role, pin_hash, active,
                            linked_client_id, portal_access
                        )
                        VALUES ($1, $2, $3, 'client', $4, true, $5, true)
                        RETURNING id
                        """,
                        invitation["client_name"],  # name (required)
                        canonical,
                        invitation["client_name"],  # full_name
                        pin_hash,
                        invitation["client_id"],
                    )
                    user_id = user["id"]

                # Mark invitation as used
                await conn.execute(
                    """
                    UPDATE client_invitations
                    SET used_at = NOW()
                    WHERE id = $1
                    """,
                    invitation["id"],
                )

                # Same-address re-invites stay live side by side (see
                # `create_invitation`); once one is redeemed, the others
                # stop being credentials.
                #
                # SKIP LOCKED because a sibling can be mid-redemption in
                # another transaction: that one holds the sibling row and
                # waits on the `clients` row this transaction locked above,
                # so a plain UPDATE deadlocks (reproduced on Postgres 17,
                # the loser surfacing as an HTTP 500). Skipped, the sibling
                # proceeds after this commit and is refused by the
                # active-account guard above.
                await conn.execute(
                    """
                    UPDATE client_invitations
                    SET expires_at = NOW()
                    WHERE id IN (
                        SELECT id FROM client_invitations
                        WHERE client_id = $1 AND id <> $2
                          AND used_at IS NULL AND expires_at > NOW()
                        FOR UPDATE SKIP LOCKED
                    )
                    """,
                    invitation["client_id"],
                    invitation["id"],
                )

                # Create default preferences
                await conn.execute(
                    """
                    INSERT INTO client_preferences (client_id)
                    VALUES ($1)
                    ON CONFLICT (client_id) DO NOTHING
                    """,
                    invitation["client_id"],
                )

                logger.info(
                    f"Client registration completed (client_id={invitation['client_id']})",
                )

                return {
                    "success": True,
                    "user_id": user_id,
                    "client_id": invitation["client_id"],
                    "email": (invitation["email"] or "").strip().lower(),
                    "name": invitation["client_name"],
                }

    async def get_client_invitations(
        self,
        client_id: int,
    ) -> list[dict[str, Any]]:
        """Get all invitations for a client."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, email, expires_at, used_at, created_by, created_at
                FROM client_invitations
                WHERE client_id = $1
                ORDER BY created_at DESC
                """,
                client_id,
            )

            return [
                {
                    "id": row["id"],
                    "email": row["email"],
                    "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                    "used_at": row["used_at"].isoformat() if row["used_at"] else None,
                    "created_by": row["created_by"],
                    "created_at": row["created_at"].isoformat(),
                    "status": "used"
                    if row["used_at"]
                    else (
                        "expired" if row["expires_at"] < datetime.now(timezone.utc) else "pending"
                    ),
                }
                for row in rows
            ]

    @cache_invalidating(
        [
            lambda self, client_id, *a, **k: f"zantara:portal_invitations:{client_id}:*",
            "zantara:portal_invitations:*",
        ]
    )
    async def resend_invitation(
        self,
        client_id: int,
        created_by: str,
    ) -> dict[str, Any]:
        """Resend invitation to a client (creates new token)."""
        async with self.pool.acquire() as conn:
            # Get client email — archived clients are excluded so an archived
            # client can't be re-invited via the resend path either.
            client = await conn.fetchrow(
                "SELECT email FROM clients WHERE id = $1 AND deleted_at IS NULL",
                client_id,
            )
            if not client or not client["email"]:
                raise ValueError("Client not found or has no email")

            return await self.create_invitation(
                client_id=client_id,
                email=client["email"],
                created_by=created_by,
            )
