"""A consultant asks the client for one more document (spec BLOCK-1-VISA, slice V2).

Three parts, kept apart so each can be proved on its own:

* ``create_document_request`` writes the slot and the request row in ONE transaction. It never
  sends anything: an email for a request that was not committed is the worst outcome here.
* ``notify_reviewers`` runs after the commit. It never raises: a failing email or a failing
  lookup must not fail or undo a request that is already written.
* the text builders, in English and Bahasa Indonesia. Every text typed by the consultant is
  HTML-escaped in the body and kept as typed in the subject (minus line breaks). The service
  puts no client datum (name, number, contact, passport) in either: it does not even read the
  client.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import asyncpg

from backend.app.core.config import settings
from backend.app.services import internal_email
from backend.app.utils.crm_utils import is_crm_admin
from backend.app.utils.service_accounts import is_human_team_member
from backend.services.crm.service_cards import seeding_armed_for

logger = logging.getLogger(__name__)

DOCUMENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
CLOSED_STATUSES = frozenset({"completed", "cancelled", "canceled"})
SEEN_WINDOW_DAYS = 90
LANGUAGES = ("en", "id")
DEFAULT_LANGUAGE = "id"
# The staff app, as the other internal emails of this package link to it.
KITA_PRACTICE_URL = "https://kita.balizero.com/process/{id}"
# The reviewers' page, linked from the email under "Open the practice".
KITA_REVIEW_URL = "https://kita.balizero.com/review/document-requests"


class DocumentRequestRefused(Exception):
    """The request cannot be made; ``code`` is what the client of the API switches on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CreatedRequest:
    request_id: int
    slot: Any
    practice_type_id: int | None
    document_type: str
    requested_at: Any


