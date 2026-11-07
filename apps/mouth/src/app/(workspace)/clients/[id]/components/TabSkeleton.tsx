import React from "react";

/** The light placeholder a lazily loaded tab shows while its chunk arrives. */
export function TabSkeleton() {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-label="Loading"
      data-testid="tab-skeleton"
      className="space-y-3 py-4"
    >
      {[72, 56, 64].map((w) => (
        <div
          key={w}
          className="h-4 rounded bg-[var(--bz-border)] motion-safe:animate-pulse"
          style={{ width: `${w}%` }}
        />
      ))}
    </div>
  );
}
