export type DashboardRole =
  | "zero"
  | "team"
  | "tax"
  | "marketing"
  | "accounting";

export const VALID_ROLES: DashboardRole[] = [
  "zero",
  "team",
  "tax",
  "marketing",
  "accounting",
];

/**
 * Normalizes a raw role string from the API into a typed DashboardRole.
 * Admins always get 'zero'. Unknown roles fall back to 'team'.
 */
export function normalizeDashboardRole(
  raw: string | undefined,
  isAdmin: boolean,
): DashboardRole {
  if (isAdmin) return "zero";
  const lower = raw?.toLowerCase() ?? "";
  const found = VALID_ROLES.find((r) => r === lower);
  return found ?? TITLE_ALIASES.get(lower) ?? "team";
}

/**
 * Job titles that resolve to a widget although they are not a widget name.
 * "HR & Accounting" (reorganisation 2026-11-09) replaces the "Accounting"
 * title and must keep the accounting widget. A Map, not an object literal:
 * a plain index would also answer for inherited keys ("constructor", ...).
 */
const TITLE_ALIASES: ReadonlyMap<string, DashboardRole> = new Map<string, DashboardRole>([
  ["hr & accounting", "accounting"],
]);