def derive_document_type(name: str) -> str:
    """The key of a slot made from a free-text name; always matches DOCUMENT_TYPE_PATTERN."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_")
    if not slug:
        return "doc_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    if not slug[0].isalpha() or len(slug) < 2:
        slug = "doc_" + slug
    return slug[:50]


def reviewers_from_setting(raw: str | None) -> list[tuple[str, str]]:
    """``address`` or ``address:lang`` entries; an entry that cannot be used is skipped, logged
    without its address."""
    reviewers: list[tuple[str, str]] = []
    for position, entry in enumerate((raw or "").split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        address, _, lang = entry.partition(":")
        address = address.strip()
        lang = lang.strip().lower() or DEFAULT_LANGUAGE
        local, at, domain = address.partition("@")
        labels = domain.removesuffix(".").split(".")
        if (
            not at
            or not local
            or "@" in domain
            or len(labels) < 2
            or not all(labels)
            or any(ch.isspace() for ch in address)
        ):
            logger.warning("document request reviewers: entry %d is not an address", position)
            continue
        if lang not in LANGUAGES:
            logger.warning("document request reviewers: entry %d has an unknown language", position)
            continue
        reviewers.append((address, lang))
    return reviewers


async def create_document_request(
    conn: asyncpg.Connection,
    *,
    practice_id: int,
    requested_by: str,
    document_label: str,
    client_text: str,
    reason: str,
    document_type: str,
) -> CreatedRequest:
    """Slot + request row + the "tell the client" flag, in one transaction.

    Raises ``DocumentRequestRefused`` (``practice_closed`` / ``already_on_practice``); the
    transaction is then rolled back and nothing is written.
    """
    async with conn.transaction():
        practice = await conn.fetchrow(
            "SELECT status, practice_type_id FROM practices WHERE id = $1 FOR UPDATE",
            practice_id,
        )
        if practice is None:
            raise DocumentRequestRefused("practice_not_found", "Practice not found")
        if (practice["status"] or "").strip().lower() in CLOSED_STATUSES:
            raise DocumentRequestRefused(
                "practice_closed", "This practice is closed: no document can be asked on it."
            )
        slot = await conn.fetchrow(
            """
            INSERT INTO practice_required_documents
                (practice_id, document_type, document_label, description, is_required, created_by)
            VALUES ($1, $2, $3, $4, true, $5)
            ON CONFLICT (practice_id, document_type) DO NOTHING
            RETURNING *
            """,
            practice_id,
            document_type,
            document_label,
            client_text,
            requested_by,
        )
        if slot is None:
            raise DocumentRequestRefused(
                "already_on_practice", "This document is already on this practice."
            )
        request = await conn.fetchrow(
            """
            INSERT INTO practice_document_requests
                (practice_id, required_document_id, practice_type_id, document_type, label,
                 client_text, reason, requested_by, decision)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')
            RETURNING id, requested_at
            """,
            practice_id,
            slot["id"],
            practice["practice_type_id"],
            document_type,
            document_label,
            client_text,
            reason,
            requested_by,
        )
        await conn.execute(
            "UPDATE practices SET client_notification_sent = FALSE, updated_at = NOW() "
            "WHERE id = $1",
            practice_id,
        )
    return CreatedRequest(
        request_id=request["id"],
        slot=slot,
        practice_type_id=practice["practice_type_id"],
        document_type=document_type,
        requested_at=request["requested_at"],
    )


async def count_recent_requests(
    conn: asyncpg.Connection,
    practice_type_id: int | None,
    document_type: str,
    exclude_request_id: int,
) -> int:
    """Requests of the same service and document in the last 90 days, the one just written
    excluded. A practice without a service counts 0."""
    if practice_type_id is None:
        return 0
    count = await conn.fetchval(
        """
        SELECT count(*) FROM practice_document_requests
        WHERE practice_type_id = $1 AND document_type = $2
          AND requested_at > now() - make_interval(days => 90)
          AND id <> $3
        """,
        practice_type_id,
        document_type,
        exclude_request_id,
    )
    return int(count or 0)


def _one_line(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def _times(count: int, lang: str) -> str:
    if lang == "en":
        if count == 0:
            return "Not asked before in the last 90 days"
        return f"{count} time{'' if count == 1 else 's'} on this service in the last 90 days"
    if count == 0:
        return "Belum pernah diminta dalam 90 hari terakhir"
    return f"{count} kali pada layanan ini dalam 90 hari terakhir"


_COPY = {
    "en": {
        "subject": "One more document asked on {service}: {name}",
        "no_service": "a practice with no service",
        "title": "A consultant asked the client for one more document on this service",
        "document": "Document",
        "service": "Service",
        "no_service_value": "None set: this practice has no service",
        "practice": "Practice",
        "asked_by": "Asked by",
        "client_text": "What the client reads",
        "why": "Why",
        "seen": "Seen before",
        "open": "Open the practice",
        "decide": "Decide in Kita",
        "foot": "The client already sees the request. Nothing changes for other practices.",
    },
    "id": {
        "subject": "Satu dokumen lagi diminta pada {service}: {name}",
        "no_service": "praktik tanpa layanan",
        "title": "Seorang konsultan meminta satu dokumen lagi dari klien pada layanan ini",
        "document": "Dokumen",
        "service": "Layanan",
        "no_service_value": "Belum diatur: praktik ini tidak punya layanan",
        "practice": "Praktik",
        "asked_by": "Diminta oleh",
        "client_text": "Yang dibaca klien",
        "why": "Alasan",
        "seen": "Pernah diminta",
        "open": "Buka praktik",
        "decide": "Putuskan di Kita",
        "foot": "Klien sudah melihat permintaan ini. Praktik lain tidak berubah.",
    },
}


def build_email(
    *,
    lang: str,
    document_label: str,
    service_name: str | None,
    practice_id: int,
    requested_by: str,
    requested_at: Any,
    client_text: str,
    reason: str,
    seen_count: int,
) -> tuple[str, str]:
    """(subject, html body). The subject is plain text; the body escapes every typed text."""
    copy = _COPY[lang]
    e = html.escape
    subject = copy["subject"].format(
        service=_one_line(service_name) if service_name else copy["no_service"],
        name=_one_line(document_label),
    )
    when = requested_at.strftime("%Y-%m-%d %H:%M UTC") if requested_at is not None else ""
    link = KITA_PRACTICE_URL.format(id=practice_id)
    decide_link = KITA_REVIEW_URL
    service_value = e(service_name) if service_name else copy["no_service_value"]

    def row(label: str, value: str, block: bool = False) -> str:
        style = "" if not block else ' style="white-space:pre-wrap"'
        return (
            f'<tr><td style="color:#6b7280;padding:4px 16px 4px 0;vertical-align:top">'
            f"{label}</td><td{style}>{value}</td></tr>"
        )

    body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111827">'
        f'<h2 style="font-size:18px;margin:0 0 16px">{copy["title"]}</h2>'
        "<table>"
        + row(copy["document"], e(document_label))
        + row(copy["service"], service_value)
        + row(copy["practice"], f"#{practice_id}")
        + row(copy["asked_by"], f"{e(requested_by)} &middot; {e(when)}")
        + row(copy["client_text"], e(client_text), block=True)
        + row(copy["why"], e(reason), block=True)
        + row(copy["seen"], _times(seen_count, lang))
        + "</table>"
        f'<p><a href="{e(link, quote=True)}">{copy["open"]}</a></p>'
        f'<p><a href="{e(decide_link, quote=True)}">{copy["decide"]}</a></p>'
        f'<p style="color:#6b7280;font-size:12px">{copy["foot"]}</p>'
        "</div>"
    )
    return subject, body


async def notify_reviewers_after_commit(db_pool: asyncpg.Pool, **request: Any) -> bool:
    """What the endpoint calls once the request is committed. Never raises: the request is
    already written, so neither a pool that cannot give a connection nor a failing email may
    turn its answer into an error. Takes no connection when nobody is to be told."""
    try:
        if not reviewers_from_setting(getattr(settings, "service_card_reviewer_emails", "")):
            return False
        async with db_pool.acquire() as conn:
            return await notify_reviewers(conn, **request)
    except Exception as exc:
        logger.warning("document request notification failed: %s", type(exc).__name__)
        return False


async def notify_reviewers(
    conn: asyncpg.Connection,
    *,
    practice_id: int,
    created: CreatedRequest,
    requested_by: str,
    document_label: str,
    client_text: str,
    reason: str,
) -> bool:
    """One email per configured reviewer, after the commit. Never raises.

    ``reviewers_notified_at`` is set by a separate UPDATE once at least one send returned True,
    and stays NULL otherwise. Returns whether it was set.
    """
    try:
        reviewers = reviewers_from_setting(getattr(settings, "service_card_reviewer_emails", ""))
        if not reviewers:
            return False
        service_name: str | None = None
        if created.practice_type_id is not None:
            service_name = await conn.fetchval(
                "SELECT name FROM practice_types WHERE id = $1", created.practice_type_id
            )
        seen = await count_recent_requests(
            conn, created.practice_type_id, created.document_type, created.request_id
        )
        delivered = False
        for address, lang in reviewers:
            subject, body = build_email(
                lang=lang,
                document_label=document_label,
                service_name=service_name,
                practice_id=practice_id,
                requested_by=requested_by,
                requested_at=created.requested_at,
                client_text=client_text,
                reason=reason,
                seen_count=seen,
            )
            try:
                sent = await internal_email.send_internal_email(
                    to=address,
                    subject=subject,
                    body=body,
                    log_context=f"document_request id={created.request_id}",
                )
            except Exception as exc:
                logger.warning("document request email failed: %s", type(exc).__name__)
                continue
            delivered = delivered or bool(sent)
        if delivered:
            await conn.execute(
                "UPDATE practice_document_requests SET reviewers_notified_at = now() WHERE id = $1",
                created.request_id,
            )
        return delivered
    except Exception as exc:
        logger.warning("document request notification failed: %s", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------------------------
# The reviewers' side (V2, second cut): who may decide, the list, the decision, the reopen.
# Nothing here writes a practice, a slot, or the request's own label and client text.
# ---------------------------------------------------------------------------------------------

DECISIONS = ("added_to_card", "case_only")
_REQUEST_LOCK = """
    SELECT id, practice_type_id, document_type, label, client_text, decision
    FROM practice_document_requests WHERE id = $1 FOR UPDATE
