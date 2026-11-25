/**
 * Structured cleaning of Sentry events and breadcrumbs for the mouth app (client, server, edge).
 *
 * The typed search text of the client, practice and company search bars travels in the query
 * string of a URL. Same rules as the backend (`sentry_config.py`), applied BY FIELD and never to
 * free text: query-string fields are replaced, URL-valued fields keep their path only, and the
 * request headers that carry a URL as an opaque string (Referer, Link, Location) are dropped.
 * Free text is not parsed for URLs, but every string is filtered for the path of a known
 * credential route (`TOKEN_PATH_PREFIXES`), so a token in a message or an exception value goes too.
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
/**
 * Every attribute or event field the installed Sentry 11.10 packages (core, browser, nextjs,
 * node, vercel-edge, browser-utils) can fill with an address: `url.path` on HTTP and pageload
 * spans, the older `http.target`/`http.url`, `url.full`, and the LCP and UI-element URLs.
 */
const URL_FIELDS = new Set([
  "url",
  "http.url",
  "url.full",
  "url.path",
  "http.target",
  "browser.web_vital.lcp.url",
  "lcp.url",
  "ui.element.url",
]);
/**
 * Fields holding a transaction or span label: an HTTP one is `METHOD /path`, a non-HTTP one may
 * still embed an address (`navigation /clients?q=...`). Query and fragment leave the address
 * inside them, whatever the span op.
 */
const LABEL_FIELDS = new Set([
  "transaction",
  "name",
  "description",
  "next.span_name",
  "sentry.segment.name",
  "sentry.custom_span_name",
]);
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

/**
 * Routes whose address carries a secret as ONE path segment (an invitation or sign-in token, an
 * unsubscribe token, a result hash that grants access), written as the prefix that precedes it.
 * The segment after the prefix is replaced by the placeholder in every address and in every
 * string this file cleans. `sentry-url-scrub.test.ts` walks `src/app` and the backend routers
 * and fails when a credential segment is missing here or from the reviewed exemption list.
 */
export type TokenPathRule = {
  prefix: string;
  staticChildren: readonly string[];
};

export const TOKEN_PATH_RULES: readonly TokenPathRule[] = [
  // apps/mouth/src/app/prime/proposal/[token]
  { prefix: "/prime/proposal/", staticChildren: [] },
  // apps/mouth/src/app/api/prime/v2/proposal/[token]
  { prefix: "/api/prime/v2/proposal/", staticChildren: [] },
  // backend invitation check called by portal.api.ts validateInviteToken, no page folder
  { prefix: "/api/portal/invite/validate/", staticChildren: [] },
  // backend magic-link check called by auth.api.ts verifyMagicLink, no page folder
  { prefix: "/api/auth/verify-magic/", staticChildren: [] },
  // backend funnel e-mail unsubscribe link, reached through the /api/[...path] proxy
  { prefix: "/api/funnel_email/unsubscribe/", staticChildren: [] },
  // apps/mouth/src/app/visa/clock/[hash] and match/[hash]: the hash grants the result
  { prefix: "/visa/clock/", staticChildren: [] },
  { prefix: "/visa/match/", staticChildren: [] },
  // the backend routes those two pages call through the proxy (JWT issuance too)
  { prefix: "/api/visa/clock/", staticChildren: ["email"] },
  { prefix: "/api/visa/match/", staticChildren: [] },
] as const;

export const TOKEN_PATH_PREFIXES: readonly string[] = TOKEN_PATH_RULES.map(
  ({ prefix }) => prefix,
);

function isRouteTemplate(segment: string): boolean {
  return /^(?:\[(?:\.\.\.)?[^/\]]+\]|\{[^/{}]+\}|:[^/:]+)$/.test(segment);
}

/**
 * Replace the credential segment of every known credential route found anywhere in `text`: a
 * bare path, an absolute URL, or a path inside an error message. Only the segment is replaced,
 * the rest of the text is kept.
 */
