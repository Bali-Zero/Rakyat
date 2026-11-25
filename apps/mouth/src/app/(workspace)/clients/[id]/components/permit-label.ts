// Display label for a permit, e.g. "E28A · Investor KITAS".
// The taxonomy keeps qualifiers such as "(Standard)"; staff do not need them.
export function cleanPermitLabel(label: string | null | undefined): string {
  if (!label) return "";
  return label
    .replace(/\s*\([^)]*\)/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/**
 * A document's server-resolved permit (`permit_label`, from OCR or a precise
 * `document_type`) as `<code> · <name>`; "" when the backend resolved none.
 * Never falls back to the raw `document_type`: a file with nothing extracted
 * is not a permit type. Shared by the Immigration panel and the Family rows.
 */
export function documentPermitLabel(doc: {
  permit_code?: string | null;
  permit_label?: string | null;
}): string {
  if (!doc.permit_label) return "";
  // A label that leads with its own visa index ("E23 — Working KITAS") reads
  // "E23 · Working KITAS" even when the backend sent no separate code.
  const code =
    doc.permit_code ||
    doc.permit_label.match(/^([A-Z][A-Z0-9]{1,7})\s*[—–-]\s/)?.[1];
  return permitDisplay(code, doc.permit_label);
}

export function permitDisplay(
  code: string | null | undefined,
  label: string | null | undefined,
): string {
  const clean = cleanPermitLabel(label);
  const c = (code ?? "").trim();
  if (!c) return clean;
  if (clean.toLowerCase() === c.toLowerCase()) return c;
  const escaped = c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const prefix = new RegExp(`^${escaped}\\s*[—–·-]\\s*`, "i");
  const rest = clean.replace(prefix, "").trim();
  return rest ? `${c} · ${rest}` : c;
}
