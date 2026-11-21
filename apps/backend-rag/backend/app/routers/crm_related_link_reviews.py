"""A CRM admin confirms or rejects a proposed link of the related-subjects registry.

POST /api/crm/related-registry/link-reviews, on the prefix of the read router and with the same gate:
CRM admins only, behind HybridAuthMiddleware, never the machine key, not behind the registry flag.
The path is static: the subject id and the client id travel in the body, which this route parses
itself with closed 422 lists (a field name or a code outside the lists is reported as ``_`` or
``invalid``), so no input, message or invented name ever reaches the answer and the generic
exception handler is never reached. Exactly one application log line per request, carrying the
subject id and an outcome word; never the client id, the actor, the decision, the relation or a body.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.utils.crm_utils import is_crm_admin
from backend.services.crm import related_link_reviews

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crm/related-registry", tags=["crm-related-registry"])

MAX_BODY_BYTES = 65536
ACTOR_MIN = 3
ACTOR_MAX = 254

_REVIEW_LOC = frozenset({"subject_id", "client_id", "decision", "relation", "body"})
_REVIEW_CODE = frozenset(
    {
        "missing",
        "extra_forbidden",
        "int_type",
        "greater_than_equal",
        "literal_error",
        "json_invalid",
        "model_type",
        "relation_required",
        "relation_not_allowed",
        "too_long",
    }
)

Relation = Literal["self", "spouse", "child", "employee", "other"]


class RelatedRegistryReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subject_id: StrictInt = Field(ge=1)
    client_id: StrictInt = Field(ge=1)
    decision: Literal["confirmed", "rejected"]
    relation: Relation | None = None

    @model_validator(mode="after")
    def _relation_matches_decision(self) -> RelatedRegistryReviewRequest:
        if self.decision == "confirmed" and self.relation is None:
            raise PydanticCustomError("relation_required", "relation required")
        if self.decision == "rejected" and self.relation is not None:
            raise PydanticCustomError("relation_not_allowed", "relation not allowed")
        return self


class RelatedRegistryReviewOut(BaseModel):
    decision: Literal["confirmed", "rejected"]
    relation: Relation | None
    decided_by: str
    decided_at: str


class RelatedRegistryReviewAnswer(BaseModel):
    status: Literal["recorded", "unchanged"]
    review: RelatedRegistryReviewOut


def _fixed(status_code: int, detail: Any) -> Response:
    return Response(
        content=json.dumps({"detail": detail}).encode(),
        status_code=status_code,
        media_type="application/json",
    )


def _invalid_review(errors: list[dict[str, Any]]) -> Response:
    """422 body: field names and codes from fixed allowlists, never an input or a message."""
    items = []
    for err in errors[:50]:
        loc = [
            part
            if (isinstance(part, int) and not isinstance(part, bool)) or part in _REVIEW_LOC
            else "_"
            for part in err.get("loc", ())
        ]
        code = err.get("type")
        items.append({"loc": loc, "code": code if code in _REVIEW_CODE else "invalid"})
    return _fixed(422, items)


def _actor(user: dict[str, Any]) -> str | None:
    email = (user.get("email") or "").strip().lower() if user else ""
    return email if ACTOR_MIN <= len(email) <= ACTOR_MAX else None


def _log(subject: int | str, outcome: str, error: str | None = None) -> None:
    if error is None:
        logger.info("related link review: subject=%s outcome=%s", subject, outcome)
    else:
        logger.warning(
            "related link review: subject=%s outcome=%s error=%s", subject, outcome, error
        )


_ANSWERS = {
    "subject_not_found": 404,
    "subject_is_merged": 409,
    "link_not_found": 404,
}


@router.post("/link-reviews", response_model=RelatedRegistryReviewAnswer)
async def review_registry_link(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Record a human decision on one proposed link (CRM admins only). Answers carry closed words."""
    actor = _actor(user)
    if not is_crm_admin(user) or actor is None:
        _log("-", "forbidden")
        return _fixed(403, "CRM admin required")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        _log("-", "invalid")
        return _invalid_review([{"loc": ("body",), "type": "too_long"}])
    try:
        payload = RelatedRegistryReviewRequest.model_validate_json(raw)
    except ValidationError as exc:
        _log("-", "invalid")
        return _invalid_review(
            exc.errors(include_input=False, include_context=False, include_url=False)
        )
    try:
        async with pool.acquire() as conn:
            result = await related_link_reviews.record_review(
                conn,
                subject_id=payload.subject_id,
                client_id=payload.client_id,
                decision=payload.decision,
                relation=payload.relation,
                decided_by=actor,
            )
    except (asyncpg.PostgresError, OSError, asyncpg.InterfaceError) as exc:
        _log(payload.subject_id, "unavailable", type(exc).__name__)
        return _fixed(503, "registry_unavailable")
    outcome = result["outcome"]
    _log(payload.subject_id, outcome)
    if outcome in _ANSWERS:
        return _fixed(_ANSWERS[outcome], outcome)
    review = {**result["review"], "decided_at": result["review"]["decided_at"].isoformat()}
    return Response(
        content=json.dumps({"status": outcome, "review": review}).encode(),
        status_code=200,
        media_type="application/json",
    )
