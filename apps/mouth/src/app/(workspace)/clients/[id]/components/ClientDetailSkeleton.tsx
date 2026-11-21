import React from "react";
import styles from "../client-detail-desk.module.css";

/**
 * The page's own shape while the client profile loads: masthead, status
 * column, tab strip and the Overview's first cards, each its own skeleton,
 * laid out with the same desk grid as the loaded page so nothing jumps when
 * the data lands. Replaces a single centred spinner.
 *
 * The tab strip is a plain row of bars, NOT a <nav>: no navigation exists
 * until the profile says which tabs this client has.
 */

function Bar({ w, h = 16 }: { w: string; h?: number }) {
  return (
    <div
      className="rounded bg-[var(--bz-border)] motion-safe:animate-pulse"
      style={{ width: w, height: h }}
    />
  );
}

function CardSkeleton({ testId, lines }: { testId: string; lines: string[] }) {
  return (
    <section
      data-testid={testId}
      className="space-y-3 rounded-lg border border-[var(--bz-border)] p-4"
    >
      <Bar w="35%" h={12} />
      {lines.map((w, i) => (
        <Bar key={i} w={w} />
      ))}
    </section>
  );
}

export function ClientDetailSkeleton() {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-label="Loading client"
      data-testid="client-detail-skeleton"
      className="space-y-6"
    >
      <div data-testid="skeleton-masthead" className="flex items-center gap-4">
        <div className="h-14 w-14 rounded-full bg-[var(--bz-border)] motion-safe:animate-pulse" />
        <div className="flex-1 space-y-2">
          <Bar w="18%" h={12} />
          <Bar w="40%" h={28} />
          <Bar w="28%" />
        </div>
      </div>

      <div className={styles.detailLayout}>
        <div className={styles.statusColumn}>
          <CardSkeleton
            testId="skeleton-status"
            lines={["80%", "65%", "72%", "50%"]}
          />
        </div>
        <div className={styles.mainColumn}>
          <div data-testid="skeleton-tabs" className="mb-4 flex gap-3">
            {["12%", "14%", "10%", "12%", "11%"].map((w, i) => (
              <Bar key={i} w={w} h={28} />
            ))}
          </div>
          <div className="space-y-4">
            <CardSkeleton
              testId="skeleton-client-info"
              lines={["70%", "55%", "62%"]}
            />
            <CardSkeleton testId="skeleton-passport" lines={["60%", "45%"]} />
            <CardSkeleton
              testId="skeleton-documents"
              lines={["75%", "68%", "52%"]}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
