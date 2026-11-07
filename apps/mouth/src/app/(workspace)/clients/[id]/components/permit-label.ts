// Display label for a permit, e.g. "E28A · Investor KITAS".
// The taxonomy keeps qualifiers such as "(Standard)"; staff do not need them.
export function cleanPermitLabel(label: string | null | undefined): string {
  if (!label) return "";
  return label
    .replace(/\s*\([^)]*\)/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
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
