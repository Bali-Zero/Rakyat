/**
 * Overdue items for the portal dashboard summary.
 *
 * The summary card used to say "All caught up" whenever the summary API
 * returned no open actions and no deadlines in the next 30 days. A filing
 * that was already late (LKPM Q3 2026, due 15 Oct) is in neither list, so a
 * client with a late filing was told nothing was due. Overdue items now come
 * first and always count against "All caught up", with no lookback limit.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  DashboardSummary,
  LKPMDraftSummary,
} from "@/lib/api/portal/portal.types";

export interface OverdueItem {
  id: string;
  label: string;
  daysLate: number;
  href: string;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** Calendar date (YYYY-MM-DD) of an ISO string, ignoring the time part. */
function dayOf(iso: string): number {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

function localDay(now: Date): number {
  return Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
}

/**
 * Whole calendar days between the due date and today. 15 Oct → 10 Nov is 26.
 * Not `Math.floor` of a timestamp difference: that turns "26 days and a few
 * hours" into 27 once the value is negative.
 */
export function daysLate(dueIso: string, now: Date): number {
  return Math.round((localDay(now) - dayOf(dueIso)) / DAY_MS);
}

/** An LKPM report counts as done once it is filed in OSS. */
function lkpmFiled(report: LKPMDraftSummary): boolean {
  return (
    report.status === "submitted" ||
    report.status === "archived" ||
    report.oss_submitted === true
  );
}

/**
 * LKPM filing deadline per quarter (month, day). Same table as
 * lkpm_service.QUARTER_DEADLINES and the portal LKPM page; Q4 falls in
 * January of the next year.
 */
const LKPM_DEADLINE: Record<number, [number, number]> = {
  1: [4, 15],
  2: [7, 15],
  3: [10, 15],
  4: [1, 15],
};

function lkpmDeadlineIso(q: number, year: number): string {
  const [month, day] = LKPM_DEADLINE[q];
  const y = q === 4 ? year + 1 : year;
  return `${y}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function quarterIndex(r: { quarter: string; year: number }): number | null {
  const q = Number(r.quarter.replace(/^Q/i, ""));
  return q >= 1 && q <= 4 ? r.year * 4 + (q - 1) : null;
}

/**
 * Every quarter from the client's first LKPM report up to today whose
 * deadline has passed and that has no filed report. No lookback limit: the
 * `/api/v1/lkpm/deadlines` calendar only returns the last 30 days, so a
 * quarter more than a month late would silently drop out of it.
 */
function lkpmOverdue(history: LKPMDraftSummary[], now: Date): OverdueItem[] {
  const indexes = history
    .map(quarterIndex)
    .filter((i): i is number => i !== null);
  if (indexes.length === 0) return [];

  const thisQuarter = now.getFullYear() * 4 + Math.floor(now.getMonth() / 3);
  const items: OverdueItem[] = [];
  for (let i = Math.min(...indexes); i <= thisQuarter; i++) {
    const year = Math.floor(i / 4);
    const q = (i % 4) + 1;
    const late = daysLate(lkpmDeadlineIso(q, year), now);
    if (late <= 0) continue;
    const filed = history.some((r) => quarterIndex(r) === i && lkpmFiled(r));
    if (filed) continue;
    items.push({
      id: `lkpm-Q${q}-${year}`,
      label: `LKPM Q${q} ${year}`,
      daysLate: late,
      href: "/portal/lkpm",
    });
  }
  return items;
}

const DEADLINE_HREF: Record<string, string> = {
  visa: "/portal/visa",
  passport: "/portal/visa",
};

export function collectOverdue({
  summaryDeadlines,
  lkpmHistory,
  now,
}: {
  summaryDeadlines: DashboardSummary["upcoming_deadlines"];
  lkpmHistory: LKPMDraftSummary[];
  now: Date;
}): OverdueItem[] {
  const items: OverdueItem[] = [];

  for (const d of summaryDeadlines) {
    if (!d.due_date) continue;
    const late = daysLate(d.due_date, now);
    if (late <= 0) continue;
    items.push({
      id: d.id,
      label: d.label,
      daysLate: late,
      href: DEADLINE_HREF[d.kind ?? ""] ?? "/portal/process",
    });
  }

  // Only a client with LKPM reports on file has the obligation.
  items.push(...lkpmOverdue(lkpmHistory, now));

  return items.sort((a, b) => b.daysLate - a.daysLate);
}

/** Only deadlines that are still ahead (or due today). */
export function stillUpcoming(
  deadlines: DashboardSummary["upcoming_deadlines"],
  now: Date,
): DashboardSummary["upcoming_deadlines"] {
  return deadlines.filter((d) => !d.due_date || daysLate(d.due_date, now) <= 0);
}

/** The LKPM history read the portal LKPM page already makes. */
export function usePortalLkpmHistory() {
  return useQuery({
    queryKey: ["portal", "dashboard", "lkpm-history"],
    queryFn: async () => {
      const res = await api.get<{
        success: boolean;
        items: LKPMDraftSummary[];
      }>("/api/v1/lkpm/history/me");
      return res.items ?? [];
    },
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
}
