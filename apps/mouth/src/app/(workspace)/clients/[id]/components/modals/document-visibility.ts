/**
 * Who may flip a document's `client_visible` from the desk.
 *
 * Mirrors the backend fence — `can_change_document_visibility` in
 * `apps/backend-rag/backend/app/utils/crm_utils.py` (CRM admin roles plus
 * `DOCUMENT_VISIBILITY_ROLES`). The backend stays the real gate: it 403s
 * anyone else and writes nothing. This predicate only decides whether to
 * OFFER the toggle. It is narrower than the fence on purpose: the backend
 * also admits CRM admins by email allow-list, which the desk cannot see, so
 * such a viewer simply gets no toggle here (fail closed, never a false offer).
 */
const DOCUMENT_VISIBILITY_ROLES: ReadonlySet<string> = new Set([
  // is_crm_admin role branch
  "admin",
  "board member",
  "ceo",
  "founder",
  // DOCUMENT_VISIBILITY_ROLES
  "team leader",
  "tax lead",
  "supervisor",
  // reorganisation 2026-11-09 titles (backend DOCUMENT_VISIBILITY_ROLES)
  "direktur",
  "tax supervisor",
  "tax responsible",
  // owner order 2026-11-15: same access as Tax Lead
  "tax consultant",
]);

export function viewerCanChangeDocumentVisibility(
  viewerRole: string | null | undefined,
): boolean {
  return DOCUMENT_VISIBILITY_ROLES.has((viewerRole ?? "").trim().toLowerCase());
}