"""
_CARD_LOCK = "SELECT practice_type_id FROM service_cards WHERE practice_type_id = $1 FOR UPDATE"
_TOUCH_CARD = (
    "UPDATE service_cards SET updated_by = $2, updated_at = now() WHERE practice_type_id = $1"
)

_LIST_FROM = """
    FROM practice_document_requests r
    JOIN practices p ON p.id = r.practice_id
"""
_PENDING_FILTER = """
    WHERE r.decision = 'pending'
      AND lower(btrim(coalesce(p.status, ''), E' \\t\\n\\r\\f\\v')) NOT IN ('cancelled', 'canceled')
      AND EXISTS (SELECT 1 FROM practice_required_documents d WHERE d.id = r.required_document_id)
"""
_DECIDED_FILTER = "WHERE r.decision <> 'pending'"


def is_card_reviewer(user: dict[str, Any] | None) -> bool:
    """A CRM admin, or a person on the team whose address is an accepted entry of the setting
    that says who is emailed. A token without an email or without a role never passes there."""
    if not user:
        return False
    if is_crm_admin(user):
        return True
    email = str(user.get("email") or "").strip().lower()
    if not email or not is_human_team_member(user.get("role")):
        return False
    accepted = {
        address.lower()
        for address, _lang in reviewers_from_setting(
            getattr(settings, "service_card_reviewer_emails", "")
        )
    }
    return email in accepted


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _list_item(row: Any) -> dict[str, Any]:
    if not row["has_card"]:
        card_state = "no_card"
    elif row["on_card"]:
        card_state = "on_card"
    else:
        card_state = "can_add"
    card_live = bool(row["has_card"] and row["card_active"] and seeding_armed_for(row["type_code"]))
    return {
        "id": row["id"],
        "practice_id": row["practice_id"],
        "service_name": row["service_name"],
        "document_type": row["document_type"],
        "label": row["label"],
        "client_text": row["client_text"],
        "reason": row["reason"],
        "requested_by": row["requested_by"],
        "requested_at": _iso(row["requested_at"]),
        "seen_count": int(row["seen_count"] or 0),
        "card_state": card_state,
        "card_live": card_live,
        "decision": row["decision"],
        "decision_condition": row["decision_condition"],
        "decided_by": row["decided_by"],
        "decided_at": _iso(row["decided_at"]),
    }


async def list_requests(
    conn: asyncpg.Connection, *, state: str, limit: int, offset: int
) -> dict[str, Any]:
    """The requests to decide (``pending``) or already decided. No column of the client is read."""
    if state == "pending":
        where, order = _PENDING_FILTER, "r.requested_at DESC, r.id DESC"
    else:
        where, order = _DECIDED_FILTER, "r.decided_at DESC, r.id DESC"
    total = await conn.fetchval(f"SELECT count(*) {_LIST_FROM} {where}")
    rows = await conn.fetch(
        f"""
        SELECT r.id, r.practice_id, pt.name AS service_name, r.document_type, r.label,
               r.client_text, r.reason, r.requested_by, r.requested_at, r.decision,
               r.decision_condition, r.decided_by, r.decided_at,
               (SELECT count(*) FROM practice_document_requests e
                 WHERE e.practice_type_id = r.practice_type_id
                   AND e.document_type = r.document_type
                   AND (e.requested_at < r.requested_at
                        OR (e.requested_at = r.requested_at AND e.id < r.id))
                   AND e.requested_at > r.requested_at - make_interval(days => {SEEN_WINDOW_DAYS})
               ) AS seen_count,
               (sc.practice_type_id IS NOT NULL) AS has_card,
               coalesce(sc.active, false) AS card_active,
               pt.code AS type_code,
               EXISTS (SELECT 1 FROM service_card_documents cd
                        WHERE cd.practice_type_id = r.practice_type_id
                          AND cd.document_type = r.document_type) AS on_card
        {_LIST_FROM}
        LEFT JOIN practice_types pt ON pt.id = r.practice_type_id
        LEFT JOIN service_cards sc ON sc.practice_type_id = r.practice_type_id
        {where}
        ORDER BY {order}
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )
    return {"items": [_list_item(row) for row in rows], "total": int(total or 0)}


