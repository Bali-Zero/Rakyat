"use client";

/**
 * Bank statement upload (CIMB CSV) for the Reconciliation inbox.
 *
 * Two steps, one File: choosing a file calls preview (stores nothing) and shows
 * the report; Import then sends THE SAME File object that was previewed. Import
 * is blocked when the statement's running balance does not add up. The backend
 * import is idempotent, so a repeated upload stores nothing new.
 */

import { useRef, useState } from "react";
import { Loader2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { logger } from "@/lib/logger";
import { formatStatementAmount } from "./formatStatementAmount";
import {
  accountingApi,
  type BankStatementImportOutcome,
  type BankStatementPreview,
} from "@/lib/api/workspace/accounting.api";

type Phase = "idle" | "previewing" | "ready" | "importing" | "done";

function Stat({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm font-medium tabular-nums">{children}</dd>
    </div>
  );
}

export function BankStatementUpload({
  onImported,
  locked = false,
  exclusive,
}: {
  onImported: () => void;
  /** Another write of the tab is pending or a dialog is open: Import waits. */
  locked?: boolean;
  /** Shared one-write-at-a-time runner of the tab. */
  exclusive?: (fn: () => Promise<void>) => Promise<void>;
}) {
  const { error } = useToast();
  const [phase, setPhase] = useState<Phase>("idle");
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<BankStatementPreview | null>(null);
  const [outcome, setOutcome] = useState<BankStatementImportOutcome | null>(
    null,
  );
  const [message, setMessage] = useState<string | null>(null);
  // Synchronous guard: a second click can land before React re-renders.
  const inFlight = useRef(false);

  const busy = phase === "previewing" || phase === "importing";

  const reset = () => {
    setPhase("idle");
    setFile(null);
    setReport(null);
    setOutcome(null);
    setMessage(null);
  };

  const fail = (title: string, err: unknown) => {
    const detail = (err instanceof Error && err.message) || "Please try again";
    setMessage(detail);
    setReport(null);
    setFile(null);
    setPhase("idle");
    error(title, detail);
    logger.error(`accounting bank statement ${title}`, {}, err as Error);
  };

  const handleChoose = async (chosen: File | undefined) => {
    if (!chosen || inFlight.current) return;
    inFlight.current = true;
    setReport(null);
    setOutcome(null);
    setMessage(null);
    setFile(chosen);
    setPhase("previewing");
    try {
      const res = await accountingApi.previewBankStatement(chosen);
      setReport(res);
      setPhase("ready");
    } catch (err) {
      fail("Statement preview failed", err);
    } finally {
      inFlight.current = false;
    }
  };

  const handleImport = async () => {
    if (!file || !report || !report.balance_check.ok || inFlight.current) {
      return;
    }
    inFlight.current = true;
    const run = async () => {
      setPhase("importing");
      try {
        const res = await accountingApi.importBankStatement(file);
        setOutcome(res.import);
        setPhase("done");
        onImported();
      } catch (err) {
        fail("Statement import failed", err);
      }
    };
    try {
      if (exclusive) await exclusive(run);
      else await run();
    } finally {
      inFlight.current = false;
    }
  };

  const mismatches = report?.balance_check.mismatch_positions.length ?? 0;
  const balanceOk = report?.balance_check.ok ?? false;

  return (
    <section
      className="space-y-3 rounded-lg border bg-card p-4"
      aria-label="Bank statement"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <Upload className="h-4 w-4 text-[var(--bz-accent)]" /> Bank statement
        </h2>
        <label className="text-sm text-muted-foreground">
          <span className="mr-2">Upload bank statement (CSV)</span>
          <input
            type="file"
            accept=".csv,text/csv"
            disabled={busy}
            data-testid="bank-statement-input"
            onChange={(e) => {
              void handleChoose(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
        </label>
      </div>

      {phase === "previewing" && (
        <p className="flex items-center text-sm text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Reading statement…
        </p>
      )}

      {message && (
        <p role="alert" className="text-sm text-[var(--state-danger)]">
          {message}
        </p>
      )}

      {report && (
        <div className="space-y-3">
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Account">
              {report.account.number} · {report.account.name}
            </Stat>
            <Stat label="Period">
              {report.period.first.replace("T", " ")} to{" "}
              {report.period.last.replace("T", " ")}
            </Stat>
            <Stat label="Transactions">{report.row_count}</Stat>
            <Stat label="Balance check">
              {balanceOk
                ? "OK"
                : `${mismatches} mismatch${mismatches === 1 ? "" : "es"}`}
            </Stat>
            <Stat label="Total debit">
              {formatStatementAmount(report.total_debit)}
            </Stat>
            <Stat label="Total credit">
              {formatStatementAmount(report.total_credit)}
            </Stat>
            <Stat label="Opening balance">
              {formatStatementAmount(report.opening_balance)}
            </Stat>
            <Stat label="Closing balance">
              {formatStatementAmount(report.closing_balance)}
            </Stat>
          </dl>

          {!balanceOk && (
            <p
              role="alert"
              className="rounded-md bg-[color-mix(in_srgb,var(--state-warning)_10%,transparent)] p-2 text-xs text-[var(--state-warning)]"
            >
              The statement&apos;s running balance does not add up, so it cannot
              be imported.
            </p>
          )}

          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="py-1 font-medium">Kind</th>
                <th className="py-1 text-right font-medium">Count</th>
                <th className="py-1 text-right font-medium">Sum</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {Object.entries(report.kinds).map(([kind, v]) => (
                <tr key={kind}>
                  <td className="py-1">{kind.replace(/_/g, " ")}</td>
                  <td className="py-1 text-right tabular-nums">{v.count}</td>
                  <td className="py-1 text-right tabular-nums">
                    {formatStatementAmount(v.total)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {(phase === "ready" || phase === "importing") && (
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={reset} disabled={busy}>
                Cancel
              </Button>
              <Button
                onClick={handleImport}
                disabled={busy || locked || !balanceOk}
              >
                {phase === "importing" && (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                )}
                Import {report.row_count} transactions
              </Button>
            </div>
          )}
        </div>
      )}

      {outcome && (
        <div className="space-y-1 text-sm" role="status">
          <p>
            {outcome.nothing_new
              ? "Nothing new: every transaction of this statement was already stored."
              : `${outcome.rows_inserted} new transactions stored, ${outcome.rows_duplicate} already stored.`}
          </p>
          {outcome.history_segments > 1 && (
            <p className="text-[var(--state-warning)]">
              The stored history of this account has {outcome.history_segments}{" "}
              separate segments: a statement for a period in between has not
              been uploaded yet.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
