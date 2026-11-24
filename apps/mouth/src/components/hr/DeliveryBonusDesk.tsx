"use client";

import {
  ArrowUpRight,
  CalendarDays,
  ChevronDown,
  CircleAlert,
  Goal,
  LoaderCircle,
  Pencil,
} from "lucide-react";
import Link from "next/link";
import React, { useEffect, useState } from "react";
import {
  CARD,
  FOCUS,
  NUMERAL_KPI,
  SERIF,
} from "@/components/workspace/r19/tokens";
import { api } from "@/lib/api";
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
  next_case: {
    practice_id: number;
    service: string;
    amount_idr: number | null;
    already_earned: boolean;
  } | null;
};
type BoundDesk = {
  data: Desk;
  nextFile?: string;
  nextDue?: string;
};
const money = (amount: number) => `Rp ${amount.toLocaleString("id-ID")}`;
const baliDate = (value: string, opts: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Makassar",
    ...opts,
  }).format(new Date(value));
const statusNames: Record<string, string> = {
  pending: "Pending approval",
  approved: "Approved",
  paid: "Paid",
  awaiting_rate: "Rate to confirm",
  payroll_locked: "Payroll review",
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
                nextFile: pack.data.needs_titles
                  ? "Define the agreed documents"
                  : (needed?.label ?? "Confirm delivery"),
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
  const next = data.next_case;
  const month = baliDate(data.month, { month: "long", year: "numeric" });
  return (
    <section
      aria-label="My delivery bonuses"
      data-sentry-block=""
      className={`sentry-block mb-5 text-[var(--bz-text-1)] ${CARD}`}
    >
      <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-5 py-4 sm:px-6">
        <h2 className="text-xl tracking-tight" style={SERIF}>
          My delivery bonuses
        </h2>
        <time
          dateTime={data.month.slice(0, 7)}
          className="flex items-center gap-2 text-sm text-[var(--bz-text-2)]"
        >
          <CalendarDays className="size-4" aria-hidden /> {month}
        </time>
      </header>
      <div className="grid border-y border-[var(--bz-border)] md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="min-w-0 bg-[var(--bz-kita-surface-navy)] px-5 py-5 text-[var(--nav-fg)] sm:px-6">
          <p className="text-sm">Bonuses in this payroll month</p>
          <p className={`mt-1 break-words ${NUMERAL_KPI}`} style={SERIF}>
            {money(total)}
          </p>
          <dl className="mt-5 flex flex-wrap gap-x-6 gap-y-3 border-t border-white/25 pt-3 sm:gap-x-9">
            {(["pending", "approved", "paid"] as const).map((key) => (
              <div key={key}>
                <dt className="text-xs text-[var(--nav-fg-muted)]">
                  {statusNames[key]}
                </dt>
                <dd className="mt-0.5 text-sm font-semibold tabular-nums">
                  {money(data.totals[key])}
                </dd>
              </div>
            ))}
          </dl>
          {(data.totals.awaiting_rate > 0 ||
            data.totals.payroll_locked > 0) && (
            <p className="mt-3 flex flex-wrap gap-x-4 text-xs text-[var(--nav-fg-muted)]">
              {data.totals.awaiting_rate > 0 && (
                <span>{data.totals.awaiting_rate} awaiting a rate</span>
              )}
              {data.totals.payroll_locked > 0 && (
                <span>
                  {data.totals.payroll_locked} awaiting payroll review
                </span>
              )}
            </p>
          )}
        </div>
        <div className="flex flex-col justify-center gap-2 px-5 py-4 sm:px-6">
          <div className="flex items-center gap-4">
            <div
              className="grid size-[78px] shrink-0 place-items-center rounded-full"
              role={data.target ? "progressbar" : undefined}
              aria-label={data.target ? "Monthly service goal" : undefined}
              aria-valuemin={data.target ? 0 : undefined}
              aria-valuemax={data.target ?? undefined}
              aria-valuenow={
                data.target ? Math.min(data.target, data.milestones) : undefined
              }
              aria-valuetext={
                data.target
                  ? `${data.milestones} qualifying services, goal ${data.target}`
                  : undefined
              }
              style={{
                background: data.target
                  ? `conic-gradient(var(--state-success) ${Math.min(100, (data.milestones / data.target) * 100)}%, var(--bz-wash) 0)`
                  : "var(--bz-wash)",
              }}
            >
              <span
                aria-hidden={!!data.target}
                className="grid size-[68px] place-content-center rounded-full bg-[var(--bz-card)] text-center text-[28px] leading-none tabular-nums"
                style={SERIF}
              >
                {data.milestones}
                {data.target && (
                  <small className="mt-0.5 text-xs text-[var(--bz-text-2)]">
                    of {data.target}
                  </small>
                )}
              </span>
            </div>
            <div className="min-w-0">
              <h3 className="text-sm font-semibold">
                Qualifying services this month
              </h3>
              <p className="mt-1 text-sm text-[var(--bz-text-2)]">
                {data.target
                  ? reached
                    ? "Monthly goal reached"
                    : `${data.target - data.milestones} more to reach your goal`
                  : "Choose a monthly service goal"}
              </p>
              {!editing && (
                <button
                  type="button"
                  className={`inline-flex min-h-11 items-center gap-1.5 text-sm font-semibold text-[var(--bz-copper-text)] hover:underline ${FOCUS}`}
                  onClick={() => {
                    setGoal(String(data.target ?? ""));
                    setSaveError(false);
                    setEditing(true);
                  }}
                >
                  {data.target ? (
                    <Pencil className="size-3.5" aria-hidden />
                  ) : (
                    <Goal className="size-4" aria-hidden />
                  )}
                  {data.target ? "Edit personal goal" : "Set a personal goal"}
                </button>
              )}
            </div>
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
        </div>
      </div>
      {holds.length > 0 && (
        <section
          aria-label="Visa bonus payout holds"
          className="border-b border-[var(--bz-border)] bg-[var(--bz-accent-subtle)] px-5 py-4 sm:px-6"
        >
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <CircleAlert
              aria-hidden
              className="size-4 text-[var(--state-warning)]"
            />{" "}
            Visa bonus payout holds{" "}
            <span className="tabular-nums">({holds.length})</span>
          </h3>
          <PermitHold row={holds[0]} />
          {holds.length > 1 && (
            <details className="group mt-1">
              <summary className={summaryClass}>
                View {holds.length - 1} more{" "}
                {holds.length === 2 ? "hold" : "holds"}{" "}
                <ChevronDown aria-hidden className={chevronClass} />
              </summary>
              <ul className="divide-y divide-[var(--bz-border)]">
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
      {next && !holds.some((row) => row.practice_id === next.practice_id) && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--bz-border)] px-5 py-3 sm:px-6">
          <div className="min-w-0">
            <h3 className="text-xs font-semibold text-[var(--bz-text-2)]">
              Next paid practice
            </h3>
            <Link
              className={`inline-flex min-h-11 items-center gap-2 font-semibold hover:underline ${FOCUS}`}
              href={`/process/${next.practice_id}#final-delivery`}
            >
              {next.service}
              {bound.nextFile && ` · ${bound.nextFile}`}{" "}
              <ArrowUpRight
                aria-hidden
                className="size-4 shrink-0 text-[var(--bz-copper-text)]"
              />
            </Link>
            {bound.nextDue && (
              <p className="text-sm">
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
          </div>
          <p className="text-sm text-[var(--bz-text-2)]">
            {next.already_earned
              ? "Bonus milestone already recorded"
              : next.amount_idr === null
                ? "Bonus rate to confirm"
                : `Potential bonus · ${money(next.amount_idr)}`}
          </p>
        </div>
      )}
      <div className="px-5 py-3 sm:px-6">
        <h3 className="text-xs font-semibold text-[var(--bz-text-2)]">
          Recent qualifying work
        </h3>
        {data.recent.length === 0 ? (
          <p className="mt-1 text-sm">
            Your first qualifying delivery will appear here.
          </p>
        ) : (
          <RecentBonus row={data.recent[0]} />
        )}
        {data.recent.length > 1 && (
          <details className="group">
            <summary className={summaryClass}>
              View {data.recent.length - 1} earlier{" "}
              {data.recent.length === 2 ? "delivery" : "deliveries"}{" "}
              <ChevronDown aria-hidden className={chevronClass} />
            </summary>
            <ul className="divide-y divide-[var(--bz-border)]">
              {data.recent.slice(1).map((row) => (
                <li key={row.practice_id}>
                  <RecentBonus row={row} />
                </li>
              ))}
            </ul>
          </details>
        )}
        {error && (
          <button
            className={`mt-2 min-h-11 text-sm underline ${FOCUS}`}
            type="button"
            onClick={() => refresh((v) => v + 1)}
          >
            Retry loading bonus details
          </button>
        )}
      </div>
    </section>
  );
}

const summaryClass = `flex min-h-11 w-fit cursor-pointer list-none items-center gap-2 text-sm font-semibold text-[var(--bz-text-1)] hover:underline [&::-webkit-details-marker]:hidden ${FOCUS}`;
const chevronClass =
  "size-4 transition-transform group-open:rotate-180 motion-reduce:transition-none";

function PermitHold({ row }: { row: Hold }) {
  return (
    <div className="mt-1 pb-1 text-sm">
      <Link
        href={`/process/${row.practice_id}#final-delivery`}
        className={`inline-flex min-h-11 items-center gap-2 font-semibold hover:underline ${FOCUS}`}
      >
        Case #{row.practice_id} · {row.service}{" "}
        <ArrowUpRight className="size-4" aria-hidden />
      </Link>
      <p>{holdActions[row.next_action]}</p>
      <p className="mt-1 text-sm">
        {row.slot_label || "Required permit"} · Due{" "}
        <time dateTime={row.due_on}>
          {baliDate(row.due_on, {
            day: "numeric",
            month: "short",
            year: "numeric",
          })}
        </time>
      </p>
      <p className="mt-1 text-xs text-[var(--bz-text-2)]">
        Earliest payroll month ·{" "}
        {baliDate(row.payout_from_month, { month: "long", year: "numeric" })}
      </p>
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
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-1.5">
      <div className="min-w-0">
        <Link
          href={`/process/${row.practice_id}`}
          className={`inline-flex min-h-11 items-center gap-1.5 text-sm font-semibold hover:underline ${FOCUS}`}
        >
          {row.service}{" "}
          <ArrowUpRight
            className="size-3.5 text-[var(--bz-text-2)]"
            aria-hidden
          />
        </Link>
        <p className="text-xs text-[var(--bz-text-2)]">
          {milestone}{" "}
          {baliDate(row.earned_at, { day: "numeric", month: "short" })} ·{" "}
          {baliDate(row.awarded_at, { month: "short", year: "numeric" })}{" "}
          payroll
        </p>
      </div>
      <div className="text-sm sm:text-right">
        {row.amount_idr !== null && (
          <div className="font-semibold tabular-nums">
            {money(row.amount_idr)}
          </div>
        )}
        <span className="text-xs text-[var(--bz-text-2)]">
          {statusNames[row.status]}
        </span>
      </div>
    </div>
  );
}
