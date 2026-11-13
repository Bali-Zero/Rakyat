/**
 * The typed search text of the client, practice and company search bars travels in the query
 * string of a URL (`?search=`, `?q=`: names, e-mails, phones, tax ids). No parse of the pairs is
 * trusted to find it (`;`, `%3B`, `%26`, encoded keys, fragments), so anything that may print or
 * store a URL keeps its PATH only: the query string and the fragment are cut.
 */

// A "?" or "#" right after a path-like character starts a suffix that runs to the next
// whitespace, quote or backslash (the quote and backslash of a JSON-stringified context).
const URL_SUFFIX = /(?<=[\w/%.~-])[?#][^\s"'<>\\]*/g;

export function stripUrlQueries(text: string): string {
  return text.replace(URL_SUFFIX, "");
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
