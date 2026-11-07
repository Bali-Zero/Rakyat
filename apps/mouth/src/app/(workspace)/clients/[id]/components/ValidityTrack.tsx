import React from "react";
import { cn } from "@/lib/utils";
import { dayNumber, parseCalendarDate } from "./documentValidity";

/**
 * A validity bar for a document's issue→expiry window: a hairline track with
 * an ink fill for the elapsed share — the fill's own right edge IS "today",
 * so no separate marker is drawn (this folder's copper allow-list is closed
 * to the `Stamp` primitive only; a raw `--bz-copper` tick here would not be
 * on it, see the R3c guard in `__tests__/client-detail-desk.test.tsx`).
 *
 * Pure calendar-day math on the two dates and the card's day: no clock, no
 * `Date`. daysRemaining is the days left clamped to the window; the fill is
 * the elapsed share. Nothing renders unless both dates are dates and the
 * window is positive.
 */
export function ValidityTrack({
  issueDate,
  expiryDate,
  day,
  className,
}: {
  /** The document's real issue date. */
  issueDate: string | null | undefined;
  /** The document's real expiry date. */
  expiryDate: string | null | undefined;
  /** The card's day (`YYYY-MM-DD`). */
  day: string;
  className?: string;
}) {
  const issue = parseCalendarDate(issueDate);
  const expiry = parseCalendarDate(expiryDate);
  const today = parseCalendarDate(day);
  if (!issue || !expiry || !today) return null;
  const daysTotal = dayNumber(expiry) - dayNumber(issue);
  if (daysTotal <= 0) return null;
  const daysLeft = dayNumber(expiry) - dayNumber(today);
  const daysRemaining = Math.min(Math.max(daysLeft, 0), daysTotal);
  const daysElapsed = daysTotal - daysRemaining;
  const pct = (daysElapsed / daysTotal) * 100;

  return (
    <div
      role="img"
      aria-label={`${daysElapsed} of ${daysTotal} days elapsed, ${daysRemaining} remaining`}
      className={cn(
        "relative my-3.5 h-[4px] rounded-[2px] bg-[var(--bz-border)]",
        className,
      )}
    >
      <i
        className="absolute inset-y-0 left-0 block rounded-[2px] bg-[var(--bz-kita-ink-fill)] not-italic"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
