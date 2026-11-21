"use client";

/**
 * What was confirmed, linked or ignored today (WITA). A confirmation of today can be undone
 * (owner rule b): two steps, the second says what goes back and what does not.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { logger } from "@/lib/logger";
import { Money } from "@balizero/core";
import {
  accountingApi,
  type DoneTodayItem,
  type DoneTodayResponse,
} from "@/lib/api/workspace/accounting.api";
import { NO_INVOICE_NUMBER } from "./CandidateIdentity";

export type DoneTodayStatus = "loading" | "ok" | "failed";

const isDoneToday = (body: unknown): body is DoneTodayResponse => {
  const b = body as Partial<DoneTodayResponse> | null | undefined;
  return Array.isArray(b?.items) && typeof b?.counts?.total === "number";
};

const isUnavailable = (err: unknown): boolean =>
  typeof err === "object" &&
  err !== null &&
  (err as { statusCode?: unknown }).statusCode === 404;

/** Owns the load, the data, the status, "older backend" and the retry. */
export function useDoneToday(version: number) {
  const [data, setData] = useState<DoneTodayResponse | null>(null);
  const [status, setStatus] = useState<DoneTodayStatus>("loading");
  const [unavailable, setUnavailable] = useState(false);
  // Token of the latest load: an older one answering late changes nothing.
  const loadReq = useRef(0);
  const unavailableRef = useRef(false);

  const load = useCallback(async () => {
    const token = ++loadReq.current;
    setStatus("loading");
    try {
      const res = await accountingApi.getDoneToday();
      if (!isDoneToday(res)) throw new Error("done-today body malformed");
      if (loadReq.current !== token) return;
      setData(res);
      setStatus("ok");
    } catch (err) {
      logger.error("accounting done-today load failed", {}, err as Error);
      if (loadReq.current !== token) return;
      if (isUnavailable(err)) {
        unavailableRef.current = true;
        setUnavailable(true);
      } else {
        setStatus("failed");
      }
    }
  }, []);

  useEffect(() => {
    if (!unavailableRef.current) void load();
  }, [version, load]);

  return { data, status, unavailable, retry: load };
}

const LABELS = new Map<string, { text: string; tone: string }>([
  ["confirmed", { text: "Confirmed", tone: "text-[var(--state-success)]" }],
  ["linked", { text: "Linked", tone: "text-[var(--state-info)]" }],
  ["ignored", { text: "Ignored", tone: "text-[var(--bz-text-2)]" }],
]);
const FALLBACK_LABEL = { text: "Done", tone: "text-[var(--bz-text-2)]" };

const timeOf = (doneAt: unknown): string | null => {
  const m = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(String(doneAt));
  return m ? m[1] : null;
};

const filled = (v: unknown): v is string =>
  typeof v === "string" && v.trim() !== "";

/** Undo for one confirmed entry; absent when the page gives no way to undo. */
export interface DoneTodayUndo {
  onUndo: (txnId: number) => void;
  locked: boolean;
  error: { txnId: number; message: string } | null;
}

function UndoControl({
  item,
  undo,
}: {
  item: DoneTodayItem;
  undo: DoneTodayUndo;
}) {
  const [asking, setAsking] = useState(false);
  const failed = undo.error?.txnId === item.txn_id ? undo.error.message : null;
  if (!asking) {
    return (
      <div className="mt-1">
        <button
          type="button"
          data-testid={`done-today-undo-${item.txn_id}`}
          disabled={undo.locked}
          onClick={() => setAsking(true)}
          className="text-xs font-semibold underline underline-offset-4 disabled:opacity-50"
        >
          Undo
        </button>
        {failed && (
          <p role="alert" className="text-xs text-[var(--state-danger)]">
            {failed}
          </p>
        )}
      </div>
    );
  }
  return (
    <div
      role="group"
      aria-label="Undo this confirmation"
      className="mt-2 space-y-2 rounded-md border border-[var(--state-danger)] p-2 text-xs"
    >
      <p>
        The transfer goes back to the inbox, and the payment, the cash-out row
        and the invoice&apos;s paid amount are taken back. A case this payment
        started goes back to waiting for payment.
      </p>
      <p>
        If this confirmation emailed the client that the payment arrived, that
        email is not taken back.
      </p>
      <div className="flex gap-3">
        <button
          type="button"
          data-testid={`done-today-undo-confirm-${item.txn_id}`}
          disabled={undo.locked}
          onClick={() => {
            setAsking(false);
            undo.onUndo(item.txn_id);
          }}
          className="rounded-md border border-[var(--state-danger)] px-2 py-1 font-semibold disabled:opacity-50"
        >
          Undo confirmation
        </button>
        <button
          type="button"
          onClick={() => setAsking(false)}
          className="px-2 py-1 underline underline-offset-4"
        >
          Keep it
        </button>
      </div>
    </div>
  );
}

function DoneTodayEntry({
  item,
  undo,
}: {
  item: DoneTodayItem;
  undo?: DoneTodayUndo;
}) {
  const time = timeOf(item.done_at);
  const label = LABELS.get(item.action) ?? FALLBACK_LABEL;
  const shows = item.action === "confirmed" || item.action === "linked";
  return (
    <li
      data-testid={`done-today-item-${item.txn_id}`}
      className="rounded-md border p-2"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">
            {item.description ? item.description : "(no memo)"}
          </p>
          <p className="text-xs text-[var(--bz-text-2)] tabular-nums">
            {time !== null && <span>{time} · </span>}
            <span className={label.tone}>{label.text}</span> · {item.txn_date}
          </p>
          {shows && (
            <p className="text-xs text-[var(--bz-text-2)]">
              {filled(item.invoice_number)
                ? item.invoice_number
                : NO_INVOICE_NUMBER}
              {filled(item.client_name) && <> · {item.client_name}</>}
            </p>
          )}
        </div>
        <span className="shrink-0 text-sm tabular-nums">
          <Money value={item.amount_idr} />
        </span>
      </div>
      {undo && item.action === "confirmed" && (
        <UndoControl item={item} undo={undo} />
      )}
    </li>
  );
}

/** The body of the section: loading, failure, or the day's entries. */
export function DoneTodayList({
  data,
  status,
  onRetry,
  undo,
}: {
  data: DoneTodayResponse | null;
  status: DoneTodayStatus;
  onRetry: () => void;
  undo?: DoneTodayUndo;
}) {
  if (data === null && status === "failed") {
    return (
      <div className="text-center text-sm text-[var(--bz-text-2)]">
        <p>Could not load what was done today.</p>
        <button
          type="button"
          className="mt-2 rounded-md border border-[var(--bz-border)] px-3 py-1 text-sm text-[var(--bz-text-1)] hover:bg-[var(--bz-card-hover)]/30"
          onClick={() => onRetry()}
        >
          Try again
        </button>
      </div>
    );
  }
  if (data === null) {
    return (
      <div className="flex items-center justify-center py-3 text-[var(--bz-text-2)]">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-xs text-[var(--bz-text-2)]">
        Today, {data.day} (WITA)
      </p>
      {data.items.length === 0 ? (
        <p className="text-center text-sm text-[var(--bz-text-2)]">
          Nothing done yet today.
        </p>
      ) : (
        <ul className="space-y-2">
          {data.items.map((item) => (
            <DoneTodayEntry key={item.txn_id} item={item} undo={undo} />
          ))}
        </ul>
      )}
      {data.truncated && (
        <p className="text-xs text-[var(--bz-text-2)]">
          Showing the latest {data.items.length} of {data.counts.total}.
        </p>
      )}
    </div>
  );
}
