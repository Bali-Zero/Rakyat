/**
 * CRM admin check (frontend, UI-gating only).
 *
 * Loosely mirrors the backend: the global admin allowlist + role==='admin'
 * (apps/backend-rag/backend/app/utils/crm_utils.py::is_crm_admin).
 *
 * This is used ONLY to hide/show UI affordances (the Accounting command-palette
 * shortcut, admin-only buttons). Real authorization happens server-side on every
 * /api/crm/accounting/* endpoint via is_crm_admin() → 403 otherwise, so a
 * misconfigured frontend can never grant access — at worst it hides a control
 * from a real admin.
 */

import { GLOBAL_ADMIN_EMAILS } from "@/lib/auth/owner";

// The CRM-only extra admins are NOT listed here: this module is imported by a
// `"use client"` component, so any list in it ships to the browser. The backend
// (`CRM_EXTRA_ADMIN_EMAILS_JSON`) owns that list and authorizes every call; this check
// only decides which UI controls to show.

const CRM_ADMIN_EMAILS: ReadonlySet<string> = new Set([...GLOBAL_ADMIN_EMAILS]);

export function isCRMAdmin(
  profile: { email?: string; role?: string } | null | undefined,
): boolean {
  if (!profile) return false;
  const email = profile.email?.toLowerCase().trim() ?? "";
  if (CRM_ADMIN_EMAILS.has(email)) return true;
  return profile.role === "admin";
}
