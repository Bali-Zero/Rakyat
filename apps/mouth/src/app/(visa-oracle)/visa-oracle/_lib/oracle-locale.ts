import type { Language } from "./flow";

export type OracleInternalPath = "/visa-oracle" | "/visa-oracle/privacy";

export type HistoryNavigationType =
  "navigate" | "reload" | "back_forward" | "unknown";

const VISIT_LOCALE_PARAM = "lang";
export const VISIT_LOCALE_STORAGE_KEY = "visa-oracle-visit-lang";

const ORACLE_LOCALE_PATHS = new Set<OracleInternalPath>([
  "/visa-oracle",
  "/visa-oracle/privacy",
]);

export function parseOracleLocale(value: string | null | undefined): Language {
  return value === "id" ? "id" : "en";
}

function searchParamsFrom(search: string): URLSearchParams {
  const raw = search.startsWith("?") ? search.slice(1) : search;
  return new URLSearchParams(raw);
}

export function localeFromLocationSearch(search: string): Language {
  return parseOracleLocale(searchParamsFrom(search).get(VISIT_LOCALE_PARAM));
}

/**
 * Rebuild pathname + search + hash for oracle routes, setting or removing only
 * `lang`. Other query keys and the fragment are preserved.
 */
export function visitLocaleUrlFromParts(
  pathname: string,
  search: string,
  hash: string,
  language: Language,
): string {
  if (!ORACLE_LOCALE_PATHS.has(pathname as OracleInternalPath)) {
    return `${pathname}${search}${hash}`;
  }
  const params = searchParamsFrom(search);
  if (language === "id") {
    params.set(VISIT_LOCALE_PARAM, "id");
  } else {
    params.delete(VISIT_LOCALE_PARAM);
  }
  const query = params.toString();
  return `${pathname}${query ? `?${query}` : ""}${hash}`;
}

export function readVisitLocaleHint(): Language | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(VISIT_LOCALE_STORAGE_KEY);
    if (raw === "id" || raw === "en") return raw;
  } catch {
    // storage denied — degrade to URL-only
  }
  return null;
}

export function writeVisitLocaleHint(language: Language): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.setItem(VISIT_LOCALE_STORAGE_KEY, language);
  } catch {
    // storage denied — URL sync still works
  }
}

export function getHistoryNavigationType(): HistoryNavigationType {
  if (typeof window === "undefined") return "unknown";
  const getEntries = performance.getEntriesByType?.bind(performance);
  if (!getEntries) return "unknown";
  const entry = getEntries("navigation")[0] as
    PerformanceNavigationTiming | undefined;
  const type = entry?.type;
  if (type === "navigate" || type === "reload" || type === "back_forward") {
    return type;
  }
  return "unknown";
}

/** Fresh explicit navigation honors the URL; traversal/reload/bfcache favor the tab hint. */
export function resolveVisitLocale(inputs: {
  urlLanguage: Language;
  visitHint: Language | null;
  navigationType: HistoryNavigationType;
  pageshowPersisted?: boolean;
  popstate?: boolean;
}): Language {
  const traversal =
    inputs.pageshowPersisted === true ||
    inputs.popstate === true ||
    inputs.navigationType === "back_forward" ||
    inputs.navigationType === "reload";

  if (traversal) {
    return inputs.visitHint ?? inputs.urlLanguage;
  }
  return inputs.urlLanguage;
}

/**
 * Next 16.4.5: first `replaceState(null, "", url)` syncs the App Router; restore
 * opaque custom state on the same URL with a second call when needed.
 */
export function replaceOracleHistoryUrl(nextUrl: string): void {
  if (typeof window === "undefined") return;
  const prior = window.history.state;
  window.history.replaceState(null, "", nextUrl);
  if (prior != null) {
    window.history.replaceState(prior, "");
  }
}

/** `replaceState` only — no extra history entries. Persists tab visit hint. */
export function syncVisitLocaleInAddressBar(language: Language): void {
  if (typeof window === "undefined") return;
  const { pathname, search, hash } = window.location;
  const next = visitLocaleUrlFromParts(pathname, search, hash, language);
  const current = `${pathname}${search}${hash}`;
  if (next !== current) {
    replaceOracleHistoryUrl(next);
  }
  writeVisitLocaleHint(language);
}

export function reconcileVisitLocaleOnClient(
  language: Language,
  options?: { pageshowPersisted?: boolean; popstate?: boolean },
): Language {
  if (typeof window === "undefined") return language;
  const { pathname, search, hash } = window.location;
  if (!ORACLE_LOCALE_PATHS.has(pathname as OracleInternalPath)) {
    return language;
  }
  const urlLanguage = localeFromLocationSearch(search);
  const visitHint = readVisitLocaleHint();
  const navigationType = getHistoryNavigationType();
  let effective = resolveVisitLocale({
    urlLanguage,
    visitHint,
    navigationType,
    pageshowPersisted: options?.pageshowPersisted,
    popstate: options?.popstate,
  });

  writeVisitLocaleHint(effective);

  const currentUrl = `${pathname}${search}${hash}`;
  const nextUrl = visitLocaleUrlFromParts(pathname, search, hash, effective);
  if (nextUrl !== currentUrl) {
    replaceOracleHistoryUrl(nextUrl);
  }
  return effective;
}

/** Visit-scoped `?lang=id` on the two internal oracle routes only; English omits the param. */
export function oracleInternalHref(
  path: OracleInternalPath,
  language: Language,
): string {
  return visitLocaleUrlFromParts(path, "", "", language);
}