async def _lock_request(conn: asyncpg.Connection, request_id: int) -> Any:
    row = await conn.fetchrow(_REQUEST_LOCK, request_id)
    if row is None:
        raise DocumentRequestRefused("request_not_found", "Document request not found")
    return row


async def decide_request(
    conn: asyncpg.Connection,
    request_id: int,
    *,
    decision: str,
    condition: str | None,
    client_text: str | None,
    reviewer: str,
) -> dict[str, Any]:
    """Decide one request in ONE transaction; locks in the order request, card, card documents.

    Raises ``DocumentRequestRefused`` (``request_not_found`` / ``already_decided`` / ``no_card`` /
    ``already_on_card``); the transaction is then rolled back and nothing is written.
    """
    async with conn.transaction():
        request = await _lock_request(conn, request_id)
        if request["decision"] != "pending":
            raise DocumentRequestRefused("already_decided", "This request was already decided.")
        type_id = request["practice_type_id"]
        kept_condition = condition if decision == "added_to_card" else None
        if decision == "added_to_card":
            if type_id is None or await conn.fetchrow(_CARD_LOCK, type_id) is None:
                raise DocumentRequestRefused("no_card", "This service has no card to add to.")
            inserted = await conn.fetchrow(
                """
                INSERT INTO service_card_documents
                    (practice_type_id, document_type, label, client_text, provider,
                     condition_text, reuse, source, source_request_id, sort_order, active,
                     created_by)
                VALUES ($1, $2, $3, $4, 'client', $5, 'fresh', 'request', $6,
                        (SELECT COALESCE(MAX(sort_order), 0) + 1
                           FROM service_card_documents WHERE practice_type_id = $1),
                        true, $7)
                ON CONFLICT (practice_type_id, document_type) DO NOTHING
                RETURNING id
                """,
                type_id,
                request["document_type"],
                request["label"],
                client_text if client_text is not None else request["client_text"],
                condition,
                request_id,
                reviewer,
            )
            if inserted is None:
                raise DocumentRequestRefused(
                    "already_on_card", "This document is already on the card."
                )
        updated = await conn.fetchrow(
            """
            UPDATE practice_document_requests
            SET decision = $2, decision_condition = $3, decided_by = $4, decided_at = now()
            WHERE id = $1
            RETURNING id, decision, decision_condition, decided_by, decided_at
            """,
            request_id,
            decision,
            kept_condition,
            reviewer,
        )
        if decision == "added_to_card":
            await conn.execute(_TOUCH_CARD, type_id, reviewer)
        # Built inside the transaction: nothing that can fail runs after the commit.
        answer = {
            "id": updated["id"],
            "decision": updated["decision"],
            "decision_condition": updated["decision_condition"],
            "decided_by": updated["decided_by"],
            "decided_at": updated["decided_at"].isoformat(),
        }
    return answer


