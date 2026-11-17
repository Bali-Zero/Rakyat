/**
 * Exact display of a backend money string ("361900825.55"), with no float in
 * between: whole rupiah grouped with ".", cents after "," only when not "00".
 * A string that is not in that shape is returned as it came.
 */
export function formatStatementAmount(raw: string): string {
  const m = /^(-?)(\d+)\.(\d{2})$/.exec(raw);
  if (!m) return raw;
  const [, sign, whole, cents] = m;
  const grouped = whole
    .replace(/^0+(?=\d)/, "")
    .replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const isZero = /^0*$/.test(whole) && cents === "00";
  return `${isZero ? "" : sign}Rp ${grouped}${cents === "00" ? "" : `,${cents}`}`;
}
