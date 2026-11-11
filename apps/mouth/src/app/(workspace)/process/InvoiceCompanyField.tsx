"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type {
  InvoiceCompanyChoice,
  InvoiceCompanyParams,
} from "@/lib/api/crm/crm.types";
import { logger } from "@/lib/logger";
import { toError } from "@/lib/types/common";

/**
 * "Invoice to company": the company a process is billed to. Empty means the
 * invoice goes to the individual. The prefill rule lives on the backend
 * (GET /api/crm/practices/invoice-company/suggestion); this component only
 * shows what it returns.
 */

export interface InvoiceCompanyValue {
  companyId: number | null;
  name: string;
  npwp: string;
}

export const EMPTY_INVOICE_COMPANY: InvoiceCompanyValue = {
  companyId: null,
  name: "",
  npwp: "",
};

export interface InvoiceCompanyAmbiguity {
  typed: string;
  candidates: { id: number; company_name: string }[];
}

const NEW = "__new__";

export function invoiceCompanyParams(
  value: InvoiceCompanyValue,
): InvoiceCompanyParams {
  if (value.companyId !== null) return { invoice_company_id: value.companyId };
  const name = value.name.trim();
  if (!name) return {};
  const npwp = value.npwp.trim();
  return {
    invoice_company_name: name,
    ...(npwp ? { invoice_company_npwp: npwp } : {}),
  };
}

/** The 409 the backend sends when a typed name matches several companies. */
export function ambiguityFromError(
  err: unknown,
  typed: string,
): InvoiceCompanyAmbiguity | null {
  const e = err as { statusCode?: number; data?: { detail?: unknown } };
  if (e?.statusCode !== 409) return null;
  const detail = e.data?.detail as
    | { code?: string; candidates?: InvoiceCompanyAmbiguity["candidates"] }
    | undefined;
  if (
    detail?.code !== "invoice_company_ambiguous" ||
    !Array.isArray(detail.candidates)
  ) {
    return null;
  }
  return { typed, candidates: detail.candidates };
}

interface Props {
  clientId: number | undefined;
  practiceTypeCode: string | undefined;
  value: InvoiceCompanyValue;
  onChange: (value: InvoiceCompanyValue) => void;
  /** Name of the company already saved on the practice (detail page). */
  savedName?: string | null;
  /** False on a practice that already has a company: never overwrite it. */
  autofill?: boolean;
  ambiguity?: InvoiceCompanyAmbiguity | null;
  onPickCandidate?: (id: number) => void;
  disabled?: boolean;
}

const fieldClass =
  "w-full px-3 py-2.5 rounded-lg border border-[var(--border)] bg-[var(--background-elevated)] text-[var(--foreground)] placeholder:text-[var(--foreground-muted)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/50";

