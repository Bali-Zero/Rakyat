"use client";

import {
  ArrowUpRight,
  CalendarDays,
  ChartNoAxesCombined,
  ChevronDown,
  CircleAlert,
  FileCheck2,
  Flag,
  History,
  WalletCards,
  Goal,
  LoaderCircle,
  Pencil,
} from "lucide-react";
import Link from "next/link";
import React, { useEffect, useId, useState } from "react";
import { CARD, FOCUS, SERIF } from "@/components/workspace/r19/tokens";
import { api } from "@/lib/api";
import { bonusMonthLabel, bonusPaydayText } from "@/lib/hr/bonus-run-format";
import styles from "./DeliveryBonusDesk.module.css";
import {
  acceptResponse,
  useSessionIdentity,
  type SessionIdentity,
} from "@/lib/auth/session-identity";

type Earned = {
  practice_id: number;
  service: string;
  event_kind: string;
  earned_at: string;
  awarded_at: string;
  status: string;
  amount_idr: number | null;
};
type Hold = {
  practice_id: number;
  service: string;
  slot_key: string;
  slot_label?: string | null;
  due_on: string;
  payout_from_month: string;
  next_action:
    | "upload_permit"
    | "send_escalation_email"
    | "verify_escalation_email"
    | "upload_after_email";
  email_state: string;
};
const holdActions: Record<Hold["next_action"], string> = {
  upload_permit: "Upload the missing permit or review postponement",
  send_escalation_email: "Review official email to Zero",
  verify_escalation_email: "Check the email delivery record",
  upload_after_email:
    "Email sent. Upload the required permit to clear this hold.",
};
type AwaitingPayment = {
  bonus_month: string;
  run_status: "open" | "approved";
  amount_idr: number;
  pending_idr: number;
  expected_payday: string | null;
  payday_salary_month: string;
};
type Desk = {
  team_member_id: string;
  month: string;
  on_payroll: boolean;
  milestones: number;
  target: number | null;
  totals: {
    pending: number;
    approved: number;
    paid: number;
    awaiting_rate: number;
    payroll_locked: number;
  };
  recent: Earned[];
  holds: Hold[];
  /** Ended bonus months whose run is not paid yet: gross amounts, no tax. */
  awaiting_payment?: AwaitingPayment[];
  next_case: {
    practice_id: number;
    service: string;
    amount_idr: number | null;
    already_earned: boolean;
    forfeits?: boolean;
  } | null;
};
type BoundDesk = {
  data: Desk;
  nextFile?: string;
  nextDue?: string;
  requirements?: { met: number; total: number };
};
const money = (amount: number) => `Rp ${amount.toLocaleString("id-ID")}`;
const baliDate = (value: string, opts: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Makassar",
    ...opts,
  }).format(new Date(value));
const sectionTitle = "flex items-center gap-2 text-sm font-bold leading-snug";
const rowLink =
  "inline-flex min-h-11 items-center gap-2 text-sm leading-snug font-semibold [overflow-wrap:anywhere] underline-offset-4 hover:underline";

const payStages = ["pending", "approved", "paid"] as const;
type PayStage = (typeof payStages)[number];
const percentage = new Intl.NumberFormat("en-GB", {
  style: "percent",
  maximumFractionDigits: 1,
});

const statusNames: Record<string, string> = {
  pending: "Pending approval",
  approved: "Approved",
  paid: "Paid",
  awaiting_rate: "Rate to confirm",
  payroll_locked: "Month closed · HR review",
  forfeited: "No bonus · paid below price",
  no_bonus: "No bonus for this service",
  reversed: "Reversed",
  rejected: "Rejected",
};

export function DeliveryBonusDesk() {
  const identity = useSessionIdentity();
  return identity ? (
    <PersonalDeliveryDesk
      key={`${identity.sub}:${identity.epoch}`}
      identity={identity}
    />
  ) : null;
}

