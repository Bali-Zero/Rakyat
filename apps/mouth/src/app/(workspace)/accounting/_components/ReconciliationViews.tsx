import {
  ArrowRight,
  ArrowUpRight,
  CircleCheck,
  FileUp,
  Loader2,
  Upload,
} from "lucide-react";
import type { DoneTodayResponse } from "@/lib/api/workspace/accounting.api";
import type { DoneTodayStatus } from "./DoneTodaySection";
import { DoneTodayList } from "./DoneTodaySection";
import { ccDayDate } from "./ccFormat";
import type { BankDateState } from "./useBankDate";

export type CentreView =
  "done" | "loading" | "failed" | "dayone" | "calm" | "work";

const SECONDARY =
  "inline-flex h-10 items-center gap-2 rounded-[10px] border border-[var(--cc-line-control)] bg-[var(--cc-card)] px-4 text-sm font-bold text-[var(--cc-tx)]";
const PRIMARY =
  "inline-flex h-11 items-center gap-2 rounded-[10px] bg-[var(--cc-act)] px-[22px] text-sm font-bold text-[var(--cc-act-on)] disabled:opacity-50";
const EMPTY_CARD =
  "mt-5 flex h-[470px] flex-col items-center justify-center rounded-[14px] border border-[var(--cc-edge)] bg-[var(--cc-card)] text-[var(--cc-tx-2)]";

export function ReconciliationBody({
  view,
  work,
  bankDate,
  done,
  doneStatus,
  doneUnavailable,
  linkedIgnored,
  onRetryInbox,
  onRetryDone,
  onShowDone,
  onUpload,
}: {
  view: CentreView;
  work: React.ReactNode;
  bankDate: BankDateState;
  done: DoneTodayResponse | null;
  doneStatus: DoneTodayStatus;
  doneUnavailable: boolean;
  linkedIgnored: React.ReactNode;
  onRetryInbox: () => void;
  onRetryDone: () => void;
  onShowDone: () => void;
  onUpload: () => void;
}) {
  if (view === "work") return work;
  if (view === "loading") {
    return (
      <div className={EMPTY_CARD}>
        <p className="flex items-center">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Loading inbox…
        </p>
      </div>
    );
  }
  if (view === "failed") {
    return (
      <div className={EMPTY_CARD}>
        <p>Could not load the inbox.</p>
        <button
          type="button"
          className={`${SECONDARY} mt-3`}
          onClick={onRetryInbox}
        >
          Try again
        </button>
      </div>
    );
  }
  if (view === "dayone") {
    return (
      <div
        data-testid="cc-dayone"
        className="mt-[30px] grid h-[470px] grid-cols-[340px_minmax(0,1fr)] overflow-hidden rounded-[14px] border border-[var(--cc-edge)] bg-[var(--cc-card)]"
      >
        <div className="flex items-center justify-center bg-[var(--cc-wash)]">
          <FileUp
            className="h-[88px] w-[88px] text-[var(--cc-tx-2)]"
            aria-hidden="true"
          />
        </div>
        <div className="flex flex-col px-[52px] py-11 text-[var(--cc-tx)]">
          <h2 className="font-serif text-[26px] font-semibold leading-tight tracking-[-0.01em] text-[var(--cc-tx)]">
            Upload this morning’s bank statement
          </h2>
          <p className="mt-2 max-w-[560px] text-base text-[var(--cc-tx-2)]">
            Once it is in, every incoming transfer waits in one of four tabs,
            with a proposal ready to check.
          </p>
          <ol className="mt-[26px] flex flex-col gap-4">
            {[
              [
                "Export the statement from CIMB",
                "In CIMB business banking, export the account statement as CSV.",
              ],
              ["Upload the file here", "Uploading pays nothing."],
              [
                "Work one tab at a time",
                "You are done when every tab is empty.",
              ],
            ].map(([title, copy], index) => (
              <li
                key={title}
                className="grid grid-cols-[30px_minmax(0,1fr)] items-start gap-3.5"
              >
                <span className="flex h-[30px] w-[30px] items-center justify-center rounded-full border-[1.5px] border-[var(--cc-tx)] font-serif text-sm font-semibold">
                  {index + 1}
                </span>
                <span>
                  <b className="mt-[3px] block text-base">{title}</b>
                  <span className="text-[var(--cc-tx-2)]">{copy}</span>
                </span>
              </li>
            ))}
          </ol>
          <div className="mt-auto flex items-center gap-[18px]">
            <button type="button" className={PRIMARY} onClick={onUpload}>
              <Upload className="h-4 w-4" aria-hidden="true" /> Upload CIMB
              statement
            </button>
            <p className="text-[var(--cc-tx-2)]">
              a .csv file exported from CIMB
            </p>
          </div>
        </div>
      </div>
    );
  }
  if (view === "done") {
    return (
      <div
        data-testid="cc-done"
        className="mt-5 rounded-[14px] border border-[var(--cc-edge)] bg-[var(--cc-card)] px-7 py-[26px] text-[var(--cc-tx)]"
      >
        <h2 className="text-xl font-semibold">Done today</h2>
        <DoneTodayList data={done} status={doneStatus} onRetry={onRetryDone} />
        <div className="mt-6 border-t border-[var(--cc-hair)] pt-6">
          {linkedIgnored}
        </div>
      </div>
    );
  }
  const handled = done?.counts.total;
  return (
    <div data-testid="cc-calm" className={`${EMPTY_CARD} text-center`}>
      <CircleCheck
        className="h-14 w-14 text-[var(--cc-confirm-ink)]"
        aria-hidden="true"
      />
      <h2 className="mt-[26px] font-serif text-[26px] font-semibold leading-tight tracking-[-0.01em] text-[var(--cc-tx)]">
        All done for today
      </h2>
      <p className="mt-2 text-base">
        {handled !== undefined &&
          `${handled} transfer${handled === 1 ? "" : "s"} handled. `}
        {bankDate.status === "ok" &&
          `Bank data is in through ${ccDayDate(bankDate.date)}.`}
      </p>
      {!doneUnavailable && (
        <button
          type="button"
          className={`${SECONDARY} mt-[26px]`}
          onClick={onShowDone}
        >
          See what was done today
        </button>
      )}
    </div>
  );
}

export function ReconciliationMore({
  onOpenCashout,
}: {
  onOpenCashout: () => void;
}) {
  return (
    <section data-testid="cc-more" className="mt-6 grid grid-cols-2 gap-6">
      <div aria-hidden="true" />
      <div
        data-testid="cc-cashout-card"
        className="flex h-20 items-center gap-4 rounded-[14px] border border-[var(--cc-edge)] bg-[var(--cc-card)] px-6"
      >
        <span className="flex h-10 w-10 items-center justify-center rounded-[12px] bg-[var(--cc-wash)] text-[var(--cc-tx-2)]">
          <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
        </span>
        <p className="text-sm font-semibold text-[var(--cc-tx-2)]">
          Cash-out this week
        </p>
        <button
          type="button"
          className="ml-auto flex items-center gap-1 font-bold text-[var(--cc-tx)] underline decoration-1 underline-offset-4"
          onClick={onOpenCashout}
        >
          Open <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>
    </section>
  );
}
