/**
 * A staff member's WhatsApp number on the HR card (DAI-153).
 *
 * The browser only ever shows the number masked (`62•••••1234`): the backend sends
 * `whatsapp_masked` to the HR writers alone, and `maskWhatsapp` masks again whatever it is
 * handed, so a full number can never reach the screen through this path. `checkWhatsapp`
 * mirrors the backend rule (hr_card_edit.normalise_whatsapp): drop spaces, dots, dashes,
 * brackets and a leading `+`, then `62` + 8 to 13 digits (10 to 15 in all). Empty means
 * "unchanged", never a delete. No message ever contains the value.
 */

const SEPARATORS = /[ .\-()]/g;
const SHAPE = /^62[0-9]{8,13}$/;
const DOT = "•";
export const WHATSAPP_MESSAGE = "Must start with 62 and have 10 to 15 digits";

export type WhatsappCheck =
  { ok: true; value: string | null } | { ok: false; message: string };

/** The stored form of a typed number; `value: null` = empty, leave the stored one unchanged. */
export function checkWhatsapp(input: string): WhatsappCheck {
  let text = input.trim().replace(SEPARATORS, "");
  if (text.startsWith("+")) text = text.slice(1);
  if (!text) return { ok: true, value: null };
  return SHAPE.test(text)
    ? { ok: true, value: text }
    : { ok: false, message: WHATSAPP_MESSAGE };
}

/** `62•••••1234`, or null when there is nothing safe to show. Idempotent on a masked value. */
export function maskWhatsapp(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const masked = new RegExp(`^([0-9]{2})${DOT}+([0-9]{4})$`).exec(value.trim());
  if (masked) return `${masked[1]}${DOT.repeat(5)}${masked[2]}`;
  const digits = value.replace(/[^0-9]/g, "");
  if (digits.length < 8) return null;
  return `${digits.slice(0, 2)}${DOT.repeat(5)}${digits.slice(-4)}`;
}
