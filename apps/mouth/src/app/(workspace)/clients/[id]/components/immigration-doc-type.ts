/**
 * Whether a document's free-text `document_type` names an immigration
 * document. Shared by the Immigration tab (what it lists) and the Documents
 * tab (what it leaves out), so the two cannot disagree.
 *
 * "visa" and "permit" are matched as words, not substrings: a plain
 * `includes("permit")` took building / business / environmental permits, and
 * `includes("visa")` took the Indonesian "devisa" (foreign exchange). A
 * permit counts only with an immigration qualifier (stay, work, exit,
 * re-entry, residence). `document_category === "immigration"` still wins on
 * its own at each call site.
 */
const IMMIGRATION_SUBSTRINGS = ["kitas", "kitap", "imta", "rptka", "voa"];
const VISA_WORD = /(^|[^a-z])e?[-_ ]?visas?([^a-z]|$)/;
const IMMIGRATION_PERMIT =
  /(^|[^a-z])(stay|work|working|exit|re[-_ ]?entry|residence|residency|limited[-_ ]stay)[-_ ]*permits?([^a-z]|$)/;

export function isImmigrationDocType(
  documentType: string | null | undefined,
): boolean {
  const t = documentType?.toLowerCase() ?? "";
  if (!t) return false;
  return (
    IMMIGRATION_SUBSTRINGS.some((k) => t.includes(k)) ||
    VISA_WORD.test(t) ||
    IMMIGRATION_PERMIT.test(t)
  );
}
