"use client";

import React, { useState, useCallback, memo, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type {
  Client,
  ClientCompanyLink,
  ClientDocument,
  AttentionItem,
} from "@/lib/api/crm/crm.types";
import { lkpmApi } from "@/lib/api/workspace/lkpm.api";
import type { LKPMBatchItem, LKPMReceipt } from "@/lib/api/portal/portal.types";
import {
  CellStack,
  EYEBROW,
  FOCUS,
  HairlineBody,
  HairlineGrid,
  HairlineHead,
  HairlineRow,
  LedgerSection,
  MICRO_LABEL,
  StatePill,
  type PillTone,
} from "@/components/workspace/r19";
import {
  coretaxStatus,
  formatNpwp,
  isSixteenDigits,
  type CoretaxStatus,
} from "./coretax";
import {
  formatDayMonth,
  formatFullDate,
  isoDayMs,
  sptFilings,
  sptLabel,
  sptStatus,
  type SptDocumentLike,
  type SptFiling,
  type SptKind,
  type SptState,
  type AttentionState,
} from "./tax-deadlines";
import { witaDateKey } from "./overview-brief";
import { dayToInstant, resolveCardDay } from "./witaClock";
import { TaxDocumentsSection } from "./TaxDocumentsSection";

// ============================================
// TAX CONSULTANT DROPDOWN (Bali Zero tax team)
// ============================================
// The five allowed values USED to be a literal here. They are not any more: this
// is a "use client" module, so a literal in it is compiled into
// `app/(workspace)/clients/[id]/page-*.js`, and that chunk is served from the
// static CDN path with no session — an anonymous
// `curl https://balizero.com/_next/static/chunks/app/(workspace)/clients/%5Bid%5D/page-*.js`
// returned 200 and a name the owner had excluded from public surfaces. Measured,
// not theorised: 2 marker hits in that chunk on production as of 2026-09-12.
//
// The list now arrives as a prop from the server (`page.tsx` ->
// `taxConsultants()` in `@/lib/workspace/roster-directory`, which is the module
// that stays in sync with backend migration 093's CHECK constraint). Only the
// TYPE crosses this boundary, and `import type` is erased at compile time.
import type { TaxConsultantOption } from "@/lib/workspace/roster-directory";
import { legacyConsultantLabel } from "./header-facts";

// Still importable from here (tests, older callers); it lives in header-facts
// so the page can use it without statically importing this whole tab.
export { legacyConsultantLabel };

type TaxYear = number;

// ============================================
// YEAR SELECTOR — module-scope to prevent remount
// ============================================
interface YearSelectorProps {
  selectedYear: TaxYear;
  /** The card's year (WITA year of the payload's `as_of`), newest on the list. */
  currentYear: TaxYear;
  onYearChange: (year: TaxYear) => void;
}

const YearSelector = memo(function YearSelector({
  selectedYear,
  currentYear,
  onYearChange,
}: YearSelectorProps) {
  const years = Array.from({ length: 5 }, (_, i) => currentYear - i);

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-sm text-[var(--bz-text-2)]">Year:</span>
      <div className="flex flex-wrap gap-1">
        {years.map((year) => (
          <Button
            key={year}
            variant="outline"
            size="sm"
            className={
              selectedYear === year
                ? "h-8 px-3 text-xs border-[var(--tx-pure)] text-[var(--tx-pure)]"
                : "h-8 px-3 text-xs"
            }
            onClick={() => onYearChange(year)}
          >
            {year}
          </Button>
        ))}
      </div>
    </div>
  );
});

// ============================================
// TAX CONSULTANT SELECTOR
// ============================================
interface TaxConsultantSelectorProps {
  clientId: number;
  initialValue: string | null | undefined;
  onSaved?: () => Promise<void> | void;
  /** Server-supplied; required, so an empty dropdown cannot pass unnoticed. */
  consultants: readonly TaxConsultantOption[];
}

