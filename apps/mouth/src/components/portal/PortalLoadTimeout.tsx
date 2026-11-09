"use client";

import React, { useCallback, useEffect, useState } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

/** How long a portal skeleton may show before we admit the load is stuck. */
export const PORTAL_LOAD_TIMEOUT_MS = 3000;

/**
 * True once `isLoading` has stayed true for `ms`. Goes back to false when
 * loading ends; `retry()` starts a fresh window.
 */
export function usePortalLoadTimeout(
  isLoading: boolean,
  ms: number = PORTAL_LOAD_TIMEOUT_MS,
): { timedOut: boolean; retry: () => void } {
  const [expiredAttempt, setExpiredAttempt] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [wasLoading, setWasLoading] = useState(isLoading);

  // Every new loading phase gets its own window, so an old expiry never
  // carries over to the next load.
  if (isLoading !== wasLoading) {
    setWasLoading(isLoading);
    if (isLoading) setAttempt((a) => a + 1);
  }

  useEffect(() => {
    if (!isLoading) return;
    const id = setTimeout(() => setExpiredAttempt(attempt), ms);
    return () => clearTimeout(id);
  }, [isLoading, ms, attempt]);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  return { timedOut: isLoading && expiredAttempt === attempt, retry };
}

/**
 * Shown in place of a portal skeleton once the load has timed out.
 *
 * Retry restarts every portal query still fetching with no data. A plain
 * `refetch()` would not: React Query joins the request already in flight,
 * which is the one that hung.
 */
export function PortalLoadTimeout({
  onRetry,
  title = "This is taking longer than expected",
}: {
  onRetry: () => void;
  title?: string;
}) {
  const queryClient = useQueryClient();
  const retry = () => {
    onRetry();
    void queryClient.resetQueries({
      queryKey: ["portal"],
      predicate: (q) =>
        q.state.status === "pending" && q.state.fetchStatus !== "idle",
    });
  };

  return (
    <div className="space-y-4">
      <Alert variant="destructive">
        <AlertTriangle className="h-4 w-4" />
        <AlertTitle>{title}</AlertTitle>
        <AlertDescription>
          We couldn&apos;t load this page yet. Check your connection and try
          again.
        </AlertDescription>
      </Alert>
      <Button type="button" onClick={retry}>
        <RefreshCw className="mr-2 h-4 w-4" />
        Retry
      </Button>
    </div>
  );
}
