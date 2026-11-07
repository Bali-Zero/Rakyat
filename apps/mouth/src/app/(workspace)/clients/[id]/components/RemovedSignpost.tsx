"use client";

import React from "react";
import { cn } from "@/lib/utils";
import { FOCUS } from "@/components/workspace/r19/tokens";

/**
 * "N removed or rejected files are in Documents": the one line a domain
 * surface (Immigration, Family, the Passport card, the Company vault) shows
 * when it drops a void document of its kind. It opens the Documents
 * "Removed or rejected" group, where every void document is listed once.
 */
export function RemovedSignpost({
  count,
  onOpen,
  className,
}: {
  count: number;
  onOpen?: () => void;
  className?: string;
}) {
  if (count <= 0) return null;
  const text = `${count} removed or rejected ${
    count === 1 ? "file is" : "files are"
  } in Documents`;
  return (
    <p
      className={cn("text-[12.5px] text-[var(--tx-secondary)]", className)}
      data-testid="removed-signpost"
    >
      {onOpen ? (
        <button
          type="button"
          onClick={onOpen}
          className={cn(
            "underline underline-offset-4 hover:text-[var(--tx-pure)]",
            FOCUS,
          )}
        >
          {text}
        </button>
      ) : (
        text
      )}
    </p>
  );
}
