/**
 * The typed search text of the client, practice and company search bars travels in the query
 * string of a URL (`?search=`, `?q=`: names, e-mails, phones, tax ids). Nothing here scrubs free
 * text (text is not reliably URL-shaped): a URL that is going to be logged is REBUILT from its
 * parts, so its query and fragment never enter the line in the first place.
 */

import { redactTokenPaths } from "@/lib/sentry-url-scrub";

/** The path of a URL or referrer, never its query or fragment; null when it is not a URL. */
export function pathOnly(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    return new URL(raw, "http://local.invalid").pathname;
  } catch {
    return null;
  }
}

/**
 * The error of a failed upstream call, rebuilt so that neither its message nor its stack carries
 * the exact target URL or the exact query string of the request (a fetch parse error prints the
 * whole URL). The cause chain is dropped: it can hold the same URL.
 */
export function safeUpstreamError(
  error: Error,
  targetUrl: string,
  logTargetUrl: string,
  search: string,
  pathname?: string,
  logPathname?: string,
): Error {
  const clean = (text: string) => {
    let out = text.split(targetUrl).join(logTargetUrl);
    if (search) out = out.split(search).join("");
    // the request path on its own, in its logged form (a secret segment replaced, or the whole path
    // a placeholder because the upstream would read it differently)
    if (pathname && logPathname !== undefined)
      out = out.split(pathname).join(logPathname);
    // the path of a credential route (invitation, sign-in, unsubscribe, result hash) can sit in
    // the message on its own, e.g. a proxy log line: only its secret segment is replaced
    return redactTokenPaths(out);
  };
  const safe = new Error(clean(error.message));
  safe.name = error.name;
  if (error.stack) safe.stack = clean(error.stack);
  return safe;
}
