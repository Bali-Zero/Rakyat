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

logger = logging.getLogger(__name__)

DOCUMENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
CLOSED_STATUSES = frozenset({"completed", "cancelled", "canceled"})
SEEN_WINDOW_DAYS = 90
LANGUAGES = ("en", "id")
DEFAULT_LANGUAGE = "id"
# The staff app, as the other internal emails of this package link to it.
KITA_PRACTICE_URL = "https://kita.balizero.com/process/{id}"


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
        if (
            not at
            or not local
            or "." not in domain.strip(".")
            or "@" in domain
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
