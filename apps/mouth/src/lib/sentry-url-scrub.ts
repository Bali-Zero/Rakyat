/**
 * Structured cleaning of Sentry events and breadcrumbs for the mouth app (client, server, edge).
 *
 * The typed search text of the client, practice and company search bars travels in the query
 * string of a URL. Same rules as the backend (`sentry_config.py`), applied BY FIELD and never to
 * free text: query-string fields are replaced, URL-valued fields keep their path only, and the
 * request headers that carry a URL as an opaque string (Referer, Link, Location) are dropped.
 * Messages, exception values and stack frames are not touched.
 */

const PLACEHOLDER = "[REDACTED]";
const QUERY_FIELDS = new Set([
  "query_string",
  "QUERY_STRING",
  "http.query",
  "http.fragment",
  "url.query",
  "url.fragment",
]);
const URL_FIELDS = new Set(["url", "http.url", "url.full"]);
const URL_HEADERS = new Set(["referer", "referrer", "link", "location"]);
/**
 * A navigation breadcrumb (`category: "navigation"`) carries the two addresses
 * of a history change as `data.from` and `data.to`, path AND query, and the SDK
 * filters neither. `/clients?q=<typed text>` from the command palette and an
 * old `/login?email=…&pin=…` rewritten on load would both reach the next event.
 */
const NAVIGATION_URL_FIELDS = new Set(["from", "to"]);
const SEARCH_PARAM_KEYS = new Set(["search", "q"]);
const MAX_DEPTH = 12;

function isSearchParamKey(key: string): boolean {
  try {
    return SEARCH_PARAM_KEYS.has(
      decodeURIComponent(key.replace(/\+/g, " ")).trim().toLowerCase(),
    );
  } catch {
    return SEARCH_PARAM_KEYS.has(key.trim().toLowerCase());
  }
}

function pathOnlyUrl(value: string): string {
  try {
    const parsed = new URL(value, "http://local.invalid");
    if (!parsed.search && !parsed.hash) return value;
    return /^[a-z][a-z0-9+.-]*:/i.test(value)
      ? `${parsed.protocol}//${parsed.host}${parsed.pathname}`
      : parsed.pathname;
  } catch {
    return PLACEHOLDER;
  }
}

function scrubHeaders(headers: unknown): unknown {
  if (Array.isArray(headers)) {
    return headers.filter(
      (pair) =>
        !(
          Array.isArray(pair) &&
          typeof pair[0] === "string" &&
          URL_HEADERS.has(pair[0].toLowerCase())
        ),
    );
  }
  if (headers && typeof headers === "object") {
    return Object.fromEntries(
      Object.entries(headers as Record<string, unknown>).filter(
        ([name]) => !URL_HEADERS.has(name.toLowerCase()),
      ),
    );
  }
  return headers;
}

function scrub(value: unknown, depth: number): unknown {
  if (depth > MAX_DEPTH) return value;
  if (Array.isArray(value)) return value.map((v) => scrub(v, depth + 1));
  if (!value || typeof value !== "object") return value;
  const source = value as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const [key, v] of Object.entries(source)) {
    if (isSearchParamKey(key) && typeof v === "string") {
      out[key] = PLACEHOLDER;
    } else if (QUERY_FIELDS.has(key) && typeof v === "string") {
      out[key] = PLACEHOLDER;
    } else if (
      key === "data" &&
      source.category === "navigation" &&
      v &&
      typeof v === "object" &&
      !Array.isArray(v)
    ) {
      out[key] = Object.fromEntries(
        Object.entries(v as Record<string, unknown>).map(([field, x]) => [
          field,
          NAVIGATION_URL_FIELDS.has(field) && typeof x === "string"
            ? pathOnlyUrl(x)
            : scrub(x, depth + 1),
        ]),
      );
    } else if (key.toLowerCase() === "headers") {
      out[key] = scrubHeaders(v);
    } else if (URL_FIELDS.has(key) && typeof v === "string") {
      out[key] = pathOnlyUrl(v);
    } else if (
      key === "description" &&
      typeof v === "string" &&
      typeof source.op === "string" &&
      source.op.startsWith("http")
    ) {
      const space = v.indexOf(" ");
      out[key] =
        space < 0
          ? pathOnlyUrl(v)
          : `${v.slice(0, space)} ${pathOnlyUrl(v.slice(space + 1))}`;
    } else if (key === "name" && typeof v === "string") {
      const attrs = source.attributes;
      const op =
        attrs && typeof attrs === "object" && !Array.isArray(attrs)
          ? (attrs as Record<string, unknown>)["sentry.op"]
          : undefined;
      if (typeof op === "string" && op.startsWith("http")) {
        const space = v.indexOf(" ");
        out[key] =
          space < 0
            ? pathOnlyUrl(v)
            : `${v.slice(0, space)} ${pathOnlyUrl(v.slice(space + 1))}`;
      } else {
        out[key] = scrub(v, depth + 1);
      }
    } else {
      out[key] = scrub(v, depth + 1);
    }
  }
  return out;
}

/** Clean an event or a breadcrumb in place of returning it unchanged. */
export function scrubSentryUrls<T>(item: T): T {
  try {
    return scrub(item, 0) as T;
  } catch {
    return item;
  }
}
