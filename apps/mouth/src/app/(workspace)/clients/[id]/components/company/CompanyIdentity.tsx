import React from "react";
import { Copy } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { FOCUS, SERIF, StatePill, TABULAR } from "@/components/workspace/r19";
import type { PillTone } from "@/components/workspace/r19";

/** Copy to clipboard — the same affordance and toast the tab always had. */
export function CopyButton({ value, field }: { value: string; field: string }) {
  return (
    <button
      type="button"
      aria-label={`Copy ${field}`}
      onClick={() => {
        void navigator.clipboard.writeText(value);
        toast.success("Copied");
      }}
      className={cn(
        "inline-flex h-6 w-6 items-center justify-center text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]",
        FOCUS,
      )}
    >
      <Copy className="h-3 w-3" aria-hidden="true" />
    </button>
  );
}

function Tile({
  label,
  className,
  sub,
  action,
  children,
}: {
  label: string;
  className?: string;
  sub?: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn("flex flex-col gap-1.5 bg-[var(--bz-card)] p-4", className)}
    >
      <span className="text-[12px] font-medium text-[var(--tx-secondary)]">
        {label}
      </span>
      <span className="flex items-center gap-1.5">
        {children}
        {action}
      </span>
      {sub ? (
        <span className="text-[12px] text-[var(--tx-secondary)]">{sub}</span>
      ) : null}
    </div>
  );
}

const NUM = "text-[22px] leading-tight text-[var(--tx-pure)]";
const NONE = "text-[15px] text-[var(--tx-secondary)]";

/** Company identity: a serif name, entity chips, then a hairline lattice of
 * key-fact tiles. Tokens only; nothing here is a fill beyond the card ground. */
export function CompanyIdentity({
  name,
  typeLabel,
  typeGloss,
  status,
  foundingLines,
  aside,
  nib,
  npwp,
  capital,
  foundingDate,
  address,
  cityProvince,
  documentCount,
  onOpenDocuments,
}: {
  name: string;
  typeLabel: string;
  typeGloss?: string;
  status?: { tone: PillTone; label: string };
  /** Under the Established date, one per line: the AHU decree number(s), then the company's age. */
  foundingLines?: string[];
  /** Beside the name (below it on mobile): the AI summary slot. */
  aside?: React.ReactNode;
  nib?: string;
  npwp?: string;
  capital?: string | null;
  foundingDate?: string;
  address?: string;
  cityProvince?: string;
  documentCount: number;
  onOpenDocuments?: () => void;
}) {
  return (
    <div className="py-6">
      <div className="flex flex-wrap items-start justify-between gap-x-8 gap-y-4">
        <div className="min-w-0">
          <h3
            className="text-[34px] leading-[1.08] tracking-[-0.02em] text-[var(--tx-pure)]"
            style={SERIF}
          >
            {name}
          </h3>
        </div>
        {aside}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2.5">
        {typeLabel !== "—" ? (
          <span className="rounded-[2px] border border-[var(--tx-pure)] px-2 py-0.5 text-[12px] font-medium text-[var(--tx-pure)]">
            {typeLabel}
          </span>
        ) : null}
        {status ? <StatePill {...status} /> : null}
        {documentCount > 0 ? (
          onOpenDocuments ? (
            <button
              type="button"
              onClick={onOpenDocuments}
              className={cn(
                "text-[12.5px] text-[var(--tx-pure)] underline underline-offset-4",
                FOCUS,
              )}
            >
              {documentCount} {documentCount === 1 ? "document" : "documents"}{" "}
              on file
            </button>
          ) : (
            <span className="text-[12.5px] text-[var(--tx-secondary)]">
              {documentCount} {documentCount === 1 ? "document" : "documents"}{" "}
              on file
            </span>
          )
        ) : null}
      </div>
      {typeGloss ? (
        <p className="mt-2 text-[13px] text-[var(--tx-secondary)]">
          {typeGloss}
        </p>
      ) : null}

      <div className="mt-6 grid grid-cols-1 gap-px overflow-hidden rounded-lg border border-[var(--bz-border)] bg-[var(--bz-border)] sm:grid-cols-2 lg:grid-cols-3">
        <Tile
          label="Established"
          sub={
            foundingLines?.length ? (
              <span className="flex flex-col">
                {foundingLines.map((l) => (
                  <span key={l}>{l}</span>
                ))}
              </span>
            ) : undefined
          }
        >
          <span
            className={foundingDate ? NUM : NONE}
            style={foundingDate ? { ...SERIF, ...TABULAR } : undefined}
          >
            {foundingDate || "—"}
          </span>
        </Tile>
        <Tile
          label="NIB"
          sub={
            nib
              ? "Nomor Induk Berusaha · OSS registered"
              : "Nomor Induk Berusaha"
          }
          action={nib ? <CopyButton value={nib} field="NIB" /> : null}
        >
          <span
            className={nib ? NUM : NONE}
            style={nib ? { ...SERIF, ...TABULAR } : undefined}
          >
            {nib || "—"}
          </span>
        </Tile>
        <Tile
          label="NPWP"
          sub="Tax Identification Number"
          action={npwp ? <CopyButton value={npwp} field="NPWP" /> : null}
        >
          <span
            className={npwp ? NUM : NONE}
            style={npwp ? { ...SERIF, ...TABULAR } : undefined}
          >
            {npwp || "—"}
          </span>
        </Tile>
        <Tile label="Authorized capital" sub={capital ? "IDR" : undefined}>
          <span
            className={capital ? NUM : NONE}
            style={capital ? { ...SERIF, ...TABULAR } : undefined}
          >
            {capital || "—"}
          </span>
        </Tile>
        <Tile
          label="Registered address"
          className="sm:col-span-1 lg:col-span-2"
          sub={address && cityProvince ? cityProvince : undefined}
        >
          <span className="text-[15px] font-medium text-[var(--tx-pure)]">
            {address || cityProvince || "—"}
          </span>
        </Tile>
      </div>
    </div>
  );
}
