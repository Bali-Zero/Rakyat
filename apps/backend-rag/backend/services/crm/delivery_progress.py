"""Which final documents a client can already see, and what that lets us tell them.

The client is told a practice is "fully complete" only when every final document is delivered
and visible. A practice can complete with documents still to come (the first proof completes it),
so the completion messages ask this module which wording is true.
"""

from dataclasses import dataclass
from typing import Any

from backend.services.crm.admin_document_visibility import admin_document_not_deleted_clause
from backend.services.crm.delivery_catalog import delivery_policy
from backend.services.portal._document_visibility import document_visibility_clause

# Same slot scope and liveness as practice_delivery (not imported: that module pulls the
# upload stack, this one is read by the listener and the portal).
_SCOPED = """d.practice_id = p.id AND d.client_id = p.client_id
             AND d.family_member_id IS NOT DISTINCT FROM p.family_member_id"""
_LIVE = f"{admin_document_not_deleted_clause('d')} AND NOT COALESCE(d.is_archived, FALSE)"
_CUSTOM_SERVICES = {"other_custom", "company_revision"}

FULL, PARTIAL, NEUTRAL, LEGACY = "full", "partial", "neutral", "legacy"


@dataclass(frozen=True)
class DeliveryProgress:
    pending: bool
    accessible_any: bool
    accessible_all: bool

    @property
    def wording(self) -> str:
        if not self.accessible_any:
            return NEUTRAL
        if not self.pending and self.accessible_all:
            return FULL
        return PARTIAL


def wording_of(progress: DeliveryProgress | None) -> str:
    """None means the service has no delivery policy: the wording that was always sent."""
    return LEGACY if progress is None else progress.wording


async def documents_pending(conn: Any, practice_id: int) -> DeliveryProgress | None:
    """Facts about the practice's final documents; None when the service has no delivery policy.

    pending: a slot (catalog or custom title) with neither a live document in scope nor a waiver.
    accessible_any / accessible_all: slot documents the client predicate of portal_files lets the
    client see, for one / every non-waived slot.
    """
    p = await conn.fetchrow(
        """SELECT p.id, pt.code
           FROM practices p JOIN practice_types pt ON pt.id = p.practice_type_id WHERE p.id = $1""",
        practice_id,
    )
    policy = delivery_policy(p["code"]) if p else None
    if not policy:
        return None
    keys = [slot["key"] for slot in policy["slots"]]
    if p["code"] in _CUSTOM_SERVICES:
        titles = await conn.fetchval(
            "SELECT titles FROM practice_delivery_titles WHERE practice_id=$1 AND service_code=$2",
            practice_id,
            p["code"],
        )
        keys = [f"custom_{i}" for i in range(1, len(titles or []) + 1)]
    rows = await conn.fetch(
        f"""SELECT s.slot_key, COALESCE(s.waiver_reason,'') <> '' AS waived,
                   COALESCE(({_LIVE}) AND NULLIF(d.file_id,'') IS NOT NULL, false) AS live,
                   COALESCE(p.client_visible IS NOT FALSE AND {document_visibility_clause("d")}, false) AS accessible
            FROM practice_delivery_slots s JOIN practices p ON p.id = s.practice_id
            LEFT JOIN documents d ON d.id = s.document_id AND {_SCOPED}
            WHERE s.practice_id=$1 AND s.service_code=$2""",
        practice_id,
        p["code"],
    )
    saved = {row["slot_key"]: row for row in rows}
    slots = [saved.get(key) for key in keys]
    open_slots = [row for row in slots if not (row and row["waived"])]
    return DeliveryProgress(
        pending=not keys or any(row is None or not (row["live"] or row["waived"]) for row in slots),
        accessible_any=any(row and row["accessible"] for row in slots),
        accessible_all=all(row and row["accessible"] for row in open_slots),
    )
