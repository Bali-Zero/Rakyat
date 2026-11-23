"use client";

import { useAccountingMonth } from "./accountingPeriod";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  accountingApi,
  type AccountingCompany,
} from "@/lib/api/workspace/accounting.api";
import { logger } from "@/lib/logger";
import { ccDayDate } from "./ccFormat";

export type CompaniesState =
  | { status: "loading" | "failed"; rows: AccountingCompany[] }
  | { status: "ok"; rows: AccountingCompany[] };

export function useCompanies() {
  const month = useAccountingMonth();
  const [state, setState] = useState<CompaniesState>({
    status: "loading",
    rows: [],
  });
  const request = useRef(0);
  const load = useCallback(async () => {
    const token = ++request.current;
    try {
      const rows = await accountingApi.getCompanies(month);
      if (request.current !== token) return;
      setState(
        Array.isArray(rows)
          ? { status: "ok", rows }
          : { status: "failed", rows: [] },
      );
    } catch (err) {
      if (request.current !== token) return;
      setState((cur) => ({ status: "failed", rows: cur.rows }));
      logger.error("accounting companies load failed", {}, err as Error);
    }
  }, [month]);
  useEffect(() => {
    void load();
  }, [load]);
  return { ...state, reload: load };
}

const CARD =
  "flex min-w-0 flex-col items-start gap-0.5 rounded-xl border px-4 py-3 text-left";
const CARD_ON = `${CARD} border-[var(--cc-act)] bg-[var(--cc-sel)]`;
const CARD_OFF = `${CARD} border-[var(--cc-edge)] bg-[var(--cc-card)] hover:bg-[var(--cc-sel)]`;

function Card({
  testId,
  selected,
  disabled,
  title,
  detail,
  status,
  onClick,
}: {
  testId: string;
  selected: boolean;
  disabled: boolean;
  title: string;
  detail: string;
  status: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      data-testid={testId}
      disabled={disabled}
      onClick={onClick}
      className={selected ? CARD_ON : CARD_OFF}
    >
      <span className="w-full truncate text-sm font-semibold text-[var(--cc-tx)]">
        {title}
      </span>
      <span className="w-full truncate text-xs text-[var(--cc-tx-2)]">
        {detail}
      </span>
      <span className="w-full truncate text-xs font-medium text-[var(--cc-tx)]">
        {status}
      </span>
    </button>
  );
}

export function companyStatus(row: AccountingCompany): string {
  if (row.data_through === null) return "No statement yet";
  const through = `Data through ${ccDayDate(row.data_through)}`;
  return row.to_reconcile > 0
    ? `${through} · ${row.to_reconcile} to reconcile`
    : `${through} · all reconciled`;
}

export function CompanyStrip({
  companies,
  selected,
  locked = false,
  onSelect,
}: {
  companies: CompaniesState;
  selected: string | null;
  /** A write is pending or a dialog is open: the company cannot change under it. */
  locked?: boolean;
  onSelect: (key: string | null) => void;
}) {
  if (companies.status === "failed" && companies.rows.length === 0)
    return (
      <p
        data-testid="cc-companies-failed"
        className="mt-6 text-sm text-[var(--cc-tx-2)]"
      >
        Companies could not be loaded.
      </p>
    );
  if (companies.rows.length === 0) return null;
  const waiting = companies.rows.reduce((sum, r) => sum + r.to_reconcile, 0);
  return (
    <div
      role="radiogroup"
      aria-label="Company"
      data-testid="cc-companies"
      className="mt-6 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3"
    >
      <Card
        testId="cc-company-all"
        selected={selected === null}
        disabled={locked}
        title="All companies"
        detail={`${companies.rows.length} bank accounts`}
        status={`${waiting} to reconcile`}
        onClick={() => onSelect(null)}
      />
      {companies.rows.map((row) => (
        <Card
          key={row.key}
          testId={`cc-company-${row.key}`}
          selected={selected === row.key}
          disabled={locked}
          title={row.company_name}
          detail={[row.bank_name, row.account_number]
            .filter(Boolean)
            .join(" · ")}
          status={companyStatus(row)}
          onClick={() => onSelect(selected === row.key ? null : row.key)}
        />
      ))}
    </div>
  );
}
