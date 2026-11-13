/**
 * The typed search text of the client, practice and company search bars travels in the `search`
 * and `q` query params (names, e-mails, phones, tax ids). Any text that may carry a URL (log
 * lines, error messages, referrers) goes through this before it is printed or stored.
 * Keys are matched URL-decoded and case-insensitively, like the backend access-log filter.
 */
const PARAM = /([?&;])([^=&#\s"'<>\\]{1,60})=([^&#;\s"'<>\\]*)/g;
const SEARCH_KEYS = new Set(["search", "q"]);

export function redactSearchParams(text: string): string {
  return text.replace(PARAM, (match, sep: string, key: string) => {
    let decoded = key;
    try {
      decoded = decodeURIComponent(key.replace(/\+/g, " "));
    } catch {
      // undecodable key: compare it as typed
    }
    return SEARCH_KEYS.has(decoded.trim().toLowerCase())
      ? `${sep}${key}=[REDACTED]`
      : match;
  });
}

/** The path of a URL or referrer, never its query or fragment; null when it is not a URL. */
export function pathOnly(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    return new URL(raw, "http://local.invalid").pathname;
  } catch {
    return null;
  }
}