export function redactTokenPaths(
  text: string,
  placeholder: string = PLACEHOLDER,
  rules: readonly TokenPathRule[] = TOKEN_PATH_RULES,
): string {
  let out = text;
  for (const { prefix, staticChildren } of rules) {
    let from = 0;
    for (;;) {
      const at = out.indexOf(prefix, from);
      if (at < 0) break;
      const nestedPrefix = rules.find(
        (rule) =>
          rule.prefix.length > prefix.length &&
          rule.prefix.endsWith(prefix) &&
          out.slice(
            at - (rule.prefix.length - prefix.length),
            at - (rule.prefix.length - prefix.length) + rule.prefix.length,
          ) === rule.prefix,
      );
      if (nestedPrefix) {
        from = at + prefix.length;
        continue;
      }
      const start = at + prefix.length;
      let end = start;
      while (end < out.length && !/[/?#\s"'`<>\\)]/.test(out[end])) end += 1;
      if (end === start) {
        from = start;
        continue;
      }
      const segment = out.slice(start, end);
      if (
        (staticChildren as readonly string[]).includes(segment) ||
        isRouteTemplate(segment)
      ) {
        from = end;
        continue;
      }
      out = `${out.slice(0, start)}${placeholder}${out.slice(end)}`;
      from = start + placeholder.length;
    }
  }
  return out;
}

function pathOnlyUrl(value: string): string {
  try {
    const parsed = new URL(value, "http://local.invalid");
    const schemeEnd = value.indexOf("//");
    const authorityEnd = schemeEnd < 0 ? -1 : value.indexOf("/", schemeEnd + 2);
    const rawPath =
      authorityEnd < 0
        ? value.split(/[?#]/, 1)[0] || "/"
        : value.slice(authorityEnd).split(/[?#]/, 1)[0] || "/";
    const pathname = redactTokenPaths(rawPath);
    if (!parsed.search && !parsed.hash && pathname === parsed.pathname) {
      return value;
    }
    return /^[a-z][a-z0-9+.-]*:/i.test(value)
      ? `${parsed.protocol}//${parsed.host}${pathname}`
      : pathname;
  } catch {
    return PLACEHOLDER;
  }
}

const ADDRESS_START = /^(\/|[a-z][a-z0-9+.-]*:\/\/)/i;

/** `METHOD /path?query`, a bare address or free label text: an address inside it loses its query and fragment. */
function cleanLabel(value: string): string {
  const space = value.indexOf(" ");
  if (
    space > 0 &&
    /^[A-Z]+$/.test(value.slice(0, space)) &&
    ADDRESS_START.test(value.slice(space + 1)) &&
    !/\s/.test(value.slice(space + 1))
  ) {
    return `${value.slice(0, space)} ${pathOnlyUrl(value.slice(space + 1))}`;
  }
  if (ADDRESS_START.test(value) && !/\s/.test(value)) return pathOnlyUrl(value);
  return redactTokenPaths(
    value.replace(/(\/[^\s?#"'`<>]*)[?#][^\s"'`<>]*/g, "$1"),
  );
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
  // Every string passes the credential-route filter: a path can sit in a message, an exception
  // value, a stack frame or a breadcrumb, not only in a URL-shaped field.
  if (typeof value === "string") return redactTokenPaths(value);
  if (depth > MAX_DEPTH) return value;
  if (Array.isArray(value)) return value.map((v) => scrub(v, depth + 1));
  if (!value || typeof value !== "object") return value;
  const source = value as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const [key, v] of Object.entries(source)) {
    if (key === "http.route" || key === "route") {
      out[key] = v;
    } else if (
      key === "transaction" &&
      (source.transaction_info as Record<string, unknown> | undefined)
        ?.source === "route"
    ) {
      out[key] = v;
    } else if (isSearchParamKey(key) && typeof v === "string") {
      out[key] = PLACEHOLDER;
    } else if (
      QUERY_FIELDS.has(key) &&
      (typeof v === "string" || (v !== null && typeof v === "object"))
    ) {
      // a string, or the structured form: an array of [name, value] pairs or an object
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
      URL_FIELDS.has(key) &&
      v &&
      typeof v === "object" &&
      !Array.isArray(v) &&
      typeof (v as Record<string, unknown>).value === "string"
    ) {
      // the typed attribute form of a streamed span: { value, type }
      out[key] = {
        ...(v as Record<string, unknown>),
        value: pathOnlyUrl((v as { value: string }).value),
      };
    } else if (LABEL_FIELDS.has(key) && typeof v === "string") {
      out[key] = cleanLabel(v);
    } else if (
      LABEL_FIELDS.has(key) &&
      v &&
      typeof v === "object" &&
      !Array.isArray(v) &&
      typeof (v as Record<string, unknown>).value === "string"
    ) {
      out[key] = {
        ...(v as Record<string, unknown>),
        value: cleanLabel((v as { value: string }).value),
      };
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
