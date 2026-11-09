import type { ClientDocument, FamilyMember } from "@/lib/api/crm/crm.types";
import {
  dateValidity,
  isEnumUrgent,
  isLive,
  parseCalendarDate,
  worseLevel,
  type AlertLevel,
  type DateValidity,
} from "./documentValidity";
import { pickCurrentPermitFor } from "./pickCurrentVisa";

/**
 * One shared result for a family member's permit and passport. The FamilyTab
 * rows and the Overview family rows both render from it, so the two surfaces
 * cannot give different answers for the same person.
 */

/** Where the date came from: the member's own field, a live document, or nowhere. */
export type FamilySource = "field" | "document" | "none";

export interface FamilyDate {
  expiry: string | null;
  source: FamilySource;
  validity: DateValidity | null;
  /** The worse of the enum and the model (field), the model alone (document). */
  level: AlertLevel | null;
  urgent: boolean;
}

/** Days under which a passport is urgent when no enum can be read. */
export const PASSPORT_FALLBACK_WINDOW_DAYS = 180;

const isPassportDocument = (doc: ClientDocument): boolean =>
  Boolean(doc.document_type?.toLowerCase().includes("passport"));

/** The newest live passport document of a member (upload time, then id). */
export function pickMemberPassport(
  documents: readonly ClientDocument[],
  memberId: number,
): ClientDocument | undefined {
  const at = (doc: ClientDocument) =>
    doc.created_at ? Date.parse(doc.created_at) || 0 : 0;
  return documents
    .filter(
      (doc) =>
        doc.family_member_id === memberId &&
        isLive(doc) &&
        isPassportDocument(doc),
    )
    .reduce<ClientDocument | undefined>((best, doc) => {
      if (!best) return doc;
      const diff = at(doc) - at(best);
      return diff > 0 || (diff === 0 && doc.id > best.id) ? doc : best;
    }, undefined);
}

function resolve(
  field: string | undefined,
  doc: ClientDocument | undefined,
): { expiry: string | null; source: FamilySource } {
  const own = parseCalendarDate(field);
  if (own) return { expiry: own, source: "field" };
  const held = parseCalendarDate(doc?.expiry_date);
  if (held) return { expiry: held, source: "document" };
  return { expiry: null, source: "none" };
}

export function familyPermit(
  member: FamilyMember,
  documents: readonly ClientDocument[],
  day: string,
): FamilyDate {
  const doc = pickCurrentPermitFor([...documents], member.id);
  const { expiry, source } = resolve(member.visa_expiry, doc);
  if (!expiry) {
    return { expiry, source, validity: null, level: null, urgent: false };
  }
  const validity = dateValidity(expiry, day);
  const alert = source === "field" ? member.visa_alert : null;
  return {
    expiry,
    source,
    validity,
    level: worseLevel(alert, validity.state),
    urgent:
      validity.state === "expired" ||
      validity.state === "expiring" ||
      isEnumUrgent(alert),
  };
}

export function familyPassport(
  member: FamilyMember,
  documents: readonly ClientDocument[],
  day: string,
): FamilyDate {
  const doc = pickMemberPassport(documents, member.id);
  const { expiry, source } = resolve(member.passport_expiry, doc);
  if (!expiry) {
    return { expiry, source, validity: null, level: null, urgent: false };
  }
  const validity = dateValidity(expiry, day);
  const alert = source === "field" ? (member.passport_alert ?? null) : null;
  const dated =
    validity.daysLeft !== null &&
    validity.daysLeft <= PASSPORT_FALLBACK_WINDOW_DAYS;
  return {
    expiry,
    source,
    validity,
    level: worseLevel(alert, validity.state),
    urgent:
      validity.state === "expired" ||
      validity.state === "expiring" ||
      (alert ? isEnumUrgent(alert) : dated),
  };
}

/** The visa pill for a level: the old file's exact words. */
export function levelPill(level: AlertLevel): {
  tone: "ok" | "wait";
  label: string;
} {
  if (level === "green") return { tone: "ok", label: "Valid" };
  if (level === "expired") return { tone: "wait", label: "Expired" };
  if (level === "red") return { tone: "wait", label: "Expiring soon" };
  return { tone: "wait", label: "Renewal recommended" };
}
