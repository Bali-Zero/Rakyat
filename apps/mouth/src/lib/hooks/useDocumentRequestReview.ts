"use client";

/**
 * The reviewers' page of the document requests: the two lists, the decision and the reopen.
 *
 * `decided` is asked only after `pending` answered 200, so a visitor who is not a card reviewer
 * causes ONE 403 (each 403 is sent to the alert service), not two. After a decision or a reopen
 * both lists are loaded again.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  DocumentRequestDecisionBody,
  DocumentRequestDecisionResult,
  DocumentRequestReopenResult,
  DocumentRequestReviewList,
} from "@/lib/types/document-requests";

const BASE = "/api/crm/practices/document-requests";

function statusOf(err: unknown): number | undefined {
  return (err as { status?: number } | null)?.status;
}

export function useDocumentRequestReview() {
  const [pending, setPending] = useState<DocumentRequestReviewList | null>(
    null,
  );
  const [decided, setDecided] = useState<DocumentRequestReviewList | null>(
    null,
  );
  const [forbidden, setForbidden] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const pendingList = await api.get<DocumentRequestReviewList>(
        `${BASE}/review?state=pending&limit=50`,
      );
      setForbidden(false);
      setPending(pendingList);
      const decidedList = await api.get<DocumentRequestReviewList>(
        `${BASE}/review?state=decided&limit=20`,
      );
      setDecided(decidedList);
    } catch (err) {
      if (statusOf(err) === 403) {
        setForbidden(true);
      } else {
        setError(
          err instanceof Error ? err.message : "Failed to load the requests",
        );
      }
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const decide = useCallback(
    async (id: number, body: DocumentRequestDecisionBody) => {
      const result = await api.post<DocumentRequestDecisionResult>(
        `${BASE}/${id}/decision`,
        body,
      );
      await load();
      return result;
    },
    [load],
  );

  const reopen = useCallback(
    async (id: number) => {
      const result = await api.post<DocumentRequestReopenResult>(
        `${BASE}/${id}/reopen`,
      );
      await load();
      return result;
    },
    [load],
  );

  return {
    pending,
    decided,
    forbidden,
    isLoading,
    error,
    decide,
    reopen,
    reload: load,
  };
}
