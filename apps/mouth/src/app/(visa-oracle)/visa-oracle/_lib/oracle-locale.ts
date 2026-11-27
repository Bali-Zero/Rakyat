import type { Language } from "./flow";

export type OracleInternalPath = "/visa-oracle" | "/visa-oracle/privacy";

const VISIT_LOCALE_PARAM = "lang";

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

/** `replaceState` only — no extra history entries, no storage. */
export function syncVisitLocaleInAddressBar(language: Language): void {
  if (typeof window === "undefined") return;
  const { pathname, search, hash } = window.location;
  const next = visitLocaleUrlFromParts(pathname, search, hash, language);
  const current = `${pathname}${search}${hash}`;
  if (next !== current) {
    window.history.replaceState(window.history.state, "", next);
  }
}

/** Visit-scoped `?lang=id` on the two internal oracle routes only; English omits the param. */
export function oracleInternalHref(
  path: OracleInternalPath,
  language: Language,
): string {
  return visitLocaleUrlFromParts(path, "", "", language);
}
