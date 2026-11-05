import type { ClientDocument } from "@/lib/api/crm/crm.types";
import { isRejectedDocument } from "./utils";

// The ONE rule for "which document is a person's current permit". The
// Overview card (`pickCurrentVisa`) and the Immigration tab (which badges the
// current permit and shows no countdown on anything older) both read it, so
// the two surfaces cannot name different permits again.
const VISA_TYPE_MARKERS = [
  "visa", // also "e-visa" / "evisa"
  "kitas",
  "kitap",
  "voa", // also "e-voa" / "evoa"
  "itk", // a stay permit (visit stay) like the KITAS family
];

// A MERP (re-entry permit) rides along with the current permit; it is never
// the permit itself, even when its stored document_type is a generic "visa".
const isMerp = (doc: ClientDocument): boolean =>
  doc.permit_family === "merp" ||
  (doc.document_type?.toLowerCase().includes("merp") ?? false);

const isVisaDocument = (doc: ClientDocument): boolean =>
  // An absent category is accepted: the Immigration tab's own filter already
  // keyed on the document type, and old rows carry no category.
  (!doc.document_category || doc.document_category === "immigration") &&
  !isMerp(doc) &&
  VISA_TYPE_MARKERS.some((marker) =>
    doc.document_type?.toLowerCase().includes(marker),
  );

const typeOf = (doc: ClientDocument): string =>
  `${doc.document_type ?? ""} ${doc.permit_family ?? ""}`.toLowerCase();

/** A stay-permit document (the card itself): KITAS/ITAS, KITAP/ITAP, ITK. */
export const isStayPermitDocument = (doc: ClientDocument): boolean =>
  /kitas|itas|kitap|itap|\bitk\b|stay/.test(typeOf(doc));

/**
 * Two documents of the SAME permit: same holder and the same permit number
 * when both carry one, otherwise the same permit code (or document type) and
 * the same expiry date. A re-upload or an e-Visa of the same KITAS is a copy
 * of the permit, not another permit.
 */
export function isSamePermit(a: ClientDocument, b: ClientDocument): boolean {
  if (a.id === b.id) return false;
  if ((a.family_member_id ?? null) !== (b.family_member_id ?? null))
    return false;
  if (a.permit_number && b.permit_number)
    return a.permit_number === b.permit_number;
  if (!a.expiry_date || a.expiry_date !== b.expiry_date) return false;
  const kind = (d: ClientDocument) =>
    (d.permit_code || d.document_type || "").trim().toLowerCase();
  return kind(a) === kind(b);
}

const toTime = (value?: string | null): number =>
  value ? Date.parse(value) || 0 : 0;

// The client's current visa/KITAS for the Overview card.
//
// Candidates: the client's own immigration visa/KITAS/KITAP documents (no
// family member), not removed by the client in the portal (deleted_at) and
// not archived by the team, and not rejected.
//
// Among dated documents the farthest expiry wins, as before. A document with
// no expiry_date yet wins only when it was uploaded AFTER that one: a new
// KITAS whose dates are not filled in is the current permit, and selecting it
// lets the card's auto-OCR fill its dates. Before this, an undated document
// always sorted last, so a new KITAS stayed hidden behind the old visa and was
// never sent to OCR (client 10247, 6 Oct).
export const pickCurrentVisa = (
  documents: ClientDocument[],
): ClientDocument | undefined => pickCurrentPermitFor(documents, null);

// True when the client has no current permit but does hold a rejected one, so
// the card says so instead of claiming nothing is on file.
export const hasOnlyRejectedPermit = (documents: ClientDocument[]): boolean =>
  !pickCurrentVisa(documents) &&
  documents.some(
    (doc) =>
      isVisaDocument(doc) &&
      !doc.family_member_id &&
      !doc.deleted_at &&
      !doc.is_archived &&
      isRejectedDocument(doc),
  );

/**
 * The current permit of ONE holder: the client themselves (`null`) or a
 * family member (`family_member_id`). Same candidates and tie-breaks as
 * `pickCurrentVisa`, which is this function for the client.
 */
export const pickCurrentPermitFor = (
  documents: ClientDocument[],
  familyMemberId: number | null,
): ClientDocument | undefined => {
  const candidates = documents.filter(
    (doc) =>
      isVisaDocument(doc) &&
      (doc.family_member_id ?? null) === familyMemberId &&
      !doc.deleted_at &&
      !doc.is_archived &&
      !isRejectedDocument(doc),
  );

  let dated: ClientDocument | undefined;
  let undated: ClientDocument | undefined;
  for (const doc of candidates) {
    if (doc.expiry_date) {
      const diff = dated
        ? toTime(doc.expiry_date) - toTime(dated.expiry_date)
        : 1;
      if (diff > 0 || (diff === 0 && dated && doc.id > dated.id)) dated = doc;
    } else {
      const diff = undated
        ? toTime(doc.created_at) - toTime(undated.created_at)
        : 1;
      if (diff > 0 || (diff === 0 && undated && doc.id > undated.id))
        undated = doc;
    }
  }

  const winner =
    !dated || (undated && toTime(undated.created_at) > toTime(dated.created_at))
      ? undated
      : dated;
  if (!winner || isStayPermitDocument(winner)) return winner;

  // Among documents of the same permit the stay-permit document is the permit;
  // an e-Visa (entry visa) is only a copy of it. Then the latest issue date,
  // upload date and id.
  const stay = candidates
    .filter((d) => isStayPermitDocument(d) && isSamePermit(d, winner))
    .sort(
      (x, y) =>
        toTime(y.issue_date) - toTime(x.issue_date) ||
        toTime(y.created_at) - toTime(x.created_at) ||
        y.id - x.id,
    );
  return stay[0] ?? winner;
};
