"use client";

/**
 * Outgoings — every debit of the imported statements, newest first, with what the bank's text
 * says (payee and purpose of a transfer, the e-billing code of a PNBP payment, the transfer a
 * fee belongs to). The accountant justifies a debit (category, optional case, note, and for a
 * cash withdrawal the recipient and a breakdown that sums to the debit) and can undo it the
 * same day (WITA). The PNBP debits can be justified in one go and that batch undone the same day;
 * a PNBP whose e-billing code was debited more than once is flagged and left to justify by hand.
 * Nothing here enters the cash-out.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Money } from "@balizero/core";
import { useToast } from "@/components/ui/toast";
import {
  accountingApi,
  type JustifyOutgoingRequest,
  type OutgoingItem,
  type OutgoingsResponse,
  type OutgoingState,
} from "@/lib/api/workspace/accounting.api";
import { logger } from "@/lib/logger";
import { ccShort } from "./ccFormat";
import { CATEGORY_LABELS, JustifyDialog } from "./JustifyDialog";
import { PNBP_BATCH_LIMIT } from "./pnbpBatch";

const ALL = "all";

function timeOf(postedAt: string | null): string {
  const m = postedAt ? /T(\d{2}:\d{2})/.exec(postedAt) : null;
  return m ? m[1] : "";
}

const WITA = "Asia/Makassar";
const dayInWita = (iso: string): string | null => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? null
    : new Intl.DateTimeFormat("en-CA", { timeZone: WITA }).format(d);
};
/** [day, HH:MM] of an ISO instant in Bali time, or null when it does not parse. */
const witaDayAndTime = (iso: string): [string, string, string] | null => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const time = new Intl.DateTimeFormat("en-GB", {
    timeZone: WITA,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(d);
  return [iso, dayInWita(iso) ?? "", time];
};
/** Undo is offered only for a justification made today, Bali time (the server decides too). */
const justifiedToday = (iso: string): boolean =>
  dayInWita(iso) === dayInWita(new Date().toISOString());

function JustificationBlock({ item }: { item: OutgoingItem }) {
  const j = item.justification;
  if (!j) return null;
  const when = witaDayAndTime(j.at);
  return (
    <div
      data-testid={`justification-${item.id}`}
      className="mt-1 space-y-0.5 text-xs text-[var(--bz-text-1)]"
    >
      <p className="font-semibold">
        {CATEGORY_LABELS[j.category] ?? j.category}
        {j.practice_id ? ` · case ${j.practice_id}` : ""}
        {j.recipient ? ` · to ${j.recipient}` : ""}
      </p>
      {j.note && <p>{j.note}</p>}
      {j.lines.length > 0 && (
        <ul className="list-disc pl-4">
          {j.lines.map((l, i) => (
            <li key={i}>
              <Money value={l.amount_idr} /> · {CATEGORY_LABELS[l.category]} ·{" "}
              {l.note}
              {l.practice_id ? ` · case ${l.practice_id}` : ""}
            </li>
          ))}
        </ul>
      )}
      <p className="text-[var(--bz-text-2)]">
        {j.follows_txn_id ? "With its transfer · " : ""}
        {j.batch_id ? "Justified together · " : ""}
        {j.by}
        {when ? ` · ${ccShort(when[1])} ${when[2]}` : ""}
      </p>
    </div>
  );
}

function KindButton({
  label,
  n,
  total,
  active,
  onClick,
}: {
  label: string;
  n: number;
  total: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`rounded-full border px-3 py-1.5 text-sm ${
        active
          ? "border-[var(--bz-text-1)] font-semibold text-[var(--bz-text-1)]"
          : "border-[var(--bz-border)] text-[var(--bz-text-2)] hover:text-[var(--bz-text-1)]"
      }`}
    >
      {label} · {n} · <Money compact value={total} />
    </button>
  );
}

