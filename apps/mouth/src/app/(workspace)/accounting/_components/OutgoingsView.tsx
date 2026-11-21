"use client";

/**
 * Outgoings to justify — every debit of the imported statements that nobody has justified yet,
 * newest first, with what the bank's text says (payee and purpose of a transfer, the e-billing
 * code of a PNBP payment, the transfer a fee belongs to). Reads only: justifying comes next.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { Money } from "@balizero/core";
import {
  accountingApi,
  type OutgoingsResponse,
} from "@/lib/api/workspace/accounting.api";
import { logger } from "@/lib/logger";
import { ccShort } from "./ccFormat";

const ALL = "all";

function timeOf(postedAt: string | null): string {
  const m = postedAt ? /T(\d{2}:\d{2})/.exec(postedAt) : null;
  return m ? m[1] : "";
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
  const [data, setData] = useState<OutgoingsResponse | null>(null);
  const [status, setStatus] = useState<"loading" | "ok" | "failed">("loading");
  const [kind, setKind] = useState<string>(ALL);

  const load = useCallback(async () => {
    setStatus("loading");
    try {
      setData(await accountingApi.getOutgoings());
      setStatus("ok");
    } catch (err) {
      logger.error("accounting outgoings load failed", {}, err as Error);
      setStatus("failed");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = useMemo(
    () => (data?.items ?? []).filter((i) => kind === ALL || i.kind === kind),
    [data, kind],
  );

  return (
    <section data-testid="outgoings" className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Outgoings to justify
        </h1>
        <p className="mt-1 text-sm text-[var(--bz-text-2)]">
          Every payment out of the imported statements, newest first, with what
          the bank wrote. Justifying them comes next: this page changes nothing.
        </p>
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

      {status === "ok" && data && data.n === 0 && (
        <p className="text-sm text-[var(--bz-text-2)]">Nothing to justify.</p>
      )}

      {status === "ok" && data && data.n > 0 && (
        <>
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
          {data.items.length < data.n && (
            <p className="text-xs text-[var(--state-warning)]">
              Showing the newest {data.items.length} of {data.n}.
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
                </tr>
              </thead>
              <tbody>
                {rows.map((i) => (
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
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      <Money value={i.amount_idr} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
