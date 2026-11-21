import type { Client, ClientProfile } from "@/lib/api/crm/crm.types";

const ADMIN_ROLES = new Set(["admin", "board member", "ceo", "founder"]);

/** Maximum binary upload size for multipart tax document endpoints (Vercel ~4.5 MB cap). */
export const TAX_DOCUMENT_MAX_BYTES = 4 * 1024 * 1024;
export const TAX_DOCUMENT_MAX_LABEL = "4 MB";

/**
 * Whether the desk offers upload, edit, and delete controls in the Tax tab's
 * Tax Documents section.
 *
 * When the backend profile carries `viewer_can_manage_tax_documents` it is
 * final (it accounts for `created_by`, validated client pools, and extra CRM
 * admins). When absent (older backend during a staggered deploy) this falls
 * back to CRM admin roles and the visible team associations (`assigned_to` and
 * `tax_consultant`). The backend endpoints remain the authoritative fence.
 */
export function viewerCanManageTaxDocuments(
  profile:
    Pick<ClientProfile, "viewer_can_manage_tax_documents"> | null | undefined,
  client: Pick<Client, "assigned_to" | "tax_consultant"> | null | undefined,
  currentUserEmail?: string | null,
  currentUserRole?: string | null,
): boolean {
  if (typeof profile?.viewer_can_manage_tax_documents === "boolean") {
    return profile.viewer_can_manage_tax_documents;
  }
  const role = (currentUserRole ?? "").trim().toLowerCase();
  if (ADMIN_ROLES.has(role)) return true;
  const email = (currentUserEmail ?? "").trim().toLowerCase();
  if (!email || !client) return false;
  const assigned = (client.assigned_to ?? "").trim().toLowerCase();
  const taxConsultant = (client.tax_consultant ?? "").trim().toLowerCase();
  return email === assigned || email === taxConsultant;
}
