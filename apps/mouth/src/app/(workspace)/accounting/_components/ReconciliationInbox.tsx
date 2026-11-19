"use client";

/**
 * Reconciliation inbox: unmatched incoming transfers, each with a light.
 *
 * A light is advice, never an action: rendering sends only the inbox GET, and
 * every write (confirm, link, ignore, reopen) waits for a click. Confirm is the
 * single writer of payment_status; Link and Ignore move no money.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Inbox } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { logger } from "@/lib/logger";
import { Money } from "@balizero/core";
import {
  accountingApi,
  type BankTransaction,
  type MatchCandidate,
  type MatchCandidatesResponse,
  type ReconciliationInboxItem,
} from "@/lib/api/workspace/accounting.api";
import { BankStatementUpload } from "./BankStatementUpload";
import { CandidateIdentity } from "./CandidateIdentity";
import { ConfirmModal } from "./ConfirmModal";
import { LinkedIgnoredSection } from "./LinkedIgnoredSection";
import { useWriteLock } from "./useWriteLock";

type Light = "green" | "recorded" | "yellow" | "red";
type Filter = "all" | Light;

const LIGHT_LABEL: Record<Light, string> = {
  green: "Match found",
  recorded: "Already recorded",
  yellow: "To check",
  red: "No invoice",
};

const LIGHT_TONE: Record<Light, string> = {
  green: "text-[var(--state-success)]",
  recorded: "text-[var(--bz-accent)]",
  yellow: "text-[var(--state-warning)]",
  red: "text-[var(--state-danger)]",
};

const FILTERS: Array<{ key: Filter; label: string }> = [
  { key: "all", label: "All" },
  { key: "green", label: LIGHT_LABEL.green },
  { key: "recorded", label: LIGHT_LABEL.recorded },
  { key: "yellow", label: LIGHT_LABEL.yellow },
  { key: "red", label: LIGHT_LABEL.red },
];

/** An unknown light value is shown as "To check". */
const normalizeLight = (light: string): Light =>
  light === "green" || light === "recorded" || light === "red"
    ? light
    : "yellow";

const MIN_REASON = 3;
const MAX_REASON = 200;

/** The server's rule: collapse whitespace, count code points. */
const normalizeReason = (raw: string): string =>
  raw.trim().replace(/\s+/g, " ");

