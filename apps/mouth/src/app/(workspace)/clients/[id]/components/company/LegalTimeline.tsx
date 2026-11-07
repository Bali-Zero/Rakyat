import React from "react";
import { SERIF, TABULAR } from "@/components/workspace/r19";
import { cn } from "@/lib/utils";

export interface LegalMilestone {
  key: string;
  /** What kind of milestone it is, in sentence case (Deed of establishment, NIB ...). */
  kind: string;
  title: string;
  body: string;
  refText?: string;
  date?: string;
}

/**
 * Newest dated milestone first; milestones with no recorded date follow, in
 * the order given. A stable sort, so equal dates keep their input order.
 */
export function orderMilestones(entries: LegalMilestone[]): LegalMilestone[] {
  const time = (e: LegalMilestone) =>
    e.date ? Date.parse(e.date) || 0 : Number.NEGATIVE_INFINITY;
  return entries
    .map((e, i) => ({ e, i }))
    .sort((a, b) => {
      const d = time(b.e) - time(a.e);
      if (d !== 0 && !Number.isNaN(d)) return d;
      return a.i - b.i;
    })
    .map(({ e }) => e);
}

/** A vertical timeline: a node per milestone, serif date numerals, the most
 * recent dated milestone emphasised (filled ink node, larger numeral). */
export function LegalTimeline({
  entries,
  formatDate,
}: {
  entries: LegalMilestone[];
  formatDate: (d: string) => string;
}) {
  const ordered = orderMilestones(entries);
  const latestKey = ordered.find((e) => e.date)?.key;
  return (
    <ol aria-label="Legal milestones" className="relative py-5">
      {ordered.map((entry, i) => {
        const latest = entry.key === latestKey;
        return (
          <li
            key={entry.key}
            data-latest={latest ? "true" : undefined}
            className={cn(
              "relative grid grid-cols-1 gap-x-8 pl-8 sm:grid-cols-[11rem_minmax(0,1fr)]",
              i < ordered.length - 1 && "pb-7",
            )}
          >
            {i < ordered.length - 1 && (
              <span
                aria-hidden="true"
                className="absolute bottom-0 left-[5px] top-3 w-px bg-[var(--bz-border)]"
              />
            )}
            <span
              aria-hidden="true"
              className={cn(
                "absolute left-0 top-[7px] h-[11px] w-[11px] rounded-full border",
                latest
                  ? "border-[var(--tx-pure)] bg-[var(--tx-pure)]"
                  : "border-[var(--tx-secondary)] bg-[var(--bz-base)]",
              )}
            />
            <div>
              {entry.date ? (
                <p
                  className={cn(
                    "leading-tight text-[var(--tx-pure)]",
                    latest ? "text-[26px]" : "text-[19px]",
                  )}
                  style={{ ...SERIF, ...TABULAR }}
                >
                  {formatDate(entry.date)}
                </p>
              ) : (
                <p className="text-[13px] text-[var(--tx-secondary)]">
                  Date not recorded
                </p>
              )}
              <p className="mt-1 text-[12px] text-[var(--tx-secondary)]">
                {entry.kind}
              </p>
              {latest ? (
                <p className="mt-1 text-[12px] font-medium text-[var(--tx-pure)]">
                  Latest milestone
                </p>
              ) : null}
            </div>
            <div className="min-w-0 pt-1 sm:pt-0">
              <p
                className={cn(
                  "text-[15px] text-[var(--tx-pure)]",
                  latest ? "font-semibold" : "font-medium",
                )}
              >
                {entry.title}
              </p>
              <p className="mt-1 text-[12.5px] text-[var(--tx-secondary)]">
                {entry.body}
              </p>
              {entry.refText ? (
                <p
                  className="mt-1 text-[12px] text-[var(--tx-secondary)]"
                  style={TABULAR}
                >
                  {entry.refText}
                </p>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
