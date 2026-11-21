/**
 * Related-subjects registry (read-only, CRM admins) — typed wrapper.
 *
 * Backend: apps/backend-rag/backend/app/routers/crm_related_registry_read.py.
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
export type RegistryLink = Omit<
  components["schemas"]["RelatedRegistryLink"],
  "files"
> & { files?: number | null };
// The detachment lists and the file count are optional here: the page must
// render when an older backend omits them.
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
