/**
 * Groups of practice types for the HR rate and manual-bonus dropdowns.
 *
 * The server assigns each type a group (hr_service.practice_group); this module
 * only orders and labels them, so the Settings page and the Add bonus form show
 * the same four groups in the same order — and the Tax group is always present,
 * even before the first tax rate exists.
 */

import type { BonusPracticeType, PracticeGroupId } from "@/types/hr";

export const PRACTICE_GROUPS: readonly {
  id: PracticeGroupId;
  label: string;
}[] = [
  { id: "visa", label: "Visa & KITAS" },
  { id: "company", label: "Company" },
  { id: "tax", label: "Tax" },
  { id: "other", label: "Other" },
];

export interface PracticeTypeGroup {
  id: PracticeGroupId;
  label: string;
  items: BonusPracticeType[];
}

/** Group by `group` in the fixed order, each group sorted by name. Empty groups are kept. */
export function groupPracticeTypes(
  types: readonly BonusPracticeType[],
  query = "",
): PracticeTypeGroup[] {
  const q = query.trim().toLowerCase();
  const match = (t: BonusPracticeType) =>
    !q ||
    t.name.toLowerCase().includes(q) ||
    t.code.toLowerCase().replace(/_/g, " ").includes(q);
  return PRACTICE_GROUPS.map((g) => ({
    ...g,
    items: types
      .filter((t) => t.group === g.id && match(t))
      .sort((a, b) => a.name.localeCompare(b.name)),
  }));
}

/** Group of a rate row whose code may not be in the active catalogue (retired type). */
export function groupOfCode(
  code: string,
  types: readonly BonusPracticeType[],
): PracticeGroupId {
  const known = types.find((t) => t.code === code);
  if (known) return known.group;
  const low = code.toLowerCase();
  if (low.startsWith("tax")) return "tax";
  if (low.startsWith("company")) return "company";
  if (/^(visa|kitas|kitap|voa|extension)/.test(low)) return "visa";
  return "other";
}

/** One theme token per group, so Settings, the picker and the bonus rows agree (day and night). */
export const GROUP_TONE: Record<PracticeGroupId, string> = {
  visa: "var(--state-info)",
  company: "var(--bz-accent)",
  tax: "var(--state-success)",
  other: "var(--bz-text-2)",
};
