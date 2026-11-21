/**
 * Related-subjects registry (CRM admins) — typed wrapper.
 *
 * Backend: apps/backend-rag/backend/app/routers/crm_related_registry_read.py
 * (reads) and crm_related_link_reviews.py (the one write: a review decision,
 * POSTed to a static path with the ids in the body and no query string).
 * The search text travels only as the query key `q`; the link filter is the
 * closed value `linked=true|false`; detail paths are built from numeric
 * subject ids and nothing else.
 */

import { api } from "@/lib/api";
import type { components } from "@/lib/api/schema";

export type RegistryKind = "person" | "company";
export type RegistryList = components["schemas"]["RelatedRegistryListOut"];
export type RegistryRow = components["schemas"]["RelatedRegistrySubjectRow"];
export type RegistryDetachment =
  components["schemas"]["RelatedRegistryDetachment"];
export type RegistrySuggestionBasis =
  | "passport_equal"
  | "passport_one_char_apart"
  | "nik_encodes_client_birth_date"
  | "birth_date_equal";
export type RegistryLink = Omit<
  components["schemas"]["RelatedRegistryLink"],
  "files" | "review" | "suggested_relation" | "suggestion_basis"
> & {
  files?: number | null;
  review?: components["schemas"]["RelatedRegistryLinkReview"] | null;
  suggested_relation?: "self" | null;
  suggestion_basis?: RegistrySuggestionBasis[];
};
// The detachment lists, the file count and the review fields are optional
// here: the page must render when an older backend omits them.
export type RegistryDetail = Omit<
  components["schemas"]["RelatedRegistrySubjectDetail"],
  "links" | "detached_into" | "detached_from"
> & {
  links: RegistryLink[];
  detached_into?: RegistryDetachment[];
  detached_from?: RegistryDetachment[];
};
export type RegistryDocument = components["schemas"]["RelatedRegistryDocument"];
export type RegistryIdentifier =
  components["schemas"]["RelatedRegistryIdentifier"];
export type RegistryMerged = components["schemas"]["RelatedRegistryMerged"];
export type RegistryLinkReview =
  components["schemas"]["RelatedRegistryLinkReview"];
export type RegistryReviewRelation =
  "self" | "spouse" | "child" | "employee" | "other";
// The route parses its own body, so the request is not in the OpenAPI document.
export type RegistryReviewRequest =
  | {
      subject_id: number;
      client_id: number;
      decision: "confirmed";
      relation: RegistryReviewRelation;
    }
  | { subject_id: number; client_id: number; decision: "rejected" };
export type RegistryReviewAnswer =
  components["schemas"]["RelatedRegistryReviewAnswer"];
export type RegistryLinked = "true" | "false";

export interface RegistryListParams {
  q?: string;
  kind?: RegistryKind;
  linked?: RegistryLinked;
  limit?: number;
  offset?: number;
}

const BASE = "/api/crm/related-registry";

export const listRegistry = (params: RegistryListParams = {}) => {
  const query = new URLSearchParams();
  if (params.q) query.set("q", params.q);
  if (params.kind) query.set("kind", params.kind);
  if (params.linked) query.set("linked", params.linked);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const text = query.toString();
  return api.get<RegistryList>(text ? `${BASE}?${text}` : BASE);
};

export const getRegistrySubject = (subjectId: number) =>
  api.get<RegistryDetail>(`${BASE}/${Math.trunc(subjectId)}`);

export const reviewRegistryLink = (body: RegistryReviewRequest) =>
  api.post<RegistryReviewAnswer>(`${BASE}/link-reviews`, body);
