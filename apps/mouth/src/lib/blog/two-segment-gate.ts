import { SERVICES_DATA } from "@/data/services_data";

import { CATEGORY_SEO } from "./category-seo";

/**
 * The status gate for two-segment public URLs.
 *
 * `(blog)/[category]/[slug]` catches every two-segment path no real route
 * owns, and `app/loading.tsx` wraps that page in a Suspense boundary, so the
 * response head (HTTP 200) goes out before the page can call `notFound()`.
 * Measured on a production build: removing the root `loading.tsx` is the only
 * change inside the route tree that yields a 404, and it would strip the
 * boundary from every route, the workspace included. The status has to be
 * decided before rendering instead, and that is `proxy.ts`'s job.
 */

/**
 * Every two-segment route the app really serves on the public domain,
 * `[category]/[slug]` excluded. `"*"` means the second segment is a dynamic
 * parameter. The table is a mirror of `src/app`, and `two-segment-gate.test.ts`
 * recomputes it from the file tree in both directions: a route added without
 * a row here turns that test red instead of turning the URL into a 404.
 *
 * Paths the proxy has already redirected by the time the gate runs (the
 * workspace routes in INTERNAL_ROUTES, /portal, /chat, /api) are not listed.
 */
export const PUBLIC_TWO_SEGMENT_ROUTES: Readonly<
  Record<string, readonly string[] | "*">
> = {
  assessment: ["briefing"],
  book: "*",
  kbli: "*",
  property: ["eligibility"],
  services: "*",
  taxes: ["gap"],
  visa: ["clock", "match", "privacy", "second-home", "terms", "voa"],
  "visa-oracle": ["privacy", "unlock"],
};

/**
 * The second segments a `"*"` route really serves, for the routes whose
 * parameter comes from a static table bundled with the app: the table's keys
 * (read from the table the page renders from, so a service added there is
 * served without touching this file) plus the static pages that sit beside
 * the `[slug]` folder. `two-segment-gate.services.test.ts` reads those static
 * pages from the file tree, so a new one without a row here turns it red.
 * Routes not listed here (`book`, `kbli`) keep passing every value: their
 * parameters are not known before rendering.
 */
export const STATIC_PARAM_VALUES: Readonly<Record<string, readonly string[]>> =
  {
    services: [...Object.keys(SERVICES_DATA), "compliance"],
  };

export type TwoSegmentVerdict = "pass" | "not-found";

/**
 * - "pass": not a two-segment path, a real route owns it, or its first segment
 *   is an article category. A valid category with an unknown slug stays a 200
 *   with noindex: whether the slug exists depends on backend articles that
 *   appear at runtime, and no decision made before rendering can know them.
 * - "not-found": no route and no article category can serve it.
 */
export function classifyTwoSegmentPath(routePath: string): TwoSegmentVerdict {
  const segments = routePath.split("/").filter(Boolean);
  if (segments.length !== 2) return "pass";
  const [first, second] = segments;
  // A dotted value is a file request, and the proxy's early return owns those.
  if (second.includes(".")) return "pass";

  const own = Object.prototype.hasOwnProperty.call(
    PUBLIC_TWO_SEGMENT_ROUTES,
    first,
  )
    ? PUBLIC_TWO_SEGMENT_ROUTES[first]
    : undefined;
  if (own === "*") {
    const known = Object.prototype.hasOwnProperty.call(
      STATIC_PARAM_VALUES,
      first,
    )
      ? STATIC_PARAM_VALUES[first]
      : undefined;
    if (!known || known.includes(second)) return "pass";
  } else if (own?.includes(second)) return "pass";

  return Object.prototype.hasOwnProperty.call(CATEGORY_SEO, first)
    ? "pass"
    : "not-found";
}
