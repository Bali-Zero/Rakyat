/**
 * Credential-bearing query parameters must never reach a third-party
 * analytics endpoint.
 *
 * Measured 2026-09-11 (live network capture on my.balizero.com, portal audit
 * finding L-GA / ux F8): opening `/portal/register?token=…` or
 * `/portal/magic?token=…` produced
 * `POST https://www.google-analytics.com/g/collect?…&dl=<the full URL, token
 * included>` → **HTTP 204**. Google accepted the single-use invite token and
 * the single-use magic-link token verbatim, in `page_location`, before the
 * client had touched anything. The invalid-token case leaked a second time as
 * `dr=` (referrer) after the auto-redirect to `/portal/login-upgraded?…
 * redirect=%2Fportal%2Fmagic%3Ftoken%3D…`. `www.google-analytics.com` is
 * whitelisted in the portal's `connect-src`, so nothing on the page blocked it.
 *
 * GA reads `document.location.href`, not the page's props, so no per-page code
 * can prevent this — the value has to be overridden before the first beacon
 * fires. `analyticsUrlRedactScript` does that in `<head>` (before gtag.js
 * loads) and `redactSensitiveQueryParams` does it for every explicit
 * `page_view` this app sends itself. Both now send origin and path only, secret path segments
 * replaced (`analyticsSafeHref`), whatever the query holds.
 *
 * This is defence in depth, not a substitute for keeping tokens out of URLs:
 * `src/app/visa/voa/auth/route.ts` shows the structural fix (a route handler
 * that moves the token into an HttpOnly cookie and redirects to a URL that
 * carries nothing). A scrub here also covers Google's own automatic
 * `page_view`, any future tag, and any token route added later.
 */

import {
  TOKEN_PATH_RULES,
  type TokenPathRule,
  redactTokenPaths,
  rawPathOf,
  encodeAsParser,
} from "@/lib/sentry-url-scrub";

/**
 * Query parameters whose VALUE is a credential or a personal identifier.
 * The key is kept (so the shape of the funnel is still measurable) and the
 * value is replaced — dropping the key entirely would hide that a
 * token-bearing URL was visited at all.
 */
export const SENSITIVE_QUERY_PARAMS = [
  "token",
  "magic_token",
  "invite",
  "invitation",
  "invite_token",
  "access_token",
  "id_token",
  "refresh_token",
  "code",
  "pin",
  "otp",
  "secret",
  "key",
  "api_key",
  "password",
  "email",
] as const;

export const REDACTED = "[redacted]";

/**
 * The only query parameters GA4 may keep: campaign attribution. Values stay as they are, in their
 * original order; a value that itself holds a credential path is still redacted.
 */
export const ANALYTICS_QUERY_ALLOWLIST = [
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_term",
  "utm_content",
  "gclid",
] as const;

function allowedQuery(search: string): string {
  const kept: string[] = [];
  for (const pair of search.replace(/^\?/, "").split("&")) {
    const eq = pair.indexOf("=");
    const key = eq < 0 ? pair : pair.slice(0, eq);
    if (!(ANALYTICS_QUERY_ALLOWLIST as readonly string[]).includes(key)) {
      continue;
    }
    const rawValue = eq < 0 ? "" : pair.slice(eq + 1);
    let decoded: string;
    try {
      decoded = decodeURIComponent(rawValue.replace(/\+/g, " "));
    } catch {
      kept.push(`${key}=${REDACTED}`);
      continue;
    }
    kept.push(
      decoded && /^[A-Za-z0-9 ._~-]+$/.test(decoded)
        ? `${key}=${rawValue}`
        : `${key}=${REDACTED}`,
    );
  }
  return kept.length ? `?${kept.join("&")}` : "";
}

/**
 * The address GA4 may see: origin and path, plus the campaign parameters of
 * `ANALYTICS_QUERY_ALLOWLIST` and nothing else. Every other query parameter and the fragment
 * never leave (proposal paths, typed search text, `return_nonce` and the next credential
 * parameter nobody has named yet all travel there), and the secret segment of every credential
 * route (`TOKEN_PATH_PREFIXES`) is replaced. An unparseable value loses everything after its
 * first `?` or `#`. Sentry keeps stripping every query: this allowlist is GA4 only.
 */
