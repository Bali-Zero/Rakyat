"use client";

import React, { useState } from "react";
import { CheckCircle, Trash2, XCircle } from "lucide-react";
import { Money } from "@balizero/core";
import { formatBonusDate } from "@/lib/hr/bonus-aggregation";
import type { Bonus } from "@/types/hr";

const tint = (tone: string, pct: number) =>
  `color-mix(in srgb, ${tone} ${pct}%, transparent)`;

/** Bonus-status pill, honestly mapped to --state-*. */
const STATUS_TONE: Record<string, string> = {
  pending: "var(--state-warning)",
  approved: "var(--state-success)",
  paid: "var(--state-success)",
  rejected: "var(--state-danger)",
  reversed: "var(--bz-text-2)",
};

export function StatusPill({ status }: { status: string }) {
  const tone = STATUS_TONE[status] ?? "var(--bz-text-2)";
  return (
    <span
      className="rounded-full px-2.5 py-0.5 text-[11px] font-semibold capitalize"
      style={{ background: tint(tone, 14), color: tone }}
    >
      {status}
    </span>
  );
}

export function SourceBadge({ source }: { source: Bonus["source"] }) {
  const manual = source === "manual";
  const tone = manual ? "var(--bz-accent)" : "var(--bz-text-2)";
  return (
    <span
      className="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
      style={{ background: tint(tone, manual ? 14 : 10), color: tone }}
    >
      {manual ? "Manual" : "Practice"}
    </span>
  );
}

interface Props {
  bonus: Bonus;
  isAdmin: boolean;
  onApprove: (id: number) => void;
  onReject: (id: number) => void;
  onDelete: (id: number) => void;
}

const BTN =
  "flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold";

export function BonusRow({
  bonus,
  isAdmin,
  onApprove,
  onReject,
  onDelete,
}: Props) {
  const [confirm, setConfirm] = useState<"reject" | "delete" | null>(null);
  const manual = bonus.source === "manual";
  const pending = bonus.status === "pending";
  const service =
    bonus.practice_type_code && bonus.practice_type_code !== "manual"
      ? bonus.practice_type_code.replace(/_/g, " ")
      : null;
  const title = manual ? (service ?? "Manual bonus") : service;

  const ask = (
    kind: "reject" | "delete",
    id: number,
    run: (id: number) => void,
  ) =>
    confirm === kind ? (
      <span className="flex items-center gap-1 text-xs">
        <button
          type="button"
          onClick={() => {
            setConfirm(null);
            run(id);
          }}
          className={BTN}
          style={{
            background: tint("var(--state-danger)", 16),
            color: "var(--state-danger)",
          }}
        >
          Yes, {kind}
        </button>
        <button
          type="button"
          onClick={() => setConfirm(null)}
          className={BTN}
          style={{ color: "var(--bz-text-2)" }}
        >
          Keep
        </button>
      </span>
    ) : (
      <button
        type="button"
        onClick={() => setConfirm(kind)}
        className={BTN}
        style={{
          background: tint("var(--state-danger)", 10),
          color: "var(--state-danger)",
        }}
      >
        {kind === "reject" ? <XCircle size={12} /> : <Trash2 size={12} />}
        {kind === "reject" ? "Reject" : "Delete"}
      </button>
    );

  return (
    <li
      className="flex items-center justify-between gap-3 border-t px-4 py-2.5 pl-14"
      style={{
        borderColor: "color-mix(in srgb, var(--bz-border) 50%, transparent)",
      }}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm" style={{ color: "var(--bz-text-1)" }}>
            {title}
          </span>
          <SourceBadge source={bonus.source} />
          <StatusPill status={bonus.status} />
        </div>
        <div
          className="mt-0.5 truncate text-xs"
          style={{ color: "var(--bz-text-2)" }}
        >
          {formatBonusDate(bonus.awarded_at)}
          {bonus.client_name && ` — ${bonus.client_name}`}
          {manual && bonus.reason && ` — ${bonus.reason}`}
          {manual &&
            isAdmin &&
            bonus.created_by &&
            ` · added by ${bonus.created_by}`}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2.5">
        <Money
          value={Number(bonus.amount_idr) || 0}
          className="text-sm"
          style={{ color: "var(--bz-text-1)" }}
        />
        {isAdmin && pending && (
          <>
            <button
              type="button"
              onClick={() => onApprove(bonus.id)}
              className={BTN}
              style={{
                background: tint("var(--state-success)", 14),
                color: "var(--state-success)",
              }}
            >
              <CheckCircle size={12} />
              Approve
            </button>
            {ask("reject", bonus.id, onReject)}
            {manual && ask("delete", bonus.id, onDelete)}
          </>
        )}
      </div>
    </li>
  );
}
