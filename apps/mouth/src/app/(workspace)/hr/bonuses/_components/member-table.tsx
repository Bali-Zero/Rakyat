"use client";

import React from "react";
import { Money } from "@balizero/core";
import type { MemberTableRow } from "@/lib/hr/bonus-aggregation";

/** One theme token per department chip: lively, and readable day and night. */
export const DEPT_TONE: Record<string, string> = {
  setup: "var(--state-info)",
  tax: "var(--state-success)",
  office: "var(--bz-accent)",
};
const DEPT_LABEL: Record<string, string> = {
  setup: "Setup",
  tax: "Tax",
  office: "Office",
};

const tint = (tone: string, pct: number) =>
  `color-mix(in srgb, ${tone} ${pct}%, transparent)`;

export function DeptChip({ department }: { department: string }) {
  if (!department) return null;
  const tone = DEPT_TONE[department] ?? "var(--bz-text-2)";
  return (
    <span
      className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
      style={{ background: tint(tone, 14), color: tone }}
    >
      {DEPT_LABEL[department] ?? department}
    </span>
  );
}

interface Props {
  rows: MemberTableRow[];
  monthCount: number;
  bonusCount: number;
  grandPending: number;
  grandApproved: number;
  grandTotal: number;
}

const SOFT = "var(--bz-text-3, var(--bz-text-2))";

/**
 * Total per member across the selection. Every active member is a row — the tax
 * team with nothing yet shows a soft dash, never a missing row. The bar is each
 * person's share of the grand total.
 */
export function MemberTable({
  rows,
  monthCount,
  bonusCount,
  grandPending,
  grandApproved,
  grandTotal,
}: Props) {
  const th = "px-4 py-2 font-medium";
  return (
    <section
      className="overflow-hidden rounded-2xl border"
      style={{ background: "var(--bz-card)", borderColor: "var(--bz-border)" }}
    >
      <div
        className="border-b px-4 py-3"
        style={{ borderColor: "var(--bz-border)" }}
      >
        <h2
          className="text-sm font-semibold"
          style={{ color: "var(--bz-text-1)" }}
        >
          Total per member
        </h2>
        <p className="mt-0.5 text-xs" style={{ color: "var(--bz-text-2)" }}>
          Across every month in the current selection. Rejected and reversed
          bonuses are not counted.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr
              className="text-xs uppercase tracking-wide"
              style={{ color: "var(--bz-text-2)" }}
            >
              <th className={`${th} text-left`}>Member</th>
              <th className={`${th} text-right`}>Months</th>
              <th className={`${th} text-right`}>Bonuses</th>
              <th className={`${th} text-right`}>Pending</th>
              <th className={`${th} text-right`}>Approved</th>
              <th className={`${th} text-right`}>Total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => {
              const empty = m.count === 0;
              const tone = DEPT_TONE[m.department] ?? "var(--bz-accent)";
              return (
                <tr
                  key={m.memberKey}
                  className="border-t"
                  style={{
                    borderColor:
                      "color-mix(in srgb, var(--bz-border) 70%, transparent)",
                    color: empty ? SOFT : "var(--bz-text-1)",
                  }}
                >
                  <td className="px-4 py-2.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span>{m.employeeName}</span>
                      <DeptChip department={m.department} />
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-right tabular-nums">
                    {empty ? "—" : m.monthCount}
                  </td>
                  <td className="px-4 py-2.5 text-right tabular-nums">
                    {empty ? "—" : m.count}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    {m.byStatus.pending ? (
                      <span
                        className="inline-block rounded-full px-2.5 py-0.5 text-xs font-semibold"
                        style={{
                          background: tint("var(--state-warning)", 14),
                          color: "var(--state-warning)",
                        }}
                      >
                        <Money value={m.byStatus.pending} />
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    {m.byStatus.approved ? (
                      <span
                        className="inline-block rounded-full px-2.5 py-0.5 text-xs font-semibold"
                        style={{
                          background: tint("var(--state-success)", 14),
                          color: "var(--state-success)",
                        }}
                      >
                        <Money value={m.byStatus.approved} />
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    {empty ? (
                      "—"
                    ) : (
                      <div className="flex flex-col items-end gap-1">
                        <Money
                          value={m.total}
                          className="font-semibold"
                          style={{ color: "var(--bz-accent)" }}
                        />
                        <div
                          className="h-1.5 w-24 overflow-hidden rounded-full"
                          style={{ background: tint(tone, 14) }}
                          aria-hidden="true"
                        >
                          <div
                            className="h-full rounded-full"
                            style={{
                              width: `${Math.max(m.share > 0 ? 4 : 0, Math.round(m.share * 100))}%`,
                              background: tone,
                            }}
                          />
                        </div>
                        <span className="sr-only">
                          {Math.round(m.share * 100)} percent of the total
                        </span>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr
              className="border-t font-semibold"
              style={{
                borderColor: "var(--bz-border-hover)",
                color: "var(--bz-text-1)",
              }}
            >
              <td className="px-4 py-2.5">All members</td>
              <td className="px-4 py-2.5 text-right tabular-nums">
                {monthCount}
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums">
                {bonusCount}
              </td>
              <td className="px-4 py-2.5 text-right">
                {grandPending ? (
                  <Money
                    value={grandPending}
                    style={{ color: "var(--state-warning)" }}
                  />
                ) : (
                  "—"
                )}
              </td>
              <td className="px-4 py-2.5 text-right">
                <Money value={grandApproved} />
              </td>
              <td className="px-4 py-2.5 text-right">
                <Money
                  value={grandTotal}
                  style={{ color: "var(--bz-accent)" }}
                />
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}
