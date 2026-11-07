// Coretax identity + access for the kita Tax tab — pure helpers.
//
// Coretax identifies a taxpayer by the 16-digit NPWP (for an individual it is
// the NIK itself). Access is not a database field: it is read from the
// client's documents — a `coretax_access` document means access is on file, an
// `efin` document means the EFIN is.

import { isLive, type LivenessFields } from "./documentValidity";

export interface CoretaxDocumentLike extends LivenessFields {
  document_type: string;
  issue_date?: string;
  created_at?: string;
}

/**
 * 16 bare digits are grouped 4-4-4-4. Anything else — an already-formatted
 * legacy 15-digit NPWP, a free-text tax id — is shown exactly as stored.
 */
export function formatNpwp(raw: string): string {
  const value = raw.trim();
  return /^\d{16}$/.test(value)
    ? value.replace(/(\d{4})(?=\d)/g, "$1 ")
    : value;
}

export function isSixteenDigits(raw: string | undefined | null): boolean {
  return !!raw && /^\d{16}$/.test(raw.trim());
}

export interface CoretaxStatus {
  coretaxAccess: { onFile: boolean; date: string | null };
  efin: { onFile: boolean; date: string | null };
}

function newest(docs: CoretaxDocumentLike[]): string | null {
  const dates = docs
    .map((d) => d.issue_date || d.created_at || "")
    .filter(Boolean)
    .sort();
  return dates.length ? dates[dates.length - 1] : null;
}

export function coretaxStatus(
  docs: readonly CoretaxDocumentLike[],
): CoretaxStatus {
  const access = docs.filter(
    (d) => d.document_type === "coretax_access" && isLive(d),
  );
  const efin = docs.filter((d) => d.document_type === "efin" && isLive(d));
  return {
    coretaxAccess: { onFile: access.length > 0, date: newest(access) },
    efin: { onFile: efin.length > 0, date: newest(efin) },
  };
}