export function analyticsSafeHref(
  href: string,
  rules: readonly TokenPathRule[] = TOKEN_PATH_RULES,
): string {
  if (!href) return href;
  if (
    /^[\x00-\x1f\x7f]|[\x00-\x1f\x7f\\]/.test(href) ||
    href.startsWith(" ") ||
    href.endsWith(" ") ||
    !/^https?:\/\/(?!\/)[^/?#]/i.test(href)
  )
    return REDACTED;
  try {
    const url = new URL(href);
    const rawPath = rawPathOf(href);
    if (hasUnsafeEscape(rawPath)) return REDACTED;
    if (encodeAsParser(rawPath) !== url.pathname) return REDACTED;
    const path = redactTokenPaths(rawPath, REDACTED, rules);
    return `${url.protocol}//${url.host}${path}${allowedQuery(url.search)}`;
  } catch {
    return REDACTED;
  }
}

function hasUnsafeEscape(path: string): boolean {
  return [...path.matchAll(/%[0-9a-f]{2}/gi)].some((match) =>
    /[A-Za-z0-9\\/.\-_~]/.test(
      String.fromCharCode(parseInt(match[0].slice(1), 16)),
    ),
  );
}

/** A pathname as GA4 `page_path`: the credential segment of a known route replaced. */
export function analyticsSafePath(
  pathname: string,
  rules: readonly TokenPathRule[] = TOKEN_PATH_RULES,
): string {
  return redactTokenPaths(pathname, REDACTED, rules);
}

/**
 * Replace the value of every credential-bearing query parameter in `href`.
 *
 * Returns `href` unchanged when it holds none of them (the overwhelmingly
 * common case) and when it cannot be parsed — a scrubber must never be the
 * reason a page stops reporting.
 */
export function redactSensitiveQueryParams(href: string): string {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return href;
  }

  let touched = false;
  for (const param of SENSITIVE_QUERY_PARAMS) {
    if (url.searchParams.has(param)) {
      url.searchParams.set(param, REDACTED);
      touched = true;
    }
  }

  // A token can also ride inside another parameter's value, which is how the
  // magic-link redirect leaked it a second time:
  // `?redirect=%2Fportal%2Fmagic%3Ftoken%3D<token>`. Redact any value that
  // embeds one of the sensitive keys as a nested query parameter.
  for (const [param, value] of Array.from(url.searchParams.entries())) {
    if (
      SENSITIVE_QUERY_PARAMS.some((p) => new RegExp(`[?&]${p}=`).test(value))
    ) {
      url.searchParams.set(param, REDACTED);
      touched = true;
    }
  }

  return touched ? url.href : href;
}

/**
 * The same redaction, as an inline `<head>` script, generated from the SAME
 * parameter list so the two copies cannot drift.
 *
 * It runs before `@next/third-parties`' gtag.js is loaded, so the
 * `gtag('set', …)` lands in `dataLayer` ahead of the `gtag('config', …)`
 * whose automatic `page_view` is the beacon that leaked. It only calls `set`
 * when the URL actually carries a credential: on every ordinary page GA keeps
 * its default behaviour untouched.
 */
export function buildAnalyticsUrlRedactScript(
  rules: readonly TokenPathRule[] = TOKEN_PATH_RULES,
): string {
  return `(function(){try{var P=${JSON.stringify(
    rules.map(({ prefix }) => prefix),
  )};var C=${JSON.stringify(rules.map(({ staticChildren }) => staticChildren))};var A=${JSON.stringify(ANALYTICS_QUERY_ALLOWLIST)};var S=${JSON.stringify(
    SENSITIVE_QUERY_PARAMS,
  )};var R=${JSON.stringify(REDACTED)};var E={};function tt(x){return /^(?:\\[(?:\\.\\.\\.)?[^/\\]]+\\]|\\{[^/{}]+\\}|%7B[^/]+%7D|:[^/:]+)$/i.test(x);}function tp(s){P.forEach(function(p,n){var i=0;for(;;){i=s.indexOf(p,i);if(i<0)break;var nested=P.some(function(x){var d=x.length-p.length;return d>0&&x.slice(-p.length)===p&&s.slice(i-d,i-d+x.length)===x;});if(nested){i+=p.length;continue;}var a=i+p.length;var e=a;while(e<s.length&&'/?#'.indexOf(s.charAt(e))<0&&!/[\\s"'<>)]/.test(s.charAt(e)))e++;var x=s.slice(a,e);if(e>a&&!tt(x)&&C[n].indexOf(x)<0){s=s.slice(0,a)+R+s.slice(e);e=a+R.length;}i=e>a?e:a;}});return s;}function ad(h){return !/[\\x00-\\x1f\\x7f\\\\]/.test(h)&&h.charAt(0)!==' '&&h.charAt(h.length-1)!==' '&&/^https?:\\/\\/(?!\\/)[^/?#]/i.test(h);}function rp(h){var r=h.split(/[?#]/)[0]||'';var m=r.match(/^(?:[a-z][a-z0-9+.-]*:)?\\/\\/[^/]*/i);return m?r.slice(m[0].length)||'/':r||'/';}function ep(s){var o='';for(var i=0;i<s.length;i++){var c=s.charAt(i);o+=c==='%'||c==='/'||c==='.'||/[A-Za-z0-9]/.test(c)?c:(E[c]||(E[c]=new URL('http://x/a'+c).pathname.slice(2)));}return o;}function d2(s){var r=/%[0-9a-f]{2}/gi,m;while((m=r.exec(s))){if(/[A-Za-z0-9\\\\/.\\-_~]/.test(String.fromCharCode(parseInt(m[0].slice(1),16))))return true;}return false;}function scrub(h){if(!h)return h;if(!ad(h))return R;var u;try{u=new URL(h);}catch(e){return R;}var path=rp(h);if(d2(path)||ep(path)!==u.pathname)return R;var q=[];u.search.replace(/^\\?/,'').split('&').forEach(function(pr){var z=pr.indexOf('=');var k=z<0?pr:pr.slice(0,z);if(A.indexOf(k)<0)return;var v=z<0?'':pr.slice(z+1);var d;try{d=decodeURIComponent(v.replace(/\\+/g,' '));}catch(e){q.push(k+'='+R);return;}q.push(d&&/^[A-Za-z0-9 ._~-]+$/.test(d)?k+'='+v:k+'='+R);});return u.protocol+'//'+u.host+tp(path)+(q.length?'?'+q.join('&'):'');}var loc=window.location.href;var ref=document.referrer;var sl=scrub(loc);var sr=scrub(ref);if(sl!==loc||sr!==ref){window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('set',{page_location:sl,page_referrer:sr});}}catch(e){}})();`;
}

export const analyticsUrlRedactScript = buildAnalyticsUrlRedactScript();