export function OutgoingsView() {
  const { success, error } = useToast();
  const [data, setData] = useState<OutgoingsResponse | null>(null);
  const [status, setStatus] = useState<"loading" | "ok" | "failed">("loading");
  const [kind, setKind] = useState<string>(ALL);
  const [tab, setTab] = useState<OutgoingState>("to_justify");
  const [justifying, setJustifying] = useState<OutgoingItem | null>(null);
  const [undoError, setUndoError] = useState<{
    id: number;
    message: string;
  } | null>(null);
  const [undoing, setUndoing] = useState(false);
  const [confirmBatch, setConfirmBatch] = useState(false);
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [lastBatch, setLastBatch] = useState<{
    id: string;
    n: number;
    at: string;
  } | null>(null);
  // Token of the latest load: an older one answering late changes nothing.
  const loadReq = useRef(0);

  const load = useCallback(async () => {
    const token = ++loadReq.current;
    setStatus("loading");
    try {
      const res = await accountingApi.getOutgoings({ state: tab });
      if (loadReq.current !== token) return;
      setData(res);
      setStatus("ok");
    } catch (err) {
      logger.error("accounting outgoings load failed", {}, err as Error);
      if (loadReq.current !== token) return;
      setStatus("failed");
    }
  }, [tab]);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = useMemo(
    () =>
      (data?.items ?? []).filter(
        (i) => tab === "justified" || kind === ALL || i.kind === kind,
      ),
    [data, kind, tab],
  );

  // «Justify all PNBP»: the PNBP still to justify whose code no other debit carries, newest first.
  // Offered only where those rows are on screen: the All and PNBP filters (gate r1 on #616).
  const pnbpShown =
    tab === "to_justify" && (kind === ALL || kind === "government_fee");
  // The Undo of the set just justified, only on the Bali day it was made (the server decides too).
  const undoable = lastBatch && justifiedToday(lastBatch.at) ? lastBatch : null;
  const pnbp = useMemo(() => {
    const open = (data?.items ?? []).filter(
      (i) => i.kind === "government_fee" && i.state === "to_justify",
    );
    const ready = open
      .filter((i) => i.reading.billing_code && i.reading.code_debits === 1)
      .slice(0, PNBP_BATCH_LIMIT);
    return {
      ready,
      total: ready.reduce((sum, i) => sum + i.amount_idr, 0),
      twice: open.filter((i) => (i.reading.code_debits ?? 0) > 1).length,
    };
  }, [data]);

  // Batches justified today, for «Undo batch» on the Justified tab.
  const todayBatches = useMemo(() => {
    const out = new Map<string, { n: number; total: number; by: string }>();
    for (const i of data?.items ?? []) {
      const j = i.justification;
      if (i.state !== "justified" || !j?.batch_id || j.follows_txn_id) continue;
      if (!justifiedToday(j.at)) continue;
      const b = out.get(j.batch_id) ?? { n: 0, total: 0, by: j.by };
      b.n += 1;
      b.total += i.amount_idr;
      out.set(j.batch_id, b);
    }
    return [...out.entries()];
  }, [data]);

  const justify = async (item: OutgoingItem, body: JustifyOutgoingRequest) => {
    try {
      await accountingApi.justifyOutgoing(item.id, body);
    } catch (err) {
      logger.error(
        `accounting justify failed txn=${item.id}`,
        {},
        err as Error,
      );
      throw err; // the dialog shows the server's message and stays open
    }
    success("Outgoing justified");
    setJustifying(null);
    void load();
  };

  const undo = async (id: number) => {
    if (undoing) return;
    setUndoing(true);
    setUndoError(null);
    try {
      await accountingApi.undoOutgoingJustification(id);
      success("Justification undone");
      void load();
    } catch (err) {
      const detail =
        (err instanceof Error && err.message) || "Please try again";
      setUndoError({ id, message: detail });
      error("Undo failed", detail);
      logger.error(
        `accounting undo justify failed txn=${id}`,
        {},
        err as Error,
      );
    } finally {
      setUndoing(false);
    }
  };

  const justifyAllPnbp = async () => {
    if (batchBusy || pnbp.ready.length === 0) return;
    setBatchBusy(true);
    setBatchError(null);
    try {
      const res = await accountingApi.justifyPnbpBatch(
        pnbp.ready.map((i) => i.id),
      );
      success(`${res.n} PNBP justified`);
      setLastBatch({
        id: res.batch_id,
        n: res.n,
        at: new Date().toISOString(),
      });
      setConfirmBatch(false);
      void load();
    } catch (err) {
      const detail =
        (err instanceof Error && err.message) || "Please try again";
      setBatchError(detail);
      logger.error("accounting pnbp batch failed", {}, err as Error);
    } finally {
      setBatchBusy(false);
    }
  };

  const undoBatch = async (batchId: string) => {
    if (batchBusy) return;
    setBatchBusy(true);
    setBatchError(null);
    try {
      const res = await accountingApi.undoOutgoingBatch(batchId);
      success(`${res.n} PNBP back to justify`);
      setLastBatch(null);
      void load();
    } catch (err) {
      const detail =
        (err instanceof Error && err.message) || "Please try again";
      setBatchError(detail);
      error("Undo failed", detail);
      logger.error("accounting pnbp batch undo failed", {}, err as Error);
    } finally {
      setBatchBusy(false);
    }
  };

  const tabButton = (
    value: OutgoingState,
    label: string,
    n: number | undefined,
  ) => (
    <button
      type="button"
      aria-pressed={tab === value}
      onClick={() => {
        setTab(value);
        setKind(ALL);
        setConfirmBatch(false);
        setBatchError(null);
      }}
      className={`rounded-full border px-3 py-1.5 text-sm ${
        tab === value
          ? "border-[var(--bz-text-1)] font-semibold text-[var(--bz-text-1)]"
          : "border-[var(--bz-border)] text-[var(--bz-text-2)] hover:text-[var(--bz-text-1)]"
      }`}
    >
      {label}
      {n !== undefined ? ` (${n})` : ""}
    </button>
  );

  const emptyText =
    tab === "to_justify" ? "Nothing to justify." : "Nothing justified yet.";
  const shownCount = tab === "to_justify" ? data?.n : data?.n_justified;

  return (
    <section data-testid="outgoings" className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Outgoings</h1>
        <p className="mt-1 text-sm text-[var(--bz-text-2)]">
          Every payment out of the imported statements, newest first, with what
          the bank wrote. Justify each one; nothing here enters the cash-out.
        </p>
      </div>

      <div role="group" aria-label="Outgoings state" className="flex gap-2">
        {tabButton("to_justify", "To justify", data?.n)}
        {tabButton("justified", "Justified", data?.n_justified)}
      </div>

      {status === "ok" &&
        pnbpShown &&
        (undoable || pnbp.ready.length > 0 || pnbp.twice > 0) && (
          <div
            data-testid="pnbp-batch"
            className="space-y-2 rounded-lg border border-[var(--bz-border)] p-3 text-sm"
          >
            {undoable && (
              <p role="status">
                {undoable.n} PNBP justified.{" "}
                <button
                  type="button"
                  disabled={batchBusy}
                  onClick={() => void undoBatch(undoable.id)}
                  className="font-semibold underline underline-offset-4 disabled:opacity-50"
                >
                  Undo
                </button>
              </p>
            )}
            {pnbp.ready.length > 0 && !confirmBatch && (
              <button
                type="button"
                onClick={() => setConfirmBatch(true)}
                className="rounded-full border border-[var(--bz-text-1)] px-3 py-1.5 font-semibold"
              >
                Justify all PNBP ({pnbp.ready.length})
              </button>
            )}
            {pnbp.ready.length > 0 && confirmBatch && (
              <div className="flex flex-wrap items-center gap-3">
                <span>
                  Mark the {pnbp.ready.length} PNBP payments as justified?{" "}
                  <Money value={pnbp.total} /> · You can undo it today.
                </span>
                <button
                  type="button"
                  disabled={batchBusy}
                  onClick={() => void justifyAllPnbp()}
                  className="rounded-full border border-[var(--bz-text-1)] px-3 py-1.5 font-semibold disabled:opacity-50"
                >
                  {batchBusy ? "Justifying…" : "Yes, justify all"}
                </button>
                <button
                  type="button"
                  disabled={batchBusy}
                  onClick={() => setConfirmBatch(false)}
                  className="underline underline-offset-4 disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            )}
            {pnbp.twice > 0 && (
              <p className="text-xs text-[var(--state-danger)]">
                {pnbp.twice === 1
                  ? "1 PNBP looks paid twice: check it and justify it by hand."
                  : `${pnbp.twice} PNBP look paid twice: check them and justify them by hand.`}
              </p>
            )}
            {batchError && (
              <p role="alert" className="text-xs text-[var(--state-danger)]">
                {batchError}
              </p>
            )}
          </div>
        )}

      {status === "loading" && (
        <div className="flex items-center p-10 text-[var(--bz-text-2)]">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading outgoings…
        </div>
      )}

      {status === "failed" && (
        <div role="alert" className="flex items-center gap-3 text-sm">
          Could not load the outgoings.
          <button
            type="button"
            className="font-semibold underline underline-offset-4"
            onClick={() => void load()}
          >
            Try again
          </button>
        </div>
      )}

      {status === "ok" && data && data.items.length === 0 && (
        <p className="text-sm text-[var(--bz-text-2)]">{emptyText}</p>
      )}

      {status === "ok" && data && data.items.length > 0 && (
        <>
          {tab === "to_justify" && (
            <div
              role="group"
              aria-label="Filter by kind"
              className="flex flex-wrap gap-2"
            >
              <KindButton
                label="All"
                n={data.n}
                total={data.total_idr}
                active={kind === ALL}
                onClick={() => setKind(ALL)}
              />
              {data.by_kind.map((k) => (
                <KindButton
                  key={k.kind}
                  label={k.label}
                  n={k.n}
                  total={k.total_idr}
                  active={kind === k.kind}
                  onClick={() => setKind(k.kind)}
                />
              ))}
            </div>
          )}
          {tab === "justified" && (
            <p className="text-sm text-[var(--bz-text-2)]">
              {data.n_justified} justified ·{" "}
              <Money compact value={data.justified_total_idr} />
            </p>
          )}
          {tab === "justified" &&
            todayBatches.map(([id, b]) => (
              <div
                key={id}
                data-testid={`batch-${id}`}
                className="flex flex-wrap items-center gap-3 text-sm"
              >
                <span>
                  {b.n} PNBP justified together today ·{" "}
                  <Money compact value={b.total} />
                </span>
                <button
                  type="button"
                  disabled={batchBusy}
                  onClick={() => void undoBatch(id)}
                  className="text-xs font-semibold underline underline-offset-4 disabled:opacity-50"
                >
                  Undo
                </button>
              </div>
            ))}
          {tab === "justified" && batchError && (
            <p role="alert" className="text-xs text-[var(--state-danger)]">
              {batchError}
            </p>
          )}
          {shownCount !== undefined && data.items.length < shownCount && (
            <p className="text-xs text-[var(--state-warning)]">
              Showing the newest {data.items.length} of {shownCount}.
            </p>
          )}
          <div className="overflow-x-auto rounded-lg border border-[var(--bz-border)]">
            <table className="w-full text-sm">
              <thead className="bg-[var(--bz-surface-2)] text-left text-xs uppercase text-[var(--bz-text-2)]">
                <tr>
                  <th className="px-3 py-2">Date</th>
                  <th className="px-3 py-2">What</th>
                  <th className="px-3 py-2">Paid to</th>
                  <th className="px-3 py-2">Detail</th>
                  <th className="px-3 py-2 text-right">Amount</th>
                  <th className="px-3 py-2">
                    <span className="sr-only">Action</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((i) => {
                  const followsTransfer =
                    i.kind === "transfer_fee" && i.fee_of_txn_id !== null;
                  const canUndo =
                    i.state === "justified" &&
                    i.justification !== null &&
                    !i.justification.follows_txn_id &&
                    justifiedToday(i.justification.at);
                  return (
                    <tr
                      key={i.id}
                      data-testid={`outgoing-${i.id}`}
                      className="border-t border-[var(--bz-border)]"
                    >
                      <td className="whitespace-nowrap px-3 py-2 tabular-nums text-[var(--bz-text-2)]">
                        {ccShort(i.txn_date)} {timeOf(i.posted_at)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 font-medium">
                        {i.reading.label}
                        {(i.reading.code_debits ?? 0) > 1 && (
                          <span
                            data-testid={`code-twice-${i.id}`}
                            className="ml-2 rounded bg-[var(--state-danger)] px-1.5 py-0.5 text-[10px] font-semibold text-white"
                          >
                            Paid twice?
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2">{i.reading.payee || "—"}</td>
                      <td className="px-3 py-2 text-[var(--bz-text-2)]">
                        {i.reading.detail}
                        {i.state === "justified" && (
                          <JustificationBlock item={i} />
                        )}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                        <Money value={i.amount_idr} />
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">
                        {i.state === "to_justify" && !followsTransfer && (
                          <button
                            type="button"
                            data-testid={`justify-${i.id}`}
                            onClick={() => setJustifying(i)}
                            className="text-xs font-semibold underline underline-offset-4"
                          >
                            Justify
                          </button>
                        )}
                        {i.state === "to_justify" && followsTransfer && (
                          <span className="text-xs text-[var(--bz-text-2)]">
                            Follows its transfer
                          </span>
                        )}
                        {canUndo && (
                          <button
                            type="button"
                            data-testid={`undo-${i.id}`}
                            disabled={undoing}
                            onClick={() => void undo(i.id)}
                            className="text-xs font-semibold underline underline-offset-4 disabled:opacity-50"
                          >
                            Undo
                          </button>
                        )}
                        {undoError?.id === i.id && (
                          <p
                            role="alert"
                            className="text-xs text-[var(--state-danger)]"
                          >
                            {undoError.message}
                          </p>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {justifying && (
        <JustifyDialog
          item={justifying}
          onClose={() => setJustifying(null)}
          onSave={(body) => justify(justifying, body)}
        />
      )}
    </section>
  );
}
