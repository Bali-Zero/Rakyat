"use client";

/**
 * Outgoings — every debit of the imported statements, newest first, with what the bank's text
 * says (payee and purpose of a transfer, the e-billing code of a PNBP payment, the transfer a
 * fee belongs to). The accountant justifies a debit (category, optional case, note, and for a
 * cash withdrawal the recipient and a breakdown that sums to the debit) and can undo it the
 * same day (WITA). Nothing here enters the cash-out.
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
/** Undo is offered only for a justification made today, Bali time (the server decides too). */
const justifiedToday = (iso: string): boolean =>
  dayInWita(iso) === dayInWita(new Date().toISOString());

function JustificationBlock({ item }: { item: OutgoingItem }) {
  const j = item.justification;
  if (!j) return null;
  const when = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(j.at);
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
