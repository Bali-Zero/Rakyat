/**
 * WhatsApp number normalisation for the portal notification forms.
 *
 * `PUT /api/portal/notifications/prefs` stores `wa_phone` as E.164 digits
 * WITHOUT the leading "+" and rejects anything else (`_E164_RE` in
 * `portal_notification_prefs.py`). Clients type numbers the way they write
 * them — "+62 890-0000-0000", "0890 0000 0000" — and the profile page keeps
 * them that way. This helper turns those into the stored form so the browser
 * never sends a number the backend would refuse. The API contract itself is
 * unchanged.
 */

/** E.164 digits, no "+", first digit 1-9 — mirrors the backend `_E164_RE`. */
export const E164_NO_PLUS = /^[1-9]\d{6,14}$/;

/**
 * Normalise a typed WhatsApp number to E.164 digits without "+".
 *
 * - strips spaces, dashes, dots and parentheses;
 * - drops ONE leading "+";
 * - otherwise a single leading national "0" becomes "62" (Indonesia).
 *
 * Returns `null` when the result is still not a valid E.164 number (letters,
 * "++", "00…", too short/long), so callers keep showing their error.
 */
export function normalizeWaPhone(
  raw: string | null | undefined,
): string | null {
  if (!raw) return null;
  let digits = raw.replace(/[\s\-.()]/g, "");
  if (digits.startsWith("+")) {
    digits = digits.slice(1);
  } else if (/^0[1-9]/.test(digits)) {
    digits = `62${digits.slice(1)}`;
  }
  return E164_NO_PLUS.test(digits) ? digits : null;
}