const TaxConsultantSelector = memo(function TaxConsultantSelector({
  clientId,
  initialValue,
  onSaved,
  consultants,
}: TaxConsultantSelectorProps) {
  const [value, setValue] = useState<string>(initialValue ?? "");
  const [isSaving, setIsSaving] = useState(false);

  // Keep local state in sync if the parent's initialValue changes
  // (e.g. after an external refresh).
  useEffect(() => {
    setValue(initialValue ?? "");
  }, [initialValue]);

  const handleChange = useCallback(
    async (e: React.ChangeEvent<HTMLSelectElement>) => {
      const newValue = e.target.value;
      const previous = value;
      setValue(newValue);
      setIsSaving(true);
      try {
        const user = await api.getProfile();
        // null clears the assignment; backend accepts explicit null for this field.
        await api.crm.updateClient(
          clientId,
          { tax_consultant: newValue || null },
          user.email,
        );
        toast.success(
          newValue
            ? `Tax consultant: ${consultants.find((c) => c.value === newValue)?.label ?? newValue}`
            : "Tax consultant cleared",
        );
        await onSaved?.();
      } catch (err) {
        setValue(previous); // revert on error
        toast.error("Failed to update tax consultant", {
          description: (err as Error).message,
        });
      } finally {
        setIsSaving(false);
      }
    },
    [clientId, value, onSaved, consultants],
  );

  return (
    <div className="min-w-0">
      <label htmlFor={`tax-consultant-${clientId}`} className={EYEBROW}>
        Tax Consultant
      </label>
      <div className="mt-1.5 flex items-center gap-2">
        <select
          id={`tax-consultant-${clientId}`}
          value={value}
          onChange={handleChange}
          disabled={isSaving}
          className={`min-h-9 w-full max-w-[240px] border border-[var(--line-control)] bg-transparent px-2.5 text-[13px] text-[var(--bz-text-1)] ${FOCUS} disabled:opacity-60`}
        >
          <option value="">— not assigned —</option>
          {value && !consultants.some((c) => c.value === value) && (
            <option value={value} disabled>
              {legacyConsultantLabel(value)}
            </option>
          )}
          {consultants.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        {isSaving && (
          <span className="text-[12px] text-[var(--tx-secondary)]">
            Saving…
          </span>
        )}
      </div>
    </div>
  );
});

// ============================================
// TAX IDENTITY KV ITEM
// ============================================
// OLD TaxIdBadge (HEAD~:168-220) had three branches: the client's own value,
// a company fallback ("via company" + value + company name), and
// "not registered". All three words survive in the kv grid — the old tinted
// boxes (emerald/warning fills) are gone per the r19 no-fill law; the facts
// are carried by the words alone.
function TaxIdItem({
  label,
  value,
  fallbackValue,
  fallbackLabel,
  note,
}: {
  label: string;
  value?: string;
  fallbackValue?: string;
  fallbackLabel?: string;
  /** Quiet sub-line under an own value (e.g. "NIK-based"). */
  note?: string;
}) {
  return (
    <div className="min-w-0">
      <p className={EYEBROW}>{label}</p>
      {value ? (
        <>
          <p className="mt-1.5 truncate font-mono text-[13px] text-[var(--tx-pure)]">
            {formatNpwp(value)}
          </p>
          {note ? (
            <p className="mt-1 truncate text-[12px] text-[var(--tx-secondary)]">
              {note}
            </p>
          ) : null}
        </>
      ) : fallbackValue ? (
        <>
          <p className="mt-1.5 truncate font-mono text-[13px] text-[var(--tx-pure)]">
            {formatNpwp(fallbackValue)}
          </p>
          <p className="mt-1 truncate text-[12px] text-[var(--tx-secondary)]">
            via company{fallbackLabel ? ` · ${fallbackLabel}` : ""}
          </p>
        </>
      ) : (
        <p className="mt-1.5 text-[13px] text-[var(--tx-secondary)]">
          Not registered
        </p>
      )}
    </div>
  );
}

// ============================================
// CORETAX ACCESS — read from the client's documents, no database field
// ============================================
function CoretaxAccessItem({
  status,
  formatDate,
}: {
  status: CoretaxStatus;
  formatDate: (d: string) => string;
}) {
  const { coretaxAccess, efin } = status;
  return (
    <div className="min-w-0" data-testid="coretax-access">
      <p className={EYEBROW}>Coretax access</p>
      <p
        className="mt-1.5 text-[13px]"
        style={{
          color: coretaxAccess.onFile
            ? "var(--state-success)"
            : "var(--tx-secondary)",
        }}
      >
        {coretaxAccess.onFile
          ? "Coretax access on file"
          : "No Coretax access on file"}
        {coretaxAccess.onFile && coretaxAccess.date ? (
          <span className="text-[var(--tx-secondary)]">
            {" · "}
            {formatDate(coretaxAccess.date)}
          </span>
        ) : null}
      </p>
      <p
        className="mt-1 text-[12px]"
        style={{
          color: efin.onFile ? "var(--state-success)" : "var(--tx-secondary)",
        }}
      >
        {efin.onFile ? "EFIN on file" : "No EFIN on file"}
      </p>
    </div>
  );
}

// ============================================
// SPT SECTIONS — Personal tax / Company tax
// ============================================
/** The line for an engine item: its state and date, our wording. */
function attentionStatus(item: AttentionItem, fallbackYear: number) {
  const deadlineMs = item.date ? isoDayMs(item.date) : NaN;
  const hasDate = Number.isFinite(deadlineMs);
  const deadlineLabel = hasDate ? formatDayMonth(deadlineMs) : "";
  const opensMs = item.opens_on
    ? isoDayMs(item.opens_on)
    : hasDate
      ? deadlineMs - 30 * 86_400_000
      : NaN;
  const yearInLabel = /(20\d{2})\s*$/.exec(item.label);
  return {
    fiscalYear: yearInLabel ? Number(yearInLabel[1]) : fallbackYear,
    state: item.state,
    label: sptLabel(
      item.state,
      item.days ?? 0,
      deadlineLabel,
      Number.isFinite(opensMs) ? formatFullDate(opensMs) : "",
    ),
  };
}

function SptBlock({
  kind,
  heading,
  docs,
  now,
  attention,
}: {
  kind: SptKind;
  heading?: string;
  docs: readonly SptDocumentLike[];
  now: Date;
  /** The backend engine's item for this subject; wins over the local helper. */
  attention?: AttentionItem;
}) {
  const filings: SptFiling[] = sptFilings(docs, kind);
  const local = sptStatus(kind, new Set(filings.map((f) => f.fiscalYear)), now);
  const status: {
    fiscalYear: number;
    state: SptState | AttentionState;
    label: string;
    isNext?: boolean;
  } = attention ? attentionStatus(attention, local.fiscalYear) : local;
  // The past year whose window closed with no filing: listed quietly in the
  // history, never as an alarm.
  const missingYear =
    local.missingYear != null && status.state !== "done"
      ? local.missingYear
      : null;
  const alarm = status.state === "urgent" || status.state === "overdue";
  const quiet =
    status.state === "none" ||
    status.state === "upcoming" ||
    status.state === "expired";
  return (
    <div data-testid={`spt-block-${kind}`} data-state={status.state}>
      {heading ? <p className={MICRO_LABEL}>{heading}</p> : null}
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2">
        <span className="text-[13px] text-[var(--tx-pure)]">
          {status.isNext ? "Next: " : ""}SPT fiscal year {status.fiscalYear}
        </span>
        {status.state === "done" ? (
          <StatePill tone="ok" label="Filed" />
        ) : (
          <span
            data-testid="spt-status"
            className={`text-[13px]${alarm ? " font-semibold" : ""}`}
            style={{
              color: alarm
                ? "var(--state-warning)"
                : quiet
                  ? "var(--tx-secondary)"
                  : "var(--tx-pure)",
            }}
          >
            {status.label}
          </span>
        )}
      </div>
      {filings.length > 0 || missingYear != null ? (
        <ul
          aria-label="Filing history"
          className="border-t border-[var(--bz-border)]"
        >
          {missingYear != null ? (
            <li
              data-testid="spt-missing-year"
              className="flex min-h-9 items-center gap-3 border-b border-[var(--bz-border)] text-[13px]"
            >
              <span className="text-[var(--tx-pure)]">FY {missingYear}</span>
              <span className="text-[12px] text-[var(--tx-secondary)]">
                no SPT on file
              </span>
            </li>
          ) : null}
          {filings.map((f) => (
            <li
              key={f.fiscalYear}
              className="flex min-h-9 items-center justify-between gap-3 border-b border-[var(--bz-border)] text-[13px]"
            >
              <span>
                <span className="text-[var(--tx-pure)]">FY {f.fiscalYear}</span>
                {f.fileName ? (
                  <span className="ml-2 text-[12px] text-[var(--tx-secondary)]">
                    {f.fileName}
                  </span>
                ) : null}
              </span>
              {f.url ? (
                <a
                  href={f.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Open SPT FY ${f.fiscalYear}`}
                  className={OPEN_LINK_CLASS}
                >
                  Open
                </a>
              ) : (
                <span className="text-[var(--tx-secondary)]">—</span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// ============================================
// LKPM ALERT HEALTH — pure decision, no red-on-kita
// ============================================
// The quarter card used to render a literal red/yellow/green emoji here. Two
// defects: kita's written rule is "no red on kita" — every alert badge in
// this directory already renders urgency as --state-warning, never
// --state-danger (see ClientDetailClient.tsx: "Alert badges — urgency (a
// date), never ownership: warning, never danger") — and an emoji carries no
// accessible name for a screen reader. Extracted so the tone/label decision
// is testable without mounting the whole quarter-row tree.
export function lkpmHealth(
  report: Pick<LKPMBatchItem, "red_alerts" | "yellow_alerts">,
): { tone: "critical" | "warning" | "success"; label: string } {
  if (report.red_alerts > 0) {
    return {
      tone: "critical",
      label: `${report.red_alerts} alert${report.red_alerts === 1 ? "" : "s"} need${report.red_alerts === 1 ? "s" : ""} attention`,
    };
  }
  if (report.yellow_alerts > 0) {
    return {
      tone: "warning",
      label: `${report.yellow_alerts} warning${report.yellow_alerts === 1 ? "" : "s"}`,
    };
  }
  return { tone: "success", label: "No alerts" };
}

// ============================================
// LKPM QUARTER ROWS — one hairline row per quarter
// ============================================
const QUARTER_MONTHS: Record<string, string> = {
  Q1: "Jan-Mar",
  Q2: "Apr-Jun",
  Q3: "Jul-Sep",
  Q4: "Oct-Dec",
};

const LKPM_COLS =
  "3.75rem minmax(6.5rem,0.8fr) minmax(9rem,1.1fr) minmax(8.5rem,1fr) minmax(9rem,1fr) minmax(7.5rem,0.8fr) 3.25rem";

// The OLD status word ladder (HEAD~:283-289): oss_submitted wins, then
// approved / validated, everything else reads "Draft". The ✅ suffix is
// gone — r19 law: no emoji as a status marker; the StatePill's diamond pip
// plus the WORD is the marker now. Tones carry no ownership signal on this
// tab, so nothing here is ever `you` (copper).
function lkpmStatus(report: LKPMBatchItem): { tone: PillTone; label: string } {
  if (report.oss_submitted) return { tone: "ok", label: "Submitted" };
  if (report.status === "approved") return { tone: "ok", label: "Approved" };
  if (report.status === "validated")
    return { tone: "ours", label: "Validated" };
  return { tone: "wait", label: "Draft" };
}

// OLD deadline colouring (HEAD~:300-305): --state-warning at <= 3 and at
// <= 7, calm above. r19 law: a due countdown is never a pill tone — urgency
// lives on the date cell as wording + weight + --state-warning. Both old
// thresholds survive: <= 3 is semibold, 4-7 keeps the warning colour,
// < 0 gains the word "Overdue" (the old raw "-N days" had no word at all).
function lkpmDeadline(report: LKPMBatchItem) {
  if (report.oss_submitted || report.days_to_deadline == null) return null;
  const days = report.days_to_deadline;
  if (days < 0)
    return {
      label: `Overdue by ${Math.abs(days)} day${days === -1 ? "" : "s"}`,
      urgent: true,
      strong: true,
    };
  if (days === 0) return { label: "Due today", urgent: true, strong: true };
  if (days <= 3)
    return {
      label: `Due in ${days} day${days === 1 ? "" : "s"}`,
      urgent: true,
      strong: true,
    };
  if (days <= 7)
    return { label: `Due in ${days} days`, urgent: true, strong: false };
  return { label: `Due in ${days} days`, urgent: false, strong: false };
}

function LkpmQuarterRow({
  quarter,
  report,
}: {
  quarter: string;
  report: LKPMBatchItem | null;
}) {
  if (!report) {
    return (
      <HairlineRow data-testid={`lkpm-row-${quarter}-empty`}>
        <div className="min-w-0 px-2.5 py-3">
          <CellStack
            primary={quarter}
            secondary={`${QUARTER_MONTHS[quarter]} · No report`}
          />
        </div>
        <div className="px-2.5 py-3 text-[13px] text-[var(--tx-secondary)]">
          —
        </div>
        <div className="px-2.5 py-3 text-[13px] text-[var(--tx-secondary)]">
          —
        </div>
        <div className="px-2.5 py-3 text-[13px] text-[var(--tx-secondary)]">
          —
        </div>
        <div className="px-2.5 py-3 text-[13px] text-[var(--tx-secondary)]">
          —
        </div>
        <div className="px-2.5 py-3 text-[13px] text-[var(--tx-secondary)]">
          —
        </div>
        <div className="px-2.5 py-3 text-[13px] text-[var(--tx-secondary)]">
          —
        </div>
      </HairlineRow>
    );
  }

  const status = lkpmStatus(report);
  const health = lkpmHealth(report);
  // Both severities render --state-warning (never --state-danger, per this
  // directory's rule); "critical" is only a stronger opacity of the same hue.
  const healthPipColor =
    health.tone === "critical"
      ? "bg-[var(--state-warning)]"
      : health.tone === "warning"
        ? "bg-[var(--state-warning)]/50"
        : "bg-[var(--state-success)]";
  const deadline = lkpmDeadline(report);
  const assignedName = report.lkpm_assigned_to
    ? report.lkpm_assigned_to
        .split(".")[0]
        .replace(/^\w/, (c) => c.toUpperCase())
    : null;

  return (
    <HairlineRow data-testid={`lkpm-row-${quarter}`}>
      <div className="min-w-0 px-2.5 py-3">
        <CellStack primary={quarter} secondary={QUARTER_MONTHS[quarter]} />
      </div>
      <div className="px-2.5 py-3">
        <StatePill tone={status.tone} label={status.label} />
      </div>
      <div className="min-w-0 px-2.5 py-3">
        <span className="flex min-w-0 items-center gap-1.5">
          <span
            role="img"
            aria-label={health.label}
            className={`inline-block h-[7px] w-[7px] shrink-0 rotate-45 rounded-[1px] ${healthPipColor}`}
          />
          <span className="truncate text-[12px] text-[var(--tx-secondary)]">
            {health.label}
          </span>
        </span>
      </div>
      <div className="px-2.5 py-3">
        {deadline ? (
          <p
            className={`text-[13px]${deadline.strong ? " font-semibold" : ""}`}
            style={{
              color: deadline.urgent
                ? "var(--state-warning)"
                : "var(--tx-secondary)",
            }}
          >
            {deadline.label}
          </p>
        ) : (
          <p className="text-[13px] text-[var(--tx-secondary)]">—</p>
        )}
      </div>
      <div className="min-w-0 px-2.5 py-3">
        {assignedName ? (
          <CellStack
            primary={assignedName}
            secondary={report.lkpm_assigned_to}
          />
        ) : (
          <p className="text-[13px] text-[var(--state-warning)]">Unassigned</p>
        )}
      </div>
      <div className="px-2.5 py-3">
        <p
          className="text-[13px]"
          style={{
            color: report.client_approved
              ? "var(--state-success)"
              : "var(--state-warning)",
          }}
        >
          {report.client_approved ? "Approved" : "Not approved"}
        </p>
      </div>
      <div className="px-2.5 py-3">
        {/* Row controls live in a plain always-rendered cell, never in
            HairlineRow's `actions` slot — that slot is hover-gated and
            hidden under (hover:none) (the defect that rejected R6). */}
        <a
          href={`/lkpm/${report.id}`}
          className="inline-flex min-h-6 items-center text-[13px] text-[var(--tx-pure)] underline-offset-4 hover:underline"
        >
          Open
        </a>
      </div>
    </HairlineRow>
  );
}

function LkpmQuarterGrid({ items }: { items: LKPMBatchItem[] }) {
  return (
    <HairlineGrid
      cols={LKPM_COLS}
      // Same collapse override as FamilyTab (#6520): the scoped collapse
      // stylesheet cannot beat an inline `--cols`, so override on the rows.
      className="max-[640px]:[&_.grid]:!grid-cols-[minmax(0,1fr)]"
    >
      <HairlineHead className="max-[640px]:hidden">
        <span>Quarter</span>
        <span>Status</span>
        <span>Health</span>
        <span>Deadline</span>
        <span>Assigned</span>
        <span>Approval</span>
        <span />
      </HairlineHead>
      <HairlineBody>
        {(["Q1", "Q2", "Q3", "Q4"] as const).map((q) => (
          <LkpmQuarterRow
            key={q}
            quarter={q}
            report={items.find((r) => r.quarter === q) ?? null}
          />
        ))}
      </HairlineBody>
    </HairlineGrid>
  );
}

// ============================================
// LKPM RECEIPTS — OSS tanda terima per kegiatan usaha
// ============================================
const RECEIPT_COLS =
  "3.5rem minmax(8rem,1fr) minmax(5.5rem,0.6fr) minmax(12rem,1.4fr) minmax(7rem,0.8fr) minmax(8rem,0.9fr) minmax(7.5rem,0.8fr) 3.25rem";

/** Receipts shown before the "Show N more" disclosure. */
const RECEIPTS_VISIBLE = 2;

function receiptCompany(r: LKPMReceipt): string {
  return r.company_name ?? r.nama_perusahaan_oss ?? "Unknown PT";
}

const OPEN_LINK_CLASS =
  "inline-flex min-h-6 items-center text-[13px] text-[var(--tx-pure)] underline-offset-4 hover:underline";

function ReceiptGrid({
  receipts,
  formatDate,
  withHead,
}: {
  receipts: LKPMReceipt[];
  formatDate: (d: string) => string;
  withHead: boolean;
}) {
  return (
    <HairlineGrid
      cols={RECEIPT_COLS}
      className="max-[640px]:[&_.grid]:!grid-cols-[minmax(0,1fr)]"
    >
      {withHead ? (
        <HairlineHead className="max-[640px]:hidden">
          <span>Qtr</span>
          <span>Company</span>
          <span>KBLI</span>
          <span>Nomor Laporan</span>
          <span>Stage</span>
          <span>Status</span>
          <span>Date</span>
          <span>PDF</span>
        </HairlineHead>
      ) : null}
      <HairlineBody>
        {receipts.map((r) => (
          <LkpmReceiptRow key={r.id} receipt={r} formatDate={formatDate} />
        ))}
      </HairlineBody>
    </HairlineGrid>
  );
}

function LkpmReceiptRow({
  receipt: r,
  formatDate,
}: {
  receipt: LKPMReceipt;
  formatDate: (d: string) => string;
}) {
  const approved = r.oss_status === "Disetujui";
  return (
    <HairlineRow>
      <div className="px-2.5 py-3 text-[13px]">{r.quarter ?? "—"}</div>
      <div className="min-w-0 truncate px-2.5 py-3 text-[13px]">
        {receiptCompany(r)}
      </div>
      <div className="truncate px-2.5 py-3 font-mono text-[13px]">
        {r.kbli_code ?? "—"}
      </div>
      <div className="min-w-0 px-2.5 py-3">
        {/* OLD carried `nomor_kegiatan_usaha` as title= only — invisible on
            touch; it is a readable sub-line now. */}
        <CellStack
          primary={<span className="font-mono">{r.nomor_laporan}</span>}
          secondary={r.nomor_kegiatan_usaha}
        />
      </div>
      <div className="px-2.5 py-3 text-[13px] text-[var(--tx-secondary)]">
        {r.stage ?? "—"}
      </div>
      <div className="px-2.5 py-3">
        <p
          className="text-[13px]"
          style={{
            color: r.oss_status
              ? approved
                ? "var(--state-success)"
                : "var(--state-warning)"
              : undefined,
          }}
        >
          {r.oss_status ?? "—"}
        </p>
      </div>
      <div className="px-2.5 py-3 text-[13px] text-[var(--tx-secondary)]">
        {r.tanggal_diterima ? formatDate(r.tanggal_diterima) : "—"}
      </div>
      <div className="px-2.5 py-3">
        {r.file_drive_url ? (
          <a
            href={r.file_drive_url}
            target="_blank"
            rel="noopener noreferrer"
            className={OPEN_LINK_CLASS}
          >
            Open
          </a>
        ) : (
          <span className="text-[13px] text-[var(--tx-secondary)]">—</span>
        )}
      </div>
    </HairlineRow>
  );
}

// ============================================
// TAX TAB COMPONENT
// ============================================
export function TaxTab({
  clientId,
  formatDate,
  client,
  companyLinks,
  onRefresh,
  taxConsultants,
  documents,
  now,
  attention,
  day: dayProp,
}: {
  clientId: number;
  formatDate: (d: string) => string;
  client: Client | null;
  companyLinks?: ClientCompanyLink[];
  onRefresh?: () => Promise<void> | void;
  /**
   * The assignable tax team, resolved on the server. REQUIRED and not defaulted:
   * a default here would put the names back into this chunk, which is the whole
   * defect, and an optional prop would let a caller silently ship an empty
   * dropdown instead of failing the type check.
   */
  taxConsultants: readonly TaxConsultantOption[];
  /** The client's documents (already loaded by the parent): Coretax access,
   * EFIN and the SPT filings are read from them — no extra request. */
  documents?: readonly ClientDocument[];
  /** Test seam for the SPT deadlines; the card hands in its own day instead. */
  now?: Date;
  /** The card's day (`YYYY-MM-DD`, the payload's `as_of`). */
  day?: string;
  /** The profile's attention items; the SPT lines use them when present. */
  attention?: readonly AttentionItem[];
}) {
  // One day for the whole tab: the card's day at 00:00 WITA. The year
  // selector, its default and the SPT status all derive from it.
  const clock = now ?? new Date(dayToInstant(resolveCardDay(dayProp)));
  const clockYear = Number(witaDateKey(clock.getTime()).slice(0, 4));
  const [selectedYear, setSelectedYear] = useState<TaxYear>(clockYear);
  const [lkpmItems, setLkpmItems] = useState<LKPMBatchItem[]>([]);
  const [lkpmLoading, setLkpmLoading] = useState(false);
  const [lkpmReceipts, setLkpmReceipts] = useState<LKPMReceipt[]>([]);
  const [lkpmReceiptsLoading, setLkpmReceiptsLoading] = useState(false);

  // Fetch LKPM reports + OSS tanda terima for this client (shareholder cascade)
  useEffect(() => {
    if (!clientId) return;
    let cancelled = false;
    setLkpmLoading(true);
    setLkpmReceiptsLoading(true);
    Promise.allSettled([
      lkpmApi.getClientHistory(clientId),
      lkpmApi.getClientReceipts(clientId),
    ])
      .then(([histRes, recRes]) => {
        if (cancelled) return;
        setLkpmItems(histRes.status === "fulfilled" ? histRes.value.items : []);
        setLkpmReceipts(
          recRes.status === "fulfilled" ? recRes.value.items : [],
        );
      })
      .finally(() => {
        if (!cancelled) {
          setLkpmLoading(false);
          setLkpmReceiptsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [clientId]);

  // Receipts for the selected year, newest first across companies.
  const yearReceipts = lkpmReceipts
    .filter((r) => r.year === selectedYear)
    .sort(
      (a, b) =>
        (b.tanggal_diterima ?? "").localeCompare(a.tanggal_diterima ?? "") ||
        b.id - a.id,
    );
  const recentReceipts = yearReceipts.slice(0, RECEIPTS_VISIBLE);
  const olderReceipts = yearReceipts.slice(RECEIPTS_VISIBLE);

  // Group LKPM items by company for the selected year
  const lkpmByCompany = lkpmItems
    .filter((item) => item.year === selectedYear)
    .reduce<Record<string, LKPMBatchItem[]>>((acc, item) => {
      const key = item.company_name;
      if (!acc[key]) acc[key] = [];
      acc[key].push(item);
      return acc;
    }, {});

  const totalReceipts = yearReceipts.length;
  const approvedReceipts = yearReceipts.filter(
    (r) => r.oss_status === "Disetujui",
  ).length;

  const docs = documents ?? [];
  const coretax = coretaxStatus(docs);
  // Tax clients: a consultant is assigned or the client has LKPM history.
  const isTaxClient = !!client?.tax_consultant || lkpmItems.length > 0;
  const primaryLink =
    companyLinks?.find((l) => l.is_primary) ?? companyLinks?.[0];
  // Company SPT blocks: one per company when there is more than one. A
  // document carrying a company_id belongs to that company; one without
  // closes none when several companies are linked (the backend counts a
  // client-level SPT only for a client with exactly one linked company).
  const companyBlocks: {
    key: string;
    heading?: string;
    companyId?: number;
    docs: ClientDocument[];
  }[] =
    companyLinks && companyLinks.length > 1
      ? companyLinks.map((l) => ({
          key: String(l.company_id),
          heading: l.company_name,
          companyId: l.company_id,
          docs: docs.filter((d) => {
            const cid = (d as SptDocumentLike).company_id;
            return cid != null && cid === l.company_id;
          }),
        }))
      : [
          {
            key: "company",
            companyId: primaryLink?.company_id,
            docs: [...docs],
          },
        ];
  // Engine items per kind. An item is used when it exists for this subject;
  // without one the local helper answers, so a partial rollout never shows a
  // false quiet line.
  const sptItems = (k: SptKind) =>
    (attention ?? []).filter((i) => i.kind === k);
  const personalItem = sptItems("spt_personal")[0];
  const companyItems = sptItems("spt_company");
  const companyItemFor = (companyId?: number) =>
    companyItems.find((i) => i.subject_id === companyId) ??
    (companyBlocks.length === 1 ? companyItems[0] : undefined);

  return (
    <div className="space-y-6">
      {/* Header with year selector — layout pinned by the round-6 overflow
          guard in __tests__/client-detail-desk.test.tsx; do not unwrap. */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-lg font-semibold text-[var(--bz-text-1)]">
            Tax Overview
          </h3>
          <p className="text-sm text-[var(--bz-text-2)]">
            Manage tax obligations and filings
          </p>
        </div>
        <YearSelector
          selectedYear={selectedYear}
          currentYear={clockYear}
          onYearChange={setSelectedYear}
        />
      </div>

      {/* Tax identity — consultant assignment, NPWP as the Coretax ID and the
          Coretax access read from documents (the NIB lives on the Company tab;
          client's own value wins, company fallback keeps its provenance). */}
      <LedgerSection n={1} title="Tax identity">
        <div className="grid gap-x-6 gap-y-5 py-4 sm:grid-cols-2 xl:grid-cols-3">
          <TaxConsultantSelector
            clientId={clientId}
            initialValue={client?.tax_consultant}
            onSaved={onRefresh}
            consultants={taxConsultants}
          />
          {(() => {
            // `||`, not `??`: an empty-string npwp must fall through to
            // tax_id — with `??` the empty string hides the real tax_id and
            // the row wrongly reads "Not registered".
            const npwpValue = client?.npwp || client?.tax_id || undefined;
            return (
              <>
                <TaxIdItem
                  label="NPWP · Coretax ID"
                  value={npwpValue}
                  fallbackValue={
                    !npwpValue ? primaryLink?.npwp_company : undefined
                  }
                  fallbackLabel={primaryLink?.company_name}
                  note={
                    client?.client_type === "individual" &&
                    isSixteenDigits(npwpValue)
                      ? "NIK-based NPWP"
                      : undefined
                  }
                />
                <CoretaxAccessItem status={coretax} formatDate={formatDate} />
              </>
            );
          })()}
        </div>
      </LedgerSection>

      {isTaxClient ? (
        <LedgerSection n={2} title="Personal tax">
          <div className="py-4">
            <SptBlock
              kind="spt_personal"
              docs={docs}
              now={clock}
              attention={personalItem}
            />
          </div>
        </LedgerSection>
      ) : null}

      {isTaxClient ? (
        <LedgerSection n={3} title="Company tax">
          <div className="space-y-5 py-4">
            {companyBlocks.map((b) => (
              <SptBlock
                key={b.key}
                kind="spt_company"
                heading={b.heading}
                docs={b.docs}
                now={clock}
                attention={companyItemFor(b.companyId)}
              />
            ))}
          </div>
        </LedgerSection>
      ) : null}

      {/* LKPM — one hairline row per quarter, per company. */}
      <LedgerSection
        n={4}
        title={
          <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            LKPM {selectedYear}
            <span className="font-sans text-[12px] font-normal tracking-normal text-[var(--tx-secondary)]">
              Laporan Kegiatan Penanaman Modal
            </span>
          </span>
        }
      >
        {lkpmLoading ? (
          <p className="py-4 text-[13px] text-[var(--tx-secondary)]">
            Loading LKPM data...
          </p>
        ) : (
          <div className="space-y-5 py-4">
            {Object.entries(lkpmByCompany).map(([companyName, items]) => (
              <div key={companyName}>
                <p className={MICRO_LABEL}>{companyName}</p>
                <LkpmQuarterGrid items={items} />
              </div>
            ))}
            {Object.keys(lkpmByCompany).length === 0 ? (
              // No LKPM data — the static Q1-Q4 placeholders, one row each.
              <LkpmQuarterGrid items={[]} />
            ) : null}
          </div>
        )}
      </LedgerSection>

      {/* OSS Tanda Terima (receipts per kegiatan usaha) — shareholder cascade.
          Hidden entirely when there are none, as before. */}
      {lkpmReceiptsLoading ? (
        <p className="text-[13px] text-[var(--tx-secondary)]">
          Loading OSS tanda terima…
        </p>
      ) : totalReceipts > 0 ? (
        <LedgerSection n={5} title={`OSS tanda terima — ${selectedYear}`}>
          <div className="space-y-5 py-4">
            <p className="text-[12px] text-[var(--tx-secondary)]">
              {totalReceipts} receipt{totalReceipts === 1 ? "" : "s"}
              {approvedReceipts > 0 && (
                <>
                  {" · "}
                  <span className="text-[var(--state-success)]">
                    {approvedReceipts} approved
                  </span>
                </>
              )}
            </p>
            <ReceiptGrid
              receipts={recentReceipts}
              formatDate={formatDate}
              withHead
            />
            {olderReceipts.length > 0 ? (
              <details className="group" data-testid="receipts-more">
                <summary
                  className={`flex min-h-9 cursor-pointer list-none items-center gap-2 text-[13px] text-[var(--tx-pure)] ${FOCUS} [&::-webkit-details-marker]:hidden`}
                >
                  <svg
                    aria-hidden="true"
                    viewBox="0 0 16 16"
                    className="h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-180"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                  >
                    <path d="M3 6l5 5 5-5" />
                  </svg>
                  <span className="group-open:hidden">
                    Show {olderReceipts.length} more
                  </span>
                  <span className="hidden group-open:inline">Show fewer</span>
                </summary>
                <ReceiptGrid
                  receipts={olderReceipts}
                  formatDate={formatDate}
                  withHead={false}
                />
              </details>
            ) : null}
          </div>
        </LedgerSection>
      ) : null}

      {/* Tax Documents — the client's and each linked company's tax folder,
          browsed live from Drive (names as in Drive), files via the proxy. */}
      <LedgerSection n={6} title="Tax Documents">
        <TaxDocumentsSection
          clientFolderId={client?.google_drive_folder_id}
          companyLinks={companyLinks}
        />
      </LedgerSection>

      {/* The per-tab AiSummaryCard is gone: v3 drops it from every tab
          (product-visible removal, disclosed in the PR body). */}
    </div>
  );
}
