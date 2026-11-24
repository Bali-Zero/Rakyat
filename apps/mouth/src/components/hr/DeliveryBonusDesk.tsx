"use client";

import Link from "next/link";
import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  acceptResponse,
  isSameIdentity,
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
  identity: SessionIdentity;
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
  const [bound, setBound] = useState<BoundDesk | null>(null);
  const [generation, refresh] = useState(0);
  const [goal, setGoal] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => {
    setEditing(false);
    setGoal("");
    setBusy(false);
  }, [identity]);
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
    if (!identity) return;
    async function read(owner: SessionIdentity) {
      try {
        const res = await api.requestWithCredential<Desk>(
          "/api/hr/me/delivery-desk",
        );
        if (!live || !acceptResponse(res, owner, res.data.team_member_id))
          return;
        setBound({ identity: owner, data: res.data });
        const next = res.data.next_case;
        if (!next) return;
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
        if (!live || !acceptResponse(pack, owner)) return;
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
        setBound({
          identity: owner,
          data: res.data,
          nextDue: openFollowup ? followup.due_on : undefined,
          nextFile: pack.data.needs_titles
            ? "Define the agreed documents"
            : (needed?.label ?? "Confirm delivery"),
        });
      } catch {
        if (live) setError(true);
      }
    }
    void read(identity);
    return () => {
      live = false;
    };
  }, [identity, generation]);
  const value =
    identity && bound && isSameIdentity(identity, bound.identity)
      ? bound
      : null;
  const data = value?.data;
  if (!identity || (data && !data.on_payroll)) return null;
  if (!data)
    return error ? (
      <button type="button" onClick={() => refresh((v) => v + 1)}>
        Reload my delivery bonuses
      </button>
    ) : null;

  async function saveGoal(event: React.FormEvent) {
    event.preventDefault();
    if (!identity || busy) return;
    const target = Number(goal);
    if (!Number.isInteger(target) || target < 1 || target > 200) return;
    setBusy(true);
    try {
      const res = await api.requestWithCredential<Desk>(
        "/api/hr/me/delivery-goal",
        { method: "PUT", body: JSON.stringify({ target }) },
      );
      if (acceptResponse(res, identity, res.data.team_member_id)) {
        setBound({ identity, data: res.data });
        setEditing(false);
        refresh((v) => v + 1);
      }
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      aria-label="My delivery bonuses"
      data-sentry-block=""
      className="sentry-block mb-5 rounded-xl border border-[var(--bz-border)] bg-[var(--bz-card)] p-4 text-[var(--bz-text-1)] md:p-5"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-xl font-semibold">My delivery bonuses</h2>
        <span className="text-sm text-[var(--bz-text-2)]">
          {baliDate(data.month, { month: "long", year: "numeric" })}
        </span>
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div>
          <strong className="text-3xl tabular-nums">
            {money(
              data.totals.pending + data.totals.approved + data.totals.paid,
            )}
          </strong>
          <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-2 text-sm">
            {(["pending", "approved", "paid"] as const).map((key) => (
              <div key={key}>
                <dt className="text-[var(--bz-text-2)]">{statusNames[key]}</dt>
                <dd>{money(data.totals[key])}</dd>
              </div>
            ))}
          </dl>
          {data.totals.awaiting_rate > 0 && (
            <p className="mt-2 text-sm">
              {data.totals.awaiting_rate} awaiting a rate
            </p>
          )}
          {data.totals.payroll_locked > 0 && (
            <p className="mt-2 text-sm">
              {data.totals.payroll_locked} awaiting payroll review
            </p>
          )}
        </div>
        <div>
          <p className="text-sm text-[var(--bz-text-2)]">
            My qualifying services this month
          </p>
          <strong className="text-2xl">
            {data.milestones}
            {data.target ? ` / ${data.target}` : ""}
          </strong>
          {data.target && (
            <>
              <progress
                className="block w-full accent-[var(--bz-accent)]"
                aria-label="Monthly service goal"
                max={data.target}
                value={Math.min(data.target, data.milestones)}
              />
              <p className="text-sm">
                {data.milestones >= data.target
                  ? "Monthly goal reached"
                  : `${data.target - data.milestones} more to reach your goal`}
              </p>
            </>
          )}
          <button
            type="button"
            className="mt-1 text-sm underline"
            onClick={() => {
              setGoal(String(data.target ?? ""));
              setEditing(!editing);
            }}
          >
            {data.target ? "Edit personal goal" : "Set a personal goal"}
          </button>
          {editing && (
            <form onSubmit={saveGoal} className="mt-2 flex gap-2">
              <input
                aria-label="Personal monthly service goal"
                className="w-20 rounded border bg-[var(--bz-surface)] px-2"
                required
                type="number"
                min="1"
                max="200"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
              />
              <button disabled={busy} type="submit" className="underline">
                Save
              </button>
            </form>
          )}
        </div>
      </div>
      {!!data.holds?.length && (
        <section
          className="mt-4 border-t border-[var(--bz-border)] pt-3"
          aria-label="Visa bonus payout holds"
        >
          <h3 className="text-sm font-semibold">Visa bonus payout holds</h3>
          <ul className="mt-1 space-y-2 text-sm">
            {data.holds.map((row) => (
              <li key={row.practice_id}>
                <Link
                  href={`/process/${row.practice_id}#final-delivery`}
                  className="font-semibold underline"
                >
                  Case #{row.practice_id} · {row.service}
                </Link>
                <p>
                  {holdActions[row.next_action]} · Due{" "}
                  <time dateTime={row.due_on}>{row.due_on}</time>
                </p>
                <p className="text-[var(--bz-text-2)]">
                  Earliest payroll month · {row.payout_from_month.slice(0, 7)}
                </p>
              </li>
            ))}
          </ul>
        </section>
      )}
      {data.next_case &&
        !data.holds?.some(
          (row) => row.practice_id === data.next_case?.practice_id,
        ) && (
          <div className="mt-4 border-t border-[var(--bz-border)] pt-3">
            <h3 className="text-sm font-semibold">Next paid practice</h3>
            <Link
              className="text-[var(--bz-accent)] underline"
              href={`/process/${data.next_case.practice_id}#final-delivery`}
            >
              {data.next_case.service}
              {value?.nextFile && ` · ${value.nextFile}`}
            </Link>
            {value?.nextDue && (
              <p className="text-sm">
                Permit due <time dateTime={value.nextDue}>{value.nextDue}</time>
              </p>
            )}
            <p className="text-sm text-[var(--bz-text-2)]">
              {data.next_case.already_earned
                ? "Bonus milestone already recorded"
                : data.next_case.amount_idr === null
                  ? "Bonus rate to confirm"
                  : `Potential bonus · ${money(data.next_case.amount_idr)}`}
            </p>
          </div>
        )}
      {data.recent.length > 0 && (
        <div className="mt-4 border-t border-[var(--bz-border)] pt-3">
          <h3 className="text-sm font-semibold">Recent qualifying work</h3>
          <ul className="space-y-1">
            {data.recent.map((r) => (
              <li key={r.practice_id} className="text-sm">
                <Link href={`/process/${r.practice_id}`} className="underline">
                  {r.service}
                </Link>{" "}
                ·{" "}
                {r.event_kind === "electronic_visa_issued"
                  ? "Electronic Visa uploaded"
                  : r.event_kind === "kitas_imk_uploaded"
                    ? "Renewed IMK uploaded"
                    : "Delivered"}{" "}
                {baliDate(r.earned_at, { day: "numeric", month: "short" })} ·{" "}
                {r.amount_idr !== null && `${money(r.amount_idr)} · `}
                {statusNames[r.status]} ·{" "}
                {baliDate(r.awarded_at, { month: "short", year: "numeric" })}{" "}
                payroll
              </li>
            ))}
          </ul>
        </div>
      )}
      {error && (
        <button
          className="mt-2 text-sm underline"
          type="button"
          onClick={() => refresh((v) => v + 1)}
        >
          Retry loading bonus details
        </button>
      )}
    </section>
  );
}
