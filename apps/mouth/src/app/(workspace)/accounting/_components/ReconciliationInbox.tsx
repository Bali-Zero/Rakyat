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
import { ConfirmModal } from "./ConfirmModal";
import { LinkedIgnoredSection } from "./LinkedIgnoredSection";

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
    <li className="flex items-center justify-between gap-3 rounded-md border bg-card p-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">
          {candidate.client_name}
          {suggested && (
            <span className="ml-2 rounded bg-[var(--bz-accent)]/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-[var(--bz-accent)]">
              Suggested
            </span>
          )}
        </p>
        <p className="text-xs text-muted-foreground">
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
      <h4 className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
        {title}
      </h4>
      {rows.length > 0 && <ul className="space-y-2">{rows.map(renderRow)}</ul>}
      {truncated && (
        <p className="mt-1 text-xs text-muted-foreground">
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
  const [loading, setLoading] = useState(true);
  // The full-list spinner is for the first load only; later reloads run in the background.
  const [hasLoaded, setHasLoaded] = useState(false);
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
  const [ignoreFor, setIgnoreFor] = useState<number | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
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
    try {
      setLoading(true);
      const rows = await accountingApi.getReconciliationInbox();
      if (loadReq.current !== token) return;
      setItems(rows);
    } catch (err) {
      if (loadReq.current !== token) return;
      error("Failed to load reconciliation inbox", "Please try again");
      logger.error("accounting inbox load failed", {}, err as Error);
    } finally {
      if (loadReq.current === token) {
        setLoading(false);
        setHasLoaded(true);
      }
    }
  }, [error]);

  useEffect(() => {
    load();
  }, [load]);

  const afterWrite = (txnId: number) => {
    // The acted row leaves the list at once; the reload then replaces the list.
    setItems((cur) => cur.filter((it) => it.txn_id !== txnId));
    setExpandedTxn(null);
    setIgnoreFor(null);
    setReason("");
    setVersion((v) => v + 1);
    void load();
    onConfirmed();
  };

  // One write at a time; the ref closes the gap before React re-renders.
  const runAction = async (
    txnId: number,
    title: string,
    run: () => Promise<unknown>,
  ) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
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
      logger.error(`accounting ${title} failed txn=${txnId}`, {}, err as Error);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const openCandidates = async (item: ReconciliationInboxItem) => {
    const token = ++candReq.current;
    if (expandedTxn === item.txn_id) {
      setExpandedTxn(null);
      return;
    }
    setExpandedTxn(item.txn_id);
    setCandidates(null);
    setCandLoading(true);
    try {
      const res = await accountingApi.getMatchCandidates(item.txn_id);
      // Only the latest request, and only an answer for this very transfer.
      if (candReq.current !== token || res.txn_id !== item.txn_id) return;
      setCandidates(res);
    } catch (err) {
      if (candReq.current !== token) return;
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

  const toggleIgnore = (txnId: number) => {
    setRowError(null);
    setReason("");
    setIgnoreFor((cur) => (cur === txnId ? null : txnId));
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
  const trimmed = reason.trim();
  const reasonOk = trimmed.length >= MIN_REASON && trimmed.length <= MAX_REASON;

  // The upload card and the linked/ignored list stay mounted while the list reloads.
  const frame = (content: React.ReactNode) => (
    <div className="space-y-4">
      <BankStatementUpload
        onImported={() => {
          void load();
          onConfirmed();
        }}
      />
      {content}
      <LinkedIgnoredSection
        version={version}
        onReopened={() => {
          setVersion((v) => v + 1);
          void load();
          onConfirmed();
        }}
      />
    </div>
  );

  if (loading && !hasLoaded) {
    return frame(
      <div className="flex items-center justify-center p-10 text-muted-foreground">
        <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading inbox…
      </div>,
    );
  }

  if (items.length === 0) {
    return frame(
      <div className="rounded-lg border p-10 text-center text-muted-foreground">
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
                ? "border-[var(--bz-accent)] text-foreground"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {f.label} ({counts[f.key]})
          </button>
        ))}
      </div>

      {visible.length === 0 && (
        <p className="rounded-lg border p-6 text-center text-sm text-muted-foreground">
          No transfers in this filter.
        </p>
      )}

      <div className="space-y-2">
        {visible.map((item) => {
          const light = normalizeLight(item.light);
          const txn = {
            id: item.txn_id,
            amount_idr: item.amount_idr,
            description: item.description || null,
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
                  className="flex min-w-0 flex-1 items-center justify-between gap-3 p-3 text-left hover:bg-muted/30"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {item.description || "(no memo)"}
                    </p>
                    <p className="text-xs text-muted-foreground tabular-nums">
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
                  disabled={busy}
                  onClick={() => toggleIgnore(item.txn_id)}
                >
                  Ignore
                </Button>
              </div>

              {ignoreFor === item.txn_id && (
                <div className="flex flex-wrap items-center gap-2 border-t bg-muted/20 p-3">
                  <input
                    type="text"
                    aria-label="Reason for ignoring"
                    placeholder="Why is this not a client payment?"
                    maxLength={MAX_REASON}
                    value={reason}
                    disabled={busy}
                    onChange={(e) => setReason(e.target.value)}
                    className="min-w-[240px] flex-1 rounded-md border bg-background px-2 py-1 text-sm"
                  />
                  <Button
                    size="sm"
                    disabled={busy || !reasonOk}
                    onClick={() =>
                      runAction(item.txn_id, "Transfer ignored", () =>
                        accountingApi.ignoreTransaction({
                          bank_txn_id: item.txn_id,
                          reason: trimmed,
                        }),
                      )
                    }
                  >
                    Ignore transfer
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() => toggleIgnore(item.txn_id)}
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
                <div className="space-y-3 border-t bg-muted/20 p-3">
                  {candLoading || !candidates ? (
                    <div className="flex items-center justify-center py-4 text-muted-foreground">
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
                          <p className="py-2 text-center text-sm text-muted-foreground">
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
                            disabled={busy}
                            onAction={() => setModal({ txn, candidate: c })}
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
                            disabled={busy}
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

      {modal && (
        <ConfirmModal
          txn={modal.txn}
          candidate={modal.candidate}
          onClose={() => setModal(null)}
          onConfirmed={() => {
            setModal(null);
            afterWrite(modal.txn.id);
          }}
        />
      )}
    </>,
  );
}
