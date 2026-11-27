import type { Language } from "./flow";

export type OracleInternalPath = "/visa-oracle" | "/visa-oracle/privacy";

export function parseOracleLocale(value: string | null | undefined): Language {
  return value === "id" ? "id" : "en";
}

/** Visit-scoped `?lang=id` on the two internal oracle routes only; English omits the param. */
export function oracleInternalHref(
  path: OracleInternalPath,
  language: Language,
): string {
  return language === "id" ? `${path}?lang=id` : path;
}