async def reopen_request(
    conn: asyncpg.Connection, request_id: int, *, reviewer: str
) -> dict[str, Any]:
    """Put a decided request back to pending; removes the card row this request created, and
    only that one. Same lock order as a decision, so the two cannot deadlock."""
    removed = False
    async with conn.transaction():
        request = await _lock_request(conn, request_id)
        if request["decision"] == "pending":
            raise DocumentRequestRefused("not_decided", "This request is not decided.")
        if request["decision"] == "added_to_card":
            type_id = request["practice_type_id"]
            if type_id is not None:
                await conn.fetchrow(_CARD_LOCK, type_id)
            deleted = await conn.fetch(
                """
                DELETE FROM service_card_documents
                WHERE source = 'request' AND source_request_id = $1
                RETURNING id, practice_type_id
                """,
                request_id,
            )
            removed = bool(deleted)
            if removed:
                await conn.execute(_TOUCH_CARD, deleted[0]["practice_type_id"], reviewer)
        await conn.fetchrow(
            """
            UPDATE practice_document_requests
            SET decision = 'pending', decision_condition = NULL, decided_by = NULL,
                decided_at = NULL
            WHERE id = $1
            RETURNING id
            """,
            request_id,
        )
    return {"id": request_id, "decision": "pending", "card_document_removed": removed}