function LightChip({ light }: { light: Light }) {
  return (
    <span
      className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${LIGHT_TONE[light]}`}
    >
      {LIGHT_LABEL[light]}
    </span>
  );
}

function CandidateRow({
  candidate,
  suggested,
  actionLabel,
  disabled,
  onAction,
}: {
  candidate: MatchCandidate;
  suggested: boolean;
  actionLabel: string;
  disabled: boolean;
  onAction: () => void;
}) {
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border bg-[var(--bz-card)] p-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">
          {candidate.client_name}
          {suggested && (
            <span className="ml-2 rounded bg-[var(--bz-accent)]/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-[var(--bz-accent)]">
              Suggested
            </span>
          )}
        </p>
        <CandidateIdentity candidate={candidate} />
        <p className="text-xs text-[var(--bz-text-2)]">
          <Money value={candidate.invoice_amount_idr} /> ·{" "}
          {(candidate.confidence * 100).toFixed(0)}% ·{" "}
          {candidate.reasons[0] ?? ""}
        </p>
      </div>
      <Button size="sm" onClick={onAction} disabled={disabled}>
        {actionLabel}
      </Button>
    </li>
  );
}

/** One group of candidates; never looks complete when the server counted more. */
function CandidateGroup({
  title,
  rows,
  count,
  renderRow,
}: {
  title: string;
  rows: MatchCandidate[];
  count: number;
  renderRow: (c: MatchCandidate) => React.ReactNode;
}) {
  const truncated = count > rows.length;
  if (rows.length === 0 && !truncated) return null;
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold uppercase text-[var(--bz-text-2)]">
        {title}
      </h4>
      {rows.length > 0 && <ul className="space-y-2">{rows.map(renderRow)}</ul>}
      {truncated && (
        <p className="mt-1 text-xs text-[var(--bz-text-2)]">
          {count} invoices have this amount; the {rows.length} closest are
          shown.
        </p>
      )}
    </div>
  );
}

export function ReconciliationInbox({
  onConfirmed,
}: {
  onConfirmed: () => void;
}) {
  const { error, success } = useToast();
  const [items, setItems] = useState<ReconciliationInboxItem[]>([]);
  // Status of the LATEST STARTED load. With no rows on screen it decides the text:
  // loading line, empty message (ok) or failure (failed); rows are never replaced.
  const [loadStatus, setLoadStatus] = useState<"loading" | "ok" | "failed">(
    "loading",
  );
  const [filter, setFilter] = useState<Filter>("all");
  const [expandedTxn, setExpandedTxn] = useState<number | null>(null);
  const [candidates, setCandidates] = useState<MatchCandidatesResponse | null>(
    null,
  );
  const [candLoading, setCandLoading] = useState(false);
  const [modal, setModal] = useState<{
    txn: Pick<
      BankTransaction,
      "id" | "amount_idr" | "description" | "txn_date"
    >;
    candidate: MatchCandidate;
  } | null>(null);
  // The ignore form's truth is a ref (mirrored to state for rendering): no
  // handler decides on a value captured at render time.
  const ignoreRef = useRef<{ txnId: number; reason: string } | null>(null);
  const [ignoreForm, setIgnoreFormState] = useState<{
    txnId: number;
    reason: string;
  } | null>(null);
  const setIgnoreForm = (v: { txnId: number; reason: string } | null) => {
    ignoreRef.current = v;
    setIgnoreFormState(v);
  };
  // ONE lock for the whole tab: the inbox, the card and "Linked and ignored" share it.
  const lock = useWriteLock();
  const [candFailed, setCandFailed] = useState(false);
  // Token of the latest candidates request: a late answer for another one is dropped.
  const candReq = useRef(0);
  const [rowError, setRowError] = useState<{
    txnId: number;
    message: string;
  } | null>(null);
  // Bumped after every write so the "Linked and ignored" list reloads too.
  const [version, setVersion] = useState(0);

  // Token of the latest reload: an older one answering late never replaces the list.
  const loadReq = useRef(0);

  const load = useCallback(async () => {
    const token = ++loadReq.current;
    setLoadStatus("loading");
    try {
      const rows = await accountingApi.getReconciliationInbox();
      if (loadReq.current !== token) return;
      setItems(rows);
      setLoadStatus("ok");
    } catch (err) {
      if (loadReq.current !== token) return;
      setLoadStatus("failed");
      error("Failed to load reconciliation inbox", "Please try again");
      logger.error("accounting inbox load failed", {}, err as Error);
    }
  }, [error]);

  useEffect(() => {
    load();
  }, [load]);

  const afterWrite = (txnId: number) => {
    // The acted row leaves the list at once; the reload then replaces the list.
    setItems((cur) => cur.filter((it) => it.txn_id !== txnId));
    setExpandedTxn((cur) => (cur === txnId ? null : cur));
    // only the acted row's form closes; another row's keeps its text
    if (ignoreRef.current?.txnId === txnId) setIgnoreForm(null);
    setVersion((v) => v + 1);
    void load();
    onConfirmed();
  };

  const runAction = (
    txnId: number,
    title: string,
    run: () => Promise<unknown>,
  ) =>
    lock.runWrite(async () => {
      setRowError(null);
      try {
        await run();
        success(title);
        afterWrite(txnId);
      } catch (err) {
        const detail =
          (err instanceof Error && err.message) || "Please try again";
        setRowError({ txnId, message: detail });
        error("Action failed", detail);
        logger.error(
          `accounting ${title} failed txn=${txnId}`,
          {},
          err as Error,
        );
      }
    });

  const openCandidates = async (item: ReconciliationInboxItem) => {
    const token = ++candReq.current;
    if (expandedTxn === item.txn_id) {
      setExpandedTxn(null);
      return;
    }
    setExpandedTxn(item.txn_id);
    setCandidates(null);
    setCandFailed(false);
    setCandLoading(true);
    try {
      const res = await accountingApi.getMatchCandidates(item.txn_id);
      // Only the latest request, and only an answer for this very transfer.
      if (candReq.current !== token || res.txn_id !== item.txn_id) return;
      setCandidates(res);
    } catch (err) {
      if (candReq.current !== token) return;
      setCandFailed(true);
      error("Failed to load candidates", "Please try again");
      logger.error(
        `accounting candidates failed txn=${item.txn_id}`,
        {},
        err as Error,
      );
    } finally {
      if (candReq.current === token) setCandLoading(false);
    }
  };

  // Opening and closing the form go through the lock like every other control.
  const openIgnore = (txnId: number) => {
    if (!lock.isFree()) return;
    if (ignoreRef.current?.txnId === txnId) return;
    setRowError(null);
    setIgnoreForm({ txnId, reason: "" });
  };
  const closeIgnore = (txnId: number) => {
    if (!lock.isFree()) return;
    if (ignoreRef.current?.txnId !== txnId) return;
    setIgnoreForm(null);
  };
  const setIgnoreReason = (txnId: number, text: string) => {
    const cur = ignoreRef.current;
    if (!cur || cur.txnId !== txnId) return;
    setIgnoreForm({ ...cur, reason: text });
  };
  // Reads the ref at click time: the reason validated and sent is the ref's.
  const sendIgnore = (txnId: number) => {
    const cur = ignoreRef.current;
    if (!cur || cur.txnId !== txnId) return;
    const text = normalizeReason(cur.reason);
    const len = Array.from(text).length;
    if (len < MIN_REASON || len > MAX_REASON) return;
    void runAction(txnId, "Transfer ignored", () =>
      accountingApi.ignoreTransaction({ bank_txn_id: txnId, reason: text }),
    );
  };

  const counts: Record<Filter, number> = {
    all: items.length,
    green: 0,
    recorded: 0,
    yellow: 0,
    red: 0,
  };
  for (const it of items) counts[normalizeLight(it.light)] += 1;
  const visible = items.filter(
    (it) => filter === "all" || normalizeLight(it.light) === filter,
  );
  const reasonLength = Array.from(
    normalizeReason(ignoreForm?.reason ?? ""),
  ).length;
  const reasonOk = reasonLength >= MIN_REASON && reasonLength <= MAX_REASON;

  // The upload card and the linked/ignored list stay mounted while the list reloads.
  const frame = (content: React.ReactNode) => (
    <div className="space-y-4">
      <BankStatementUpload
        locked={lock.locked}
        exclusive={lock.runWrite}
        onImported={() => {
          void load();
          onConfirmed();
        }}
      />
      {content}
      {modal && (
        <ConfirmModal
          txn={modal.txn}
          candidate={modal.candidate}
          onClose={() => {
            if (lock.tryCloseDialog()) setModal(null);
          }}
          exclusive={lock.runDialogWrite}
          onConfirmed={() => {
            lock.dialogDone();
            setModal(null);
            afterWrite(modal.txn.id);
          }}
        />
      )}
      <LinkedIgnoredSection
        version={version}
        locked={lock.locked}
        exclusive={lock.runWrite}
        onReopened={() => {
          // the section refreshed itself at the reopen: no version bump here
          void load();
          onConfirmed();
        }}
      />
    </div>
  );

  if (items.length === 0 && loadStatus === "loading") {
    return frame(
      <div className="flex items-center justify-center p-10 text-[var(--bz-text-2)]">
        <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading inbox…
      </div>,
    );
  }

  if (items.length === 0 && loadStatus === "failed") {
    return frame(
      <div className="rounded-lg border p-10 text-center text-[var(--bz-text-2)]">
        <p>Could not load the inbox.</p>
        <Button
          className="mt-3"
          variant="outline"
          size="sm"
          onClick={() => void load()}
        >
          Try again
        </Button>
      </div>,
    );
  }

  if (items.length === 0) {
    return frame(
      <div className="rounded-lg border p-10 text-center text-[var(--bz-text-2)]">
        <Inbox className="mx-auto mb-2 h-8 w-8 opacity-50" />
        No unmatched incoming transfers. Inbox zero.
      </div>,
    );
  }

  return frame(
    <>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            aria-pressed={filter === f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded-full border px-3 py-1 text-xs font-medium ${
              filter === f.key
                ? "border-[var(--bz-accent)] text-[var(--bz-text-1)]"
                : "text-[var(--bz-text-2)] hover:text-[var(--bz-text-1)]"
            }`}
          >
            {f.label} ({counts[f.key]})
          </button>
        ))}
      </div>

      {visible.length === 0 && (
        <p className="rounded-lg border p-6 text-center text-sm text-[var(--bz-text-2)]">
          No transfers in this filter.
        </p>
      )}

      <div className="space-y-2">
        {visible.map((item) => {
          const light = normalizeLight(item.light);
          const txn = {
            id: item.txn_id,
            amount_idr: item.amount_idr,
            description: item.description,
            txn_date: item.txn_date,
          };
          const suggestedId = item.suggestion?.invoice_id;
          return (
            <div
              key={item.txn_id}
              className="rounded-lg border"
              data-testid={`inbox-row-${item.txn_id}`}
            >
              <div className="flex items-center gap-2 pr-3">
                <button
                  onClick={() => openCandidates(item)}
                  aria-expanded={expandedTxn === item.txn_id}
                  className="flex min-w-0 flex-1 items-center justify-between gap-3 p-3 text-left hover:bg-[var(--bz-card-hover)]/30"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {item.description || "(no memo)"}
                    </p>
                    <p className="text-xs text-[var(--bz-text-2)] tabular-nums">
                      {item.txn_date}
                    </p>
                  </div>
                  <LightChip light={light} />
                  <span className="shrink-0 font-semibold tabular-nums text-[var(--state-success)]">
                    <Money value={item.amount_idr} />
                  </span>
                </button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={lock.locked}
                  onClick={() => openIgnore(item.txn_id)}
                >
                  Ignore
                </Button>
              </div>

              {ignoreForm?.txnId === item.txn_id && (
                <div className="flex flex-wrap items-center gap-2 border-t bg-[var(--bz-surface)]/20 p-3">
                  <input
                    type="text"
                    aria-label="Reason for ignoring"
                    placeholder="Why is this not a client payment?"
                    value={ignoreForm.reason}
                    disabled={lock.locked}
                    onChange={(e) =>
                      setIgnoreReason(item.txn_id, e.target.value)
                    }
                    className="min-w-[240px] flex-1 rounded-md border bg-[var(--bz-base)] px-2 py-1 text-sm"
                  />
                  <p className="basis-full text-xs text-[var(--bz-text-2)]">
                    Ignoring moves no money. The transfer can be reopened under
                    Linked and ignored.
                  </p>
                  <Button
                    size="sm"
                    disabled={lock.locked || !reasonOk}
                    onClick={() => sendIgnore(item.txn_id)}
                  >
                    Ignore transfer
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={lock.locked}
                    onClick={() => closeIgnore(item.txn_id)}
                  >
                    Cancel
                  </Button>
                </div>
              )}

              {rowError?.txnId === item.txn_id && (
                <p
                  role="alert"
                  className="border-t px-3 py-2 text-sm text-[var(--state-danger)]"
                >
                  {rowError.message}
                </p>
              )}

              {expandedTxn === item.txn_id && (
                <div className="space-y-3 border-t bg-[var(--bz-surface)]/20 p-3">
                  {candFailed ? (
                    <p className="py-2 text-center text-sm text-[var(--state-danger)]">
                      Could not load the invoices for this transfer. Close the
                      row and open it again.
                    </p>
                  ) : candLoading || !candidates ? (
                    <div className="flex items-center justify-center py-4 text-[var(--bz-text-2)]">
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Ranking
                      candidates…
                    </div>
                  ) : (
                    <>
                      {light === "recorded" && (
                        <p className="text-sm text-[var(--bz-accent)]">
                          This payment looks already recorded. Link it instead
                          of confirming it again.
                        </p>
                      )}
                      {candidates.candidates.length === 0 &&
                        candidates.recorded_candidates.length === 0 &&
                        item.open_candidates <= 0 &&
                        item.recorded_candidates <= 0 && (
                          <p className="py-2 text-center text-sm text-[var(--bz-text-2)]">
                            No plausible invoice match. Leave unmatched or
                            handle manually.
                          </p>
                        )}
                      <CandidateGroup
                        title="Open invoices"
                        rows={candidates.candidates}
                        count={item.open_candidates}
                        renderRow={(c) => (
                          <CandidateRow
                            key={c.invoice_id}
                            candidate={c}
                            suggested={
                              light === "green" && c.invoice_id === suggestedId
                            }
                            actionLabel="Match"
                            disabled={lock.locked}
                            onAction={() => {
                              if (lock.tryOpenDialog())
                                setModal({ txn, candidate: c });
                            }}
                          />
                        )}
                      />
                      <CandidateGroup
                        title="Already paid"
                        rows={candidates.recorded_candidates}
                        count={item.recorded_candidates}
                        renderRow={(c) => (
                          <CandidateRow
                            key={c.invoice_id}
                            candidate={c}
                            suggested={
                              light === "recorded" &&
                              c.invoice_id === suggestedId
                            }
                            actionLabel="Link, no money moved"
                            disabled={lock.locked}
                            onAction={() =>
                              runAction(item.txn_id, "Transfer linked", () =>
                                accountingApi.linkRecorded({
                                  bank_txn_id: item.txn_id,
                                  practice_id: c.practice_id,
                                  invoice_id: c.invoice_id,
                                }),
                              )
                            }
                          />
                        )}
                      />
                    </>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </>,
  );
}
