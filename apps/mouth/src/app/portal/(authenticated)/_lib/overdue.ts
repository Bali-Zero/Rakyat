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
  LKPMDeadline,
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

const DEADLINE_HREF: Record<string, string> = {
  visa: "/portal/visa",
  passport: "/portal/visa",
};

export function collectOverdue({
  summaryDeadlines,
  lkpmDeadlines,
  lkpmHistory,
  now,
}: {
  summaryDeadlines: DashboardSummary["upcoming_deadlines"];
  lkpmDeadlines: LKPMDeadline[];
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

  // The LKPM deadline calendar is the same for everyone. Only a client with
  // LKPM reports on file has the obligation, so no history = no LKPM item.
  if (lkpmHistory.length > 0) {
    for (const d of lkpmDeadlines) {
      const late = daysLate(d.deadline, now);
      if (late <= 0) continue;
      const filed = lkpmHistory.some(
        (r) => r.quarter === d.quarter && r.year === d.year && lkpmFiled(r),
      );
      if (filed) continue;
      items.push({
        id: `lkpm-${d.quarter}-${d.year}`,
        label: `LKPM ${d.quarter} ${d.year}`,
        daysLate: late,
        href: "/portal/lkpm",
      });
    }
  }

  return items.sort((a, b) => b.daysLate - a.daysLate);
}

/** Only deadlines that are still ahead (or due today). */
export function stillUpcoming(
  deadlines: DashboardSummary["upcoming_deadlines"],
  now: Date,
): DashboardSummary["upcoming_deadlines"] {
  return deadlines.filter((d) => !d.due_date || daysLate(d.due_date, now) <= 0);
}

/** The two LKPM reads the portal LKPM page already makes, for the dashboard. */
export function usePortalLkpmStatus() {
  return useQuery({
    queryKey: ["portal", "dashboard", "lkpm"],
    queryFn: async () => {
      const [history, deadlines] = await Promise.all([
        api.get<{ success: boolean; items: LKPMDraftSummary[] }>(
          "/api/v1/lkpm/history/me",
        ),
        api.get<{ success: boolean; deadlines: LKPMDeadline[] }>(
          "/api/v1/lkpm/deadlines",
        ),
      ]);
      return {
        history: history.items ?? [],
        deadlines: deadlines.deadlines ?? [],
      };
    },
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
}
