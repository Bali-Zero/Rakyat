import React from "react";
import { cn } from "@/lib/utils";
import { SECTION_H2, SERIF_SECTION } from "./tokens";
import { OrdinalMargin } from "./OrdinalMargin";
import type { NumeralTone } from "./NumberedList";

/**
 * The numbered ledger section — TEPAT's section grammar. A 44px head
 * (`OrdinalMargin` + a Plex heading + the caller's `actions` pushed
 * right) over a border, and the caller's `children` as the body underneath.
 * This is the section-level counterpart to `NumberedList`'s per-row ordinal.
 */
export function LedgerSection({
  n,
  tone = "wait",
  title,
  actions,
  wrapActions = false,
  children,
  className,
}: {
  n: number;
  tone?: NumeralTone;
  title: React.ReactNode;
  actions?: React.ReactNode;
  /** Below sm the actions wrap under the title instead of widening the page. */
  wrapActions?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={className}>
      <div
        className={cn(
          "flex min-h-11 items-center gap-2.5 border-b border-[var(--bz-border)]",
          wrapActions && "max-sm:flex-wrap max-sm:py-2",
        )}
      >
        <OrdinalMargin n={n} tone={tone} />
        <h2 className={SECTION_H2} style={SERIF_SECTION}>
          {title}
        </h2>
        {actions ? (
          <div
            className={cn(
              "ml-auto flex shrink-0 items-center gap-2",
              wrapActions &&
                "max-sm:ml-0 max-sm:min-w-0 max-sm:shrink max-sm:basis-full max-sm:flex-wrap",
            )}
          >
            {actions}
          </div>
        ) : null}
      </div>
      {children}
    </section>
  );
}
