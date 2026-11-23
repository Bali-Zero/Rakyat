import { NextResponse } from "next/server";
import { toIcalString } from "@balizero/core/utils";
import { getUpcomingTaxDeadlines } from "../deadlines";

// Computed relative to request time, not build time: force per-request
// rendering so the feed never freezes on a build-time "now".
export const dynamic = "force-dynamic";

export async function GET() {
  const ics = toIcalString(
    getUpcomingTaxDeadlines(new Date(), 12).map((d) => ({
      uid: `balizero-tax-${d.id}@balizero.com`,
      summary: d.title,
      allDayDate: d.date.slice(0, 10),
      description: d.description,
    })),
    { prodId: "BaliZero//TaxCalendar//EN" },
  );
  return new NextResponse(ics, {
    headers: {
      "Content-Type": "text/calendar; charset=utf-8",
      "Content-Disposition": 'attachment; filename="bali-tax-deadlines.ics"',
    },
  });
}
