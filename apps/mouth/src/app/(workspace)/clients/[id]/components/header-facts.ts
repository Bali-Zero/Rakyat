import type {
  ClientCompanyLink,
  ClientDocument,
} from "@/lib/api/crm/crm.types";
import { humanize } from "./overview-brief";
import { pickCurrentVisa } from "./pickCurrentVisa";
import { permitDisplay } from "./permit-label";

/**
 * Pure derivations behind the client header: who the client is in their PT and
 * which permit they hold now. Every value is a field the profile already
 * carries; a missing field yields null so the header omits the line instead of
 * guessing.
 */

/** "Director · PT Contoh Abadi" from the client's company links, or null. */
export function roleLine(
  links: ClientCompanyLink[] | undefined | null,
): string | null {
  const withRole = (links ?? []).filter(
    (l) => l.role?.trim() && l.company_name?.trim(),
  );
  if (withRole.length === 0) return null;
  const link = withRole.find((l) => l.is_primary) ?? withRole[0];
  return `${humanize(link.role)} · ${link.company_name.trim()}`;
}

/**
 * "E28A · Investor KITAS" from the current permit document, or null when the
 * client holds none. The popular name is `permit_label` as the backend
 * resolved it, cleaned of "(Standard)"-style qualifiers (permitDisplay), falling
 * back to the family label and finally to the raw document type.
 */
export function permitLine(documents: ClientDocument[]): string | null {
  const doc = pickCurrentVisa(documents);
  if (!doc) return null;
  return (
    permitDisplay(
      doc.permit_code,
      doc.permit_label ||
        doc.permit_family_label ||
        humanize(doc.document_type),
    ) || null
  );
}

const capitalise = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/**
 * The first name of a team member: the first word of their display name, else
 * the first segment of the email's local part ("dewaayu.tax@x" -> "Dewaayu").
 * Empty string when neither exists.
 */
export function firstName(
  displayName: string | undefined | null,
  email: string | undefined | null,
): string {
  const word = displayName?.trim().split(/\s+/)[0];
  if (word && !word.includes("@")) return word;
  const local = (email ?? displayName ?? "").split("@")[0];
  const segment = local.split(/[._\-+\s]/).find(Boolean) ?? "";
  return capitalise(segment);
}

/** Label for a stored consultant that is no longer offered: "legacy.tax@x" -> "Legacy". */
export function legacyConsultantLabel(value: string): string {
  const local = value.split("@")[0].replace(/\.tax$/, "");
  return local ? local.charAt(0).toUpperCase() + local.slice(1) : value;
}
