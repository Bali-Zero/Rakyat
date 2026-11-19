"use client";

/**
 * Bank statement upload for the Reconciliation inbox: a CIMB Niaga CSV, a Bank
 * Mandiri, BNI or Bank Permata PDF (the server detects which from the content).
 *
 * Two steps, one File: choosing a file calls preview (stores nothing) and shows
 * the report; Import then sends THE SAME File object that was previewed. Import
 * is blocked when the statement's running balance does not add up. The backend
 * import is idempotent, so a repeated upload stores nothing new.
 */

import { useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { logger } from "@/lib/logger";
import { formatStatementAmount } from "./formatStatementAmount";
import { CcGlyph, CC_SECONDARY } from "./CcGlyph";
import {
  accountingApi,
  type BankStatementImportOutcome,
  type BankStatementPreview,
} from "@/lib/api/workspace/accounting.api";

type Phase = "idle" | "previewing" | "ready" | "importing" | "done";

export const BANK_FILE_INPUT_ID = "bank-statement-file";

function Stat({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <dt className="text-xs text-[var(--bz-text-2)]">{label}</dt>
      <dd className="text-sm font-medium tabular-nums">{children}</dd>
    </div>
  );
}

export function BankStatementUpload({
  onImported,
  locked = false,
  exclusive,
  title,
  status,
  hideTrigger = false,
}: {
  onImported: () => void;
  /** Another write of the tab is pending or a dialog is open: Import waits. */
  locked?: boolean;
  /** Shared one-write-at-a-time runner of the tab. */
  exclusive?: (fn: () => Promise<unknown>) => Promise<unknown>;
  title?: string;
  status?: React.ReactNode;
  hideTrigger?: boolean;
}) {
  const { error } = useToast();
  const [phase, setPhase] = useState<Phase>("idle");
  // The chosen file and its report: truth in a ref (Import reads it at click
  // time), mirrored to state for rendering.
  const chosenRef = useRef<{
    file: File | null;
    report: BankStatementPreview | null;
  }>({ file: null, report: null });
  const [report, setReportState] = useState<BankStatementPreview | null>(null);
  const setChosen = (file: File | null, rep: BankStatementPreview | null) => {
    chosenRef.current = { file, report: rep };
    setReportState(rep);
  };
  const [outcome, setOutcome] = useState<BankStatementImportOutcome | null>(
    null,
  );
  const [message, setMessage] = useState<string | null>(null);
  // Synchronous guard: a second click can land before React re-renders.
  const inFlight = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const busy = phase === "previewing" || phase === "importing";

  const reset = () => {
    if (inFlight.current) return;
    setPhase("idle");
    setChosen(null, null);
    setOutcome(null);
    setMessage(null);
  };

  const fail = (title: string, err: unknown) => {
    const detail = (err instanceof Error && err.message) || "Please try again";
    setMessage(detail);
    setChosen(null, null);
    setPhase("idle");
    error(title, detail);
    logger.error(`accounting bank statement ${title}`, {}, err as Error);
  };

  const handleChoose = async (chosen: File | undefined) => {
    if (!chosen || inFlight.current) return;
    inFlight.current = true;
    setChosen(chosen, null);
    setOutcome(null);
    setMessage(null);
    setPhase("previewing");
    try {
      const res = await accountingApi.previewBankStatement(chosen);
      setChosen(chosen, res);
      setPhase("ready");
    } catch (err) {
      fail("Statement preview failed", err);
    } finally {
      inFlight.current = false;
    }
  };

  const handleImport = async () => {
    // Decide on the refs, at click time.
    const { file, report: rep } = chosenRef.current;
    if (!file || !rep || !rep.balance_check.ok || inFlight.current) return;
    // The flag is set only when the request really starts (inside what the runner
    // calls): an import the lock refuses leaves nothing set.
    const run = async () => {
      inFlight.current = true;
      setPhase("importing");
      try {
        const res = await accountingApi.importBankStatement(file);
        setOutcome(res.import);
        setPhase("done");
        onImported();
      } catch (err) {
        fail("Statement import failed", err);
      } finally {
        inFlight.current = false;
      }
    };
    if (exclusive) await exclusive(run);
    else await run();
  };

  const mismatches = report?.balance_check.mismatch_positions.length ?? 0;
  const balanceOk = report?.balance_check.ok ?? false;

  const panel = phase !== "idle" || message || report || outcome;

  return (
    <>
      <div
        data-testid="cc-header"
        className="flex h-11 items-center gap-[18px]"
      >
        {title && (
          <h1 className="font-serif text-[26px] font-medium leading-none tracking-[-0.02em] text-[var(--cc-tx)]">
            {title}
          </h1>
        )}
        <div className="ml-auto flex items-center gap-3.5">
          {status}
          {!hideTrigger && (
            <button
              type="button"
              className={CC_SECONDARY}
              onClick={() => inputRef.current?.click()}
              disabled={busy}
            >
              <CcGlyph name="upload" className="h-4 w-4" /> Upload statement
            </button>
          )}
        </div>
      </div>
      <input
        ref={inputRef}
        id={BANK_FILE_INPUT_ID}
        type="file"
        accept=".csv,text/csv,.pdf,application/pdf"
        disabled={busy}
        className="sr-only"
        tabIndex={-1}
        aria-label="Upload bank statement (CSV or PDF)"
        data-testid="bank-statement-input"
        onChange={(e) => {
          void handleChoose(e.target.files?.[0]);
          e.target.value = "";
        }}
      />

      {!panel && !hideTrigger && (
        <p className="mt-2 text-xs text-[var(--bz-text-2)]">
          Reads CIMB Niaga CSV, Bank Mandiri, BNI and Bank Permata PDF
          statements (IDR).
        </p>
      )}

      {panel && (
        <section
          className="mt-5 space-y-3 rounded-[14px] border border-[var(--cc-edge)] bg-[var(--cc-card)] p-5"
          aria-label="Bank statement"
        >
          {phase === "previewing" && (
            <p className="flex items-center text-sm text-[var(--bz-text-2)]">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Reading
              statement…
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
                  The statement&apos;s running balance does not add up, so it
                  cannot be imported.
                </p>
              )}

              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase text-[var(--bz-text-2)]">
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
                      <td className="py-1 text-right tabular-nums">
                        {v.count}
                      </td>
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
                  The stored history of this account has{" "}
                  {outcome.history_segments} separate segments: a statement for
                  a period in between has not been uploaded yet.
                </p>
              )}
            </div>
          )}
        </section>
      )}
    </>
  );
}
