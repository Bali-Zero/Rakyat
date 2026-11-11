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

function isReadableReport(r: LKPMDraftSummary): boolean {
  return (
    typeof r?.quarter === "string" &&
    /^Q[1-4]$/i.test(r.quarter) &&
    Number.isInteger(r.year) &&
    Number.isInteger(r.company_id)
  );
}

function quarterIndex(r: { quarter: string; year: number }): number | null {
  const q = Number(r.quarter.replace(/^Q/i, ""));
  return q >= 1 && q <= 4 ? r.year * 4 + (q - 1) : null;
}

/**
 * Every quarter, per company, from that company's first LKPM report up to
 * today whose deadline has passed and that has no filed report FOR THAT
 * COMPANY. The history carries the reports of every company the client is
 * linked to, so "filed" must never be answered across companies: one filed
 * company cannot hide another's late quarter. No lookback limit: the
 * `/api/v1/lkpm/deadlines` calendar only returns the last 30 days, so a
 * quarter more than a month late would silently drop out of it.
 */
function lkpmOverdue(history: LKPMDraftSummary[], now: Date): OverdueItem[] {
  const byCompany = new Map<string, LKPMDraftSummary[]>();
  for (const report of history) {
    const key = String(report.company_id ?? "none");
    byCompany.set(key, [...(byCompany.get(key) ?? []), report]);
  }
  const multiCompany = byCompany.size > 1;

  const thisQuarter = now.getFullYear() * 4 + Math.floor(now.getMonth() / 3);
  const items: OverdueItem[] = [];
  for (const [companyKey, reports] of byCompany) {
    const indexes = reports
      .map(quarterIndex)
      .filter((i): i is number => i !== null);
    if (indexes.length === 0) continue;

    const name = reports.find((r) => r.company_name)?.company_name;
    for (let i = Math.min(...indexes); i <= thisQuarter; i++) {
      const year = Math.floor(i / 4);
      const q = (i % 4) + 1;
      const late = daysLate(lkpmDeadlineIso(q, year), now);
      if (late <= 0) continue;
      const filed = reports.some((r) => quarterIndex(r) === i && lkpmFiled(r));
      if (filed) continue;
      items.push({
        id: `lkpm-${companyKey}-Q${q}-${year}`,
        label:
          multiCompany && name
            ? `LKPM Q${q} ${year} · ${name}`
            : `LKPM Q${q} ${year}`,
        daysLate: late,
        href: "/portal/lkpm",
      });
    }
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

/**
 * The LKPM history read the portal LKPM page already makes.
 *
 * The cache key carries the signed-in identity and the superuser impersonation
 * target, like `usePortalUnreadMessages`.
 *
 * A reply that is not a well-formed history is an error, never "no reports":
 * an empty list means "nothing overdue" downstream.
 */
export function usePortalLkpmHistory() {
  return useQuery({
    queryKey: [
      "portal",
      "dashboard",
      "lkpm-history",
      api.getUserProfile()?.id ?? "cookie-session",
      api.getPortalImpersonation(),
    ],
    queryFn: async () => {
      const res = await api.get<{
        success: boolean;
        items: LKPMDraftSummary[];
      }>("/api/v1/lkpm/history/me");
      if (!res?.success || !Array.isArray(res.items)) {
        throw new Error("LKPM history unavailable");
      }
      // A row that cannot be placed in a company and quarter would be skipped silently
      // and could turn into "nothing overdue": unreadable, not empty.
      if (!res.items.every(isReadableReport)) {
        throw new Error("LKPM history unreadable");
      }
      return res.items;
    },
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
}