export function InvoiceCompanyField({
  clientId,
  practiceTypeCode,
  value,
  onChange,
  savedName,
  autofill = true,
  ambiguity,
  onPickCandidate,
  disabled,
}: Props) {
  const [companies, setCompanies] = useState<InvoiceCompanyChoice[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [typing, setTyping] = useState(false);
  const touched = useRef(false);
  const autoFilled = useRef(false);
  const lastClient = useRef<number | undefined>(clientId);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (lastClient.current !== clientId) {
      lastClient.current = clientId;
      touched.current = false;
      autoFilled.current = false;
      setTyping(false);
      onChangeRef.current(EMPTY_INVOICE_COMPANY);
    }
    if (!clientId) {
      setCompanies([]);
      setLoaded(false);
      return;
    }
    let cancelled = false;
    Promise.resolve()
      .then(() =>
        api.crm.getInvoiceCompanySuggestion(clientId, practiceTypeCode),
      )
      .then((s) => {
        if (cancelled) return;
        setCompanies(s.companies);
        setLoaded(true);
        if (!autofill || touched.current) return;
        if (s.suggested_company_id !== null) {
          autoFilled.current = true;
          onChangeRef.current({
            companyId: s.suggested_company_id,
            name: "",
            npwp: "",
          });
        } else if (autoFilled.current) {
          autoFilled.current = false;
          onChangeRef.current(EMPTY_INVOICE_COMPANY);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setLoaded(true);
        logger.warn(
          "Invoice company suggestion unavailable",
          { component: "InvoiceCompanyField", action: "suggest" },
          toError(err),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [clientId, practiceTypeCode, autofill]);

  const options = [...companies];
  if (
    value.companyId !== null &&
    !options.some((c) => c.id === value.companyId)
  ) {
    options.push({
      id: value.companyId,
      company_name: savedName || `Company #${value.companyId}`,
      npwp_company: null,
      role: null,
      is_director: false,
    });
  }

  const showTypedInputs =
    typing || (loaded && options.length === 0 && value.companyId === null);
  const selectValue =
    value.companyId !== null ? String(value.companyId) : typing ? NEW : "";

  const pick = (raw: string) => {
    touched.current = true;
    if (raw === NEW) {
      setTyping(true);
      onChange({ companyId: null, name: value.name, npwp: value.npwp });
    } else if (raw === "") {
      setTyping(false);
      onChange(EMPTY_INVOICE_COMPANY);
    } else {
      setTyping(false);
      onChange({ companyId: Number(raw), name: "", npwp: "" });
    }
  };

  return (
    <div className="space-y-2" data-testid="invoice-company-field">
      {options.length > 0 && (
        <div className="flex gap-2">
          <select
            aria-label="Invoice to company"
            value={selectValue}
            disabled={disabled}
            onChange={(e) => pick(e.target.value)}
            className={fieldClass}
          >
            <option value="">No company, invoice the individual</option>
            {options.map((c) => (
              <option key={c.id} value={c.id}>
                {c.company_name}
                {c.is_director && options.length > 1 ? " (director)" : ""}
              </option>
            ))}
            <option value={NEW}>Another company, type its name...</option>
          </select>
          {value.companyId !== null && (
            <button
              type="button"
              aria-label="Clear invoice company"
              disabled={disabled}
              onClick={() => pick("")}
              className="px-3 rounded-lg border border-[var(--border)] text-sm text-[var(--foreground-muted)]"
            >
              Clear
            </button>
          )}
        </div>
      )}
      {showTypedInputs && (
        <div className="grid grid-cols-1 sm:grid-cols-[2fr_1fr] gap-2">
          <input
            type="text"
            aria-label="Company name"
            value={value.name}
            disabled={disabled}
            maxLength={200}
            onChange={(e) => {
              touched.current = true;
              onChange({
                companyId: null,
                name: e.target.value,
                npwp: value.npwp,
              });
            }}
            className={fieldClass}
            placeholder="Company name (optional)"
          />
          <input
            type="text"
            aria-label="Company NPWP"
            value={value.npwp}
            disabled={disabled}
            maxLength={30}
            onChange={(e) =>
              onChange({
                companyId: null,
                name: value.name,
                npwp: e.target.value,
              })
            }
            className={fieldClass}
            placeholder="NPWP (optional)"
          />
        </div>
      )}
      {ambiguity && (
        <div
          role="alert"
          className="rounded-lg border border-[var(--state-warning)] p-3 text-sm space-y-2"
          data-testid="invoice-company-ambiguity"
        >
          <p>
            Several companies match &ldquo;{ambiguity.typed}&rdquo;. Pick the
            right one:
          </p>
          <div className="flex flex-col gap-1">
            {ambiguity.candidates.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => onPickCandidate?.(c.id)}
                className="text-left px-3 py-1.5 rounded border border-[var(--border)] hover:bg-[var(--background-elevated)]"
              >
                {c.company_name}
              </button>
            ))}
          </div>
        </div>
      )}
      <p className="text-xs text-[var(--foreground-muted)]">
        {showTypedInputs
          ? "Typing a new name creates the company and links it to this client."
          : "The invoice is addressed to this company, attention to the client."}
      </p>
    </div>
  );
}