function PersonalDeliveryDesk({ identity }: { identity: SessionIdentity }) {
  const [bound, setBound] = useState<BoundDesk | null>(null);
  const [generation, refresh] = useState(0);
  const [goal, setGoal] = useState("");
  const [editing, setEditing] = useState(false);
  const [selectedStage, selectStage] = useState<PayStage | null>(null);
  const activityId = useId();
  const [busy, setBusy] = useState(false);
  const [payrollConfirmed, setPayrollConfirmed] = useState(false);
  const [error, setError] = useState(false);
  const [saveError, setSaveError] = useState(false);
  useEffect(() => {
    const update = () => refresh((v) => v + 1);
    window.addEventListener("focus", update);
    window.addEventListener("kita:delivery-changed", update);
    return () => {
      window.removeEventListener("focus", update);
      window.removeEventListener("kita:delivery-changed", update);
    };
  }, []);
  useEffect(() => {
    let live = true;
    setError(false);
    async function read(owner: SessionIdentity) {
      try {
        const res = await api.requestWithCredential<Desk>(
          "/api/hr/me/delivery-desk",
        );
        if (!live) return;
        if (!acceptResponse(res, owner, res.data.team_member_id)) {
          setBound(null);
          setError(true);
          return;
        }
        setPayrollConfirmed(res.data.on_payroll);
        setBound({ data: res.data });
        const next = res.data.next_case;
        if (
          !res.data.on_payroll ||
          !next ||
          res.data.holds?.some((row) => row.practice_id === next.practice_id)
        )
          return;
        // Reuse the practice's effective slots, including waived and expired documents.
        const pack = await api.requestWithCredential<{
          needs_titles?: boolean;
          visa_followup?: {
            slot_key: string;
            due_on: string;
            fulfilled_at: string | null;
          } | null;
          slots: {
            key: string;
            label: string;
            document_id: number | null;
            company_document_id?: number | null;
            waiver_reason?: string | null;
          }[];
        }>(`/api/crm/practices/${next.practice_id}/delivery`);
        if (!live) return;
        if (!acceptResponse(pack, owner)) {
          setBound(null);
          setError(true);
          return;
        }
        const followup = pack.data.visa_followup;
        const openFollowup = followup && !followup.fulfilled_at;
        const missing = pack.data.slots.filter(
          (slot) =>
            !slot.document_id &&
            !slot.company_document_id &&
            !slot.waiver_reason,
        );
        const needed =
          (openFollowup &&
            missing.find((slot) => slot.key === followup.slot_key)) ||
          missing[0];
        setBound((current) =>
          current?.data !== res.data
            ? current
            : {
                data: res.data,
                nextDue: openFollowup ? followup.due_on : undefined,
                requirements:
                  !pack.data.needs_titles && pack.data.slots.length
                    ? {
                        met: pack.data.slots.length - missing.length,
                        total: pack.data.slots.length,
                      }
                    : undefined,
                nextFile: pack.data.needs_titles
                  ? "Define the agreed documents"
                  : (needed?.label ??
                    (pack.data.slots.length ? "Confirm delivery" : undefined)),
              },
        );
      } catch {
        if (live) setError(true);
      }
    }
    void read(identity);
    return () => {
      live = false;
    };
  }, [identity, generation]);
  const data = bound?.data;
  if (!payrollConfirmed || (data && !data.on_payroll)) return null;
  if (!data) {
    return (
      <div
        className={`sentry-block mb-5 flex min-h-14 items-center gap-3 px-5 text-sm text-[var(--bz-text-2)] ${CARD}`}
        data-sentry-block=""
      >
        {saveError && (
          <p role="alert">
            Your goal update could not be verified. Reload to check your goal.
          </p>
        )}
        {error ? (
          <button
            type="button"
            className={`min-h-11 underline ${FOCUS}`}
            onClick={() => refresh((v) => v + 1)}
          >
            Reload my delivery bonuses
          </button>
        ) : (
          <span role="status" className="flex items-center gap-2">
            <LoaderCircle
              aria-hidden
              className="size-4 motion-safe:animate-spin"
            />{" "}
            Loading personal totals
          </span>
        )}
      </div>
    );
  }

  async function saveGoal(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    const target = Number(goal);
    if (!Number.isInteger(target) || target < 1 || target > 200) return;
    setBusy(true);
    setSaveError(false);
    try {
      const res = await api.requestWithCredential<Desk>(
        "/api/hr/me/delivery-goal",
        { method: "PUT", body: JSON.stringify({ target }) },
      );
      if (acceptResponse(res, identity, res.data.team_member_id)) {
        setBound({ data: res.data });
        setEditing(false);
        refresh((v) => v + 1);
      } else {
        setBound(null);
        setEditing(false);
        setSaveError(true);
        setError(true);
      }
    } catch {
      setSaveError(true);
    } finally {
      setBusy(false);
    }
  }

  const total = data.totals.pending + data.totals.approved + data.totals.paid;
  const reached = data.target !== null && data.milestones >= data.target;
  const holds = data.holds ?? [];
  const awaiting = data.awaiting_payment ?? [];
  const next = holds.some(
    (row) => row.practice_id === data.next_case?.practice_id,
  )
    ? null
    : data.next_case;
  const month = baliDate(data.month, { month: "long", year: "numeric" });
  const recent = selectedStage
    ? data.recent.filter(
        (row) =>
          row.status === selectedStage &&
          baliDate(row.awarded_at, { month: "long", year: "numeric" }) ===
            month,
      )
    : data.recent;
  const goalShare = data.target
    ? Math.min(1, data.milestones / data.target)
    : 0;
  return (
    <section
      aria-label="My delivery bonuses"
      data-sentry-block=""
      className={`sentry-block ${styles.desk}`}
    >
      <header className={styles.header}>
        <div className="flex min-w-0 items-center gap-3">
          <span
            className="grid size-8 shrink-0 place-items-center rounded-[10px] bg-[var(--bz-kita-rail)] text-[var(--nav-fg)]"
            aria-hidden
          >
            <WalletCards size={19} strokeWidth={1.6} />
          </span>
          <h2 style={SERIF}>My delivery bonuses</h2>
        </div>
        <time
          className="inline-flex items-center gap-2 rounded-full border border-[var(--bz-border)] px-2.5 py-1 text-xs font-semibold text-[var(--bz-text-2)]"
          dateTime={data.month.slice(0, 7)}
        >
          <CalendarDays size={14} aria-hidden />
          {month}
        </time>
      </header>

      <div className={styles.statement}>
        <section className={styles.balance} aria-label="Bonus breakdown">
          <h3 className="flex items-center gap-2 text-xs leading-snug font-bold text-[var(--bz-text-2)]">
            Bonuses this month
          </h3>
          <p className={styles.money} style={SERIF}>
            {money(total)}
          </p>
          <div className={styles.distribution} aria-hidden>
            {payStages.map(
              (stage) =>
                data.totals[stage] > 0 && (
                  <i
                    key={stage}
                    data-stage={stage}
                    data-muted={
                      selectedStage !== null && selectedStage !== stage
                    }
                    style={{ flexGrow: data.totals[stage] }}
                  />
                ),
            )}
          </div>
          <div
            className="grid grid-cols-3 gap-1"
            role="group"
            aria-label="Inspect bonus amounts"
          >
            {payStages.map((stage) => (
              <button
                key={stage}
                type="button"
                className={styles.legendButton}
                aria-pressed={selectedStage === stage}
                aria-controls={activityId}
                onClick={() =>
                  selectStage(selectedStage === stage ? null : stage)
                }
              >
                <span className="flex items-center gap-1.5 text-[11px] leading-snug text-[var(--bz-text-2)]">
                  <i
                    className="size-2 shrink-0 rounded-[2px]"
                    data-stage={stage}
                    aria-hidden
                  />
                  {statusNames[stage]}
                </span>
                <strong>{money(data.totals[stage])}</strong>
              </button>
            ))}
          </div>
          {selectedStage && (
            <p
              role="status"
              className="mt-1.5 text-xs leading-relaxed text-[var(--bz-text-2)]"
            >
              {total > 0
                ? `${statusNames[selectedStage]} · ${percentage.format(data.totals[selectedStage] / total)} of this month's total`
                : "No priced bonuses this month."}
            </p>
          )}
          {(data.totals.awaiting_rate > 0 ||
            data.totals.payroll_locked > 0) && (
            <p className={styles.qualifiers}>
              {data.totals.awaiting_rate > 0 && (
                <span>{data.totals.awaiting_rate} awaiting a rate</span>
              )}
              {data.totals.payroll_locked > 0 && (
                <span>
                  {data.totals.payroll_locked} · Month closed · HR review
                </span>
              )}
            </p>
          )}
        </section>

        <section className={styles.goal} aria-label="Personal service goal">
          <h3 className="flex items-center gap-2 text-xs leading-snug font-bold text-[var(--bz-text-2)]">
            <Flag
              size={14}
              className="text-[var(--state-success)]"
              aria-hidden
            />
            Qualifying services this month
          </h3>
          <div className="flex items-baseline justify-between gap-2">
            <p className={styles.goalNumber} style={SERIF}>
              {data.milestones}
              {data.target !== null && <span> / {data.target}</span>}
            </p>
            {data.target !== null && (
              <strong className="text-sm font-bold text-[var(--state-success)]">
                {percentage.format(data.milestones / data.target)}
              </strong>
            )}
          </div>
          {data.target !== null && (
            <div
              className={styles.goalTrack}
              role="progressbar"
              aria-label="Monthly service goal"
              aria-valuemin={0}
              aria-valuemax={data.target}
              aria-valuenow={Math.min(data.target, data.milestones)}
              aria-valuetext={`${data.milestones} qualifying services, goal ${data.target}`}
            >
              <span style={{ width: percentage.format(goalShare) }} />
            </div>
          )}
          <div className="flex flex-wrap items-center justify-between gap-x-2 text-xs leading-relaxed text-[var(--bz-text-2)]">
            <p>
              {data.target !== null
                ? reached
                  ? "Monthly goal reached"
                  : `${data.target - data.milestones} more to reach your goal`
                : "Choose a monthly service goal"}
            </p>
            {!editing && (
              <button
                type="button"
                className="inline-flex min-h-11 items-center gap-1.5 text-xs font-semibold text-[var(--bz-copper-text)] underline-offset-4 hover:underline"
                onClick={() => {
                  setGoal(String(data.target ?? ""));
                  setSaveError(false);
                  setEditing(true);
                }}
              >
                {data.target ? (
                  <Pencil size={13} aria-hidden />
                ) : (
                  <Goal size={14} aria-hidden />
                )}
                {data.target ? "Edit personal goal" : "Set a personal goal"}
              </button>
            )}
          </div>
          {editing && (
            <form
              onSubmit={saveGoal}
              className="flex flex-wrap items-center gap-2"
            >
              <label className="text-sm font-medium">
                Monthly goal
                <input
                  aria-label="Personal monthly service goal"
                  className={`ml-2 min-h-11 w-[4.5rem] rounded border border-[var(--line-control)] bg-[var(--bz-card)] px-2 tabular-nums ${FOCUS}`}
                  required
                  type="number"
                  min="1"
                  max="200"
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  autoFocus
                />
              </label>
              <button
                disabled={busy}
                type="submit"
                className={`min-h-11 rounded bg-[var(--tx-pure)] px-3 text-sm font-semibold text-[var(--bz-card)] disabled:opacity-60 ${FOCUS}`}
              >
                {busy ? "Saving…" : "Save"}
              </button>
              <button
                disabled={busy}
                type="button"
                className={`min-h-11 px-2 text-sm hover:underline ${FOCUS}`}
                onClick={() => setEditing(false)}
              >
                Cancel
              </button>
              {saveError && (
                <p
                  role="alert"
                  className="w-full text-sm text-[var(--state-warning)]"
                >
                  Your goal could not be saved. Try again.
                </p>
              )}
            </form>
          )}
        </section>
      </div>

      {awaiting.length > 0 && (
        <section aria-label="Awaiting payment" className="grid gap-1 py-3">
          <h3 className={sectionTitle}>Awaiting payment</h3>
          {awaiting.map((row) => (
            <p
              key={row.bonus_month}
              className="text-xs leading-relaxed text-[var(--bz-text-2)]"
            >
              {bonusMonthLabel(row.bonus_month)} ·{" "}
              <strong>{money(row.amount_idr)}</strong> gross ·{" "}
              {bonusPaydayText(row.expected_payday, row.payday_salary_month)}
              {row.pending_idr > 0 &&
                ` · ${money(row.pending_idr)} still pending, not paid by this run`}
            </p>
          ))}
        </section>
      )}

      {holds.length > 0 && (
        <section aria-label="Visa bonus payout holds" className={styles.holds}>
          <h3 className={sectionTitle}>
            <CircleAlert
              size={16}
              className="text-[var(--state-warning)]"
              aria-hidden
            />
            Visa bonus payout holds{" "}
            <span className="rounded border border-[var(--bz-border)] px-1.5 text-xs">
              {holds.length}
            </span>
          </h3>
          <PermitHold row={holds[0]} />
          {holds.length > 1 && (
            <details className={styles.disclosure}>
              <summary>
                View {holds.length - 1} more{" "}
                {holds.length === 2 ? "hold" : "holds"}
                <ChevronDown size={15} aria-hidden />
              </summary>
              <ul>
                {holds.slice(1).map((row) => (
                  <li key={row.practice_id}>
                    <PermitHold row={row} />
                  </li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}

      <div className={styles.work} data-split={Boolean(next)}>
        {next && (
          <section aria-label="Next paid practice" className={styles.next}>
            <h3 className={sectionTitle}>
              <FileCheck2 size={16} aria-hidden />
              Next paid practice
            </h3>
            <div className={styles.casefile}>
              <span className="text-[11px] font-bold tracking-[0.07em] text-[var(--bz-text-2)] uppercase">
                Practice #{next.practice_id}
              </span>
              <Link
                className={styles.caseLink}
                href={`/process/${next.practice_id}#final-delivery`}
              >
                <span>
                  {next.service}
                  {bound.nextFile && ` · ${bound.nextFile}`}
                </span>
                <ArrowUpRight aria-hidden size={17} />
              </Link>
              {bound.requirements && (
                <div className="flex items-center justify-between gap-2 border-t border-[var(--bz-border)] pt-1.5 text-xs leading-relaxed text-[var(--bz-text-2)]">
                  <span>Requirements met</span>
                  <strong>
                    {bound.requirements.met} / {bound.requirements.total}
                  </strong>
                </div>
              )}
              {bound.nextDue && (
                <p className="mt-1 text-xs leading-relaxed text-[var(--state-warning)]">
                  Permit due{" "}
                  <time dateTime={bound.nextDue}>
                    {baliDate(bound.nextDue, {
                      day: "numeric",
                      month: "short",
                      year: "numeric",
                    })}
                  </time>
                </p>
              )}
              <p className="mt-1 text-xs leading-relaxed text-[var(--bz-text-2)]">
                {next.already_earned
                  ? "Bonus milestone already recorded"
                  : next.amount_idr === null
                    ? "Bonus rate to confirm"
                    : next.forfeits
                      ? "No bonus · the client paid less than the price"
                      : next.amount_idr === 0
                        ? "No bonus for this service"
                        : `Potential bonus · ${money(next.amount_idr)}`}
              </p>
            </div>
          </section>
        )}
        <section
          id={activityId}
          className={styles.activity}
          aria-label="Recent qualifying work"
        >
          <h3 className={sectionTitle}>
            <History size={16} aria-hidden />
            Recent qualifying work
          </h3>
          {selectedStage && (
            <div className="flex flex-wrap items-center justify-between gap-x-3 text-xs">
              <span>
                {statusNames[selectedStage]} · {month} bonuses
              </span>
              <button
                type="button"
                className="min-h-11 font-semibold underline underline-offset-4"
                onClick={() => selectStage(null)}
              >
                Show all
              </button>
            </div>
          )}
          {recent.length === 0 ? (
            <div className="flex items-center gap-2.5 py-3.5 text-[13px] text-[var(--bz-text-2)]">
              <ChartNoAxesCombined size={20} strokeWidth={1.5} aria-hidden />
              <p>
                {selectedStage
                  ? "No matching deliveries in recent activity."
                  : "Your first qualifying delivery will appear here."}
              </p>
            </div>
          ) : (
            <ul>
              {(selectedStage ? recent : recent.slice(0, 1)).map((row) => (
                <li key={row.practice_id}>
                  <RecentBonus row={row} />
                </li>
              ))}
            </ul>
          )}
          {!selectedStage && recent.length > 1 && (
            <details className={styles.disclosure}>
              <summary>
                View {recent.length - 1} earlier{" "}
                {recent.length === 2 ? "delivery" : "deliveries"}
                <ChevronDown size={15} aria-hidden />
              </summary>
              <ul>
                {recent.slice(1).map((row) => (
                  <li key={row.practice_id}>
                    <RecentBonus row={row} />
                  </li>
                ))}
              </ul>
            </details>
          )}
        </section>
      </div>
      {error && (
        <button
          className="mx-[var(--pad)] mb-2 inline-flex min-h-11 items-center text-xs font-semibold text-[var(--bz-copper-text)] underline-offset-4 hover:underline"
          type="button"
          onClick={() => refresh((v) => v + 1)}
        >
          Retry loading bonus details
        </button>
      )}
    </section>
  );
}

function PermitHold({ row }: { row: Hold }) {
  return (
    <div className={styles.holdRow}>
      <div>
        <Link
          className={rowLink}
          href={`/process/${row.practice_id}#final-delivery`}
        >
          Case #{row.practice_id} · {row.service}
          <ArrowUpRight size={15} aria-hidden />
        </Link>
        <p className="text-xs leading-relaxed text-[var(--bz-text-2)]">
          {holdActions[row.next_action]}
        </p>
      </div>
      <div className="self-center border-s border-[var(--bz-border)] ps-2.5">
        <p className="text-xs leading-relaxed font-semibold">
          {row.slot_label || "Required permit"} · Due{" "}
          <time dateTime={row.due_on}>
            {baliDate(row.due_on, {
              day: "numeric",
              month: "short",
              year: "numeric",
            })}
          </time>
        </p>
        <p className="text-xs leading-relaxed text-[var(--bz-text-2)]">
          Earliest bonus month ·{" "}
          {baliDate(row.payout_from_month, { month: "long", year: "numeric" })}
        </p>
      </div>
    </div>
  );
}

function RecentBonus({ row }: { row: Earned }) {
  const milestone =
    row.event_kind === "electronic_visa_issued"
      ? "Electronic Visa uploaded"
      : row.event_kind === "kitas_imk_uploaded"
        ? "Renewed IMK uploaded"
        : "Delivered";
  // A receipt that carries no bonus or was forfeited never reaches a payroll.
  const noPayroll = row.status === "no_bonus" || row.status === "forfeited";
  return (
    <div className={styles.entry}>
      <time className={styles.dateStamp} dateTime={row.earned_at}>
        <span>{baliDate(row.earned_at, { month: "short" })}</span>
        <strong>{baliDate(row.earned_at, { day: "2-digit" })}</strong>
      </time>
      <div className="min-w-0">
        <Link href={`/process/${row.practice_id}`} className={rowLink}>
          {row.service}
          <ArrowUpRight size={13} aria-hidden />
        </Link>
        <p className="text-xs leading-relaxed text-[var(--bz-text-2)]">
          {milestone}
          {!noPayroll && (
            <>
              {" "}
              ·{" "}
              {baliDate(row.awarded_at, {
                month: "short",
                year: "numeric",
              })}{" "}
              bonuses
            </>
          )}
        </p>
      </div>
      <div className={styles.entryMoney}>
        {row.amount_idr !== null && !noPayroll && (
          <strong>{money(row.amount_idr)}</strong>
        )}
        <span className="text-xs text-[var(--bz-text-2)]">
          {statusNames[row.status] ?? row.status}
        </span>
      </div>
    </div>
  );
}
