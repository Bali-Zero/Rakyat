/**
 * Who is OFFERED "Add company" on the client desk.
 *
 * Mirrors the backend fence — `can_create_company` in
 * `apps/backend-rag/backend/app/utils/crm_utils.py` (CRM admin roles plus
 * `COMPANY_CREATE_ROLES`, i.e. the tax team). The backend stays the real
 * gate: it 403s anyone else and writes nothing. Edit and delete of a company
 * stay admin-only there. Like `document-visibility.ts`, this is narrower
 * than the fence on purpose: the backend also admits CRM admins by email
 * allow-list, which the desk cannot see, so such a viewer simply gets no
 * button here (fail closed, never a false offer).
 */
const COMPANY_CREATE_ROLES: ReadonlySet<string> = new Set([
  // is_crm_admin role branch
  "admin",
  "board member",
  "ceo",
  "founder",
  // tax team
  "tax consultant",
  "tax lead",
  "tax supervisor",
  "tax responsible",
]);

export function viewerCanCreateCompany(
  viewerRole: string | null | undefined,
): boolean {
  return COMPANY_CREATE_ROLES.has((viewerRole ?? "").trim().toLowerCase());
}
